import base64
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import config
from config import OLLAMA_URL, MODELO, TIMEOUT_RESPOSTA, TIMEOUT_RESPOSTA_LOCAL, LMSTUDIO_URL, KOBOLDCPP_URL
import cerebros
import model_params
import gguf_info
import kobold_manager

# log dedicado pra quando o Ollama rejeita "tools" (400) e o Jarvis cai
# no fallback silencioso de tentar de novo SEM ferramentas — antes disso
# não sobrava rastro nenhum de que aconteceu (bug relatado em 19/08/2026:
# modelo com tools=True no seletor mesmo assim "inventando" chamada de
# função como texto solto, sem executar de verdade).
FALLBACK_LOG_PATH = os.path.join(config.BASE_DIR, "chat_erro.log")


def _log_fallback_tools(response, modelo):
    try:
        detalhe = None
        try:
            corpo = response.json()
            detalhe = corpo.get("error") if isinstance(corpo, dict) else None
        except Exception:
            pass
        if not detalhe:
            detalhe = (response.text or "").strip()[:300]
        linha = (f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [TOOLS-FALLBACK] "
                 f"modelo={modelo} — Ollama recusou a chamada COM ferramentas "
                 f"(400), refeita SEM ferramentas (o modelo respondeu sem "
                 f"saber que elas existem). Motivo do Ollama: {detalhe}\n")
        print(linha, end="")
        with open(FALLBACK_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(linha)
    except Exception:
        pass

# 🧠 camada de LLM: usa /api/chat do Ollama com suporte a tools
# (diferente do seu llm.py antigo que usava /api/generate sem tools)
#
# Cérebros de nuvem (Groq, 9router, e qualquer outro que você cadastrar
# em cerebros.json) são configuráveis sem mexer em código: cada um vira
# um prefixo "<nome>/<modelo>" no seletor, roteado por _chat_openai_compat.



# ── backend local ativo ─────────────────────────────────────────────
def _usar_lmstudio():
    """True quando o backend selecionado é o LM Studio."""
    return getattr(config, "SERVIDOR_LOCAL", "ollama").lower() == "lmstudio"


def _usar_koboldcpp():
    """True quando o backend selecionado é o KoboldCpp (integração nativa)."""
    return getattr(config, "SERVIDOR_LOCAL", "ollama").lower() == "koboldcpp"


def _url_local():
    """Retorna a URL base do backend local ativo (Ollama, LM Studio ou KoboldCpp)."""
    if _usar_koboldcpp():
        return config.KOBOLDCPP_URL
    return config.LMSTUDIO_URL if _usar_lmstudio() else config.OLLAMA_URL


def servidor_local_atual():
    """Nome do backend local ativo — usado pela interface."""
    if _usar_koboldcpp():
        return "koboldcpp"
    return "lmstudio" if _usar_lmstudio() else "ollama"


def trocar_servidor_local(nome):
    """Troca o backend local ao vivo (sem reiniciar).
    nome: 'ollama', 'lmstudio' ou 'koboldcpp'."""
    nome = nome.strip().lower()
    if nome not in ("ollama", "lmstudio", "koboldcpp"):
        return False
    anterior = servidor_local_atual()
    config.SERVIDOR_LOCAL = nome
    # Limpa caches de capacidades (cada backend reporta diferente)
    _CAP_CACHE.clear()
    _VISAO_CACHE.clear()
    # Ao SAIR do KoboldCpp, derruba o processo gerenciado (libera a VRAM).
    if anterior == "koboldcpp" and nome != "koboldcpp":
        try:
            kobold_manager.parar()
        except Exception:
            pass
    # Ollama/LM Studio não usam o wrapper nativo do llama-cpp-python.
    if nome != "koboldcpp":
        try:
            from llama_wrapper import get_wrapper
            wrapper = get_wrapper()
            wrapper.unload_model()
        except Exception:
            pass
    return True

# modelo em uso — pode ser trocado pela interface, sem reiniciar
MODELO_ATUAL = MODELO

# 📊 métricas (tempo, tokens) da ÚLTIMA chamada a chat() — lidas pelo
# agent.py logo depois de cada chamada, pra montar o resumo que aparece
# embaixo de cada resposta na interface. Não dá pra simplesmente anexar
# isso na mensagem retornada por chat() porque essa mensagem também é
# reenviada como histórico pro próprio modelo (e pra API do Ollama/
# OpenAI-compat) — campos extras ali seriam ruído no payload.
_ULTIMA_METRICA = {}


def obter_ultima_metrica():
    return dict(_ULTIMA_METRICA)

# 🔔 callbacks avisados quando o modelo muda (ex: modo desempenho local
# pausando processos pesados). Cada callback recebe (nome_modelo, eh_local).
_callbacks_troca_modelo = []


def registrar_callback_troca(func):
    _callbacks_troca_modelo.append(func)


def definir_modelo(nome):
    global MODELO_ATUAL
    MODELO_ATUAL = _resolver_tag_local(nome)
    local = not _eh_nuvem(nome)
    for cb in _callbacks_troca_modelo:
        try:
            cb(nome, local)
        except Exception:
            pass
    return MODELO_ATUAL


def modelo_atual():
    return MODELO_ATUAL


def descarregar_modelo_atual():
    """Descarrega o modelo LOCAL atual da VRAM AGORA, sem esperar o
    keep_alive (60m) expirar sozinho — truque documentado do próprio
    Ollama: manda keep_alive=0 sem mensagem nenhuma, ele libera a memória
    na hora. Seguro (é a mesma coisa que ele já faz sozinho por timeout,
    só que sob demanda); a próxima mensagem só recarrega mais devagar.
    Não faz nada com cérebro de nuvem (não usa VRAM local)."""
    if _eh_nuvem(MODELO_ATUAL):
        return False
    try:
        if _usar_koboldcpp():
            # KoboldCpp real: derruba o processo gerenciado (libera a VRAM).
            return kobold_manager.parar()
        elif _usar_lmstudio():
            # LM Studio: POST /v1/lm-studio/server/unload descarrega,
            # mas nem toda versão suporta — tenta e ignora se falhar.
            try:
                requests.post(f"{config.LMSTUDIO_URL}/v1/lm-studio/server/unload",
                              json={}, timeout=10)
            except Exception:
                pass
            return True
        else:
            # /api/generate com keep_alive=0 é o jeito mais confiável de
            # forçar o Ollama a liberar VRAM agora, sem esperar timeout.
            requests.post(f"{config.OLLAMA_URL}/api/generate",
                          json={"model": MODELO_ATUAL, "prompt": "", "keep_alive": 0},
                          timeout=10)
            return True
    except Exception:
        return False


def listar_modelos():
    """Lista os modelos instalados no backend local (Ollama, LM Studio ou KoboldCpp nativo)."""
    try:
        if _usar_koboldcpp():
            # KoboldCpp nativo: lista arquivos .gguf nas pastas configuradas
            pastas = getattr(config, "PASTAS_MODELOS_GGUF", [])
            if not pastas:
                # Fallback para pasta models/ do Jarvis se não configurado
                pastas = [os.path.join(config.BASE_DIR, "models")]

            modelos = []
            for pasta in pastas:
                if not os.path.exists(pasta):
                    continue
                # Busca recursivamente por arquivos .gguf
                for root, dirs, files in os.walk(pasta):
                    for f in files:
                        if f.endswith(".gguf"):
                            # Retorna caminho relativo ou absoluto
                            caminho_completo = os.path.join(root, f)
                            # Remove a pasta base para nomes mais curtos
                            # Ex: "C:\Users\Casa\.lmstudio\models\llama-3.2-3b.Q4_K_M.gguf"
                            #     -> "llama-3.2-3b.Q4_K_M.gguf"
                            # Caminho ABSOLUTO: o KoboldCpp precisa do
                            # arquivo real pra carregar, e o seletor usa
                            # esse valor como id do modelo.
                            modelos.append(caminho_completo)
            return sorted(set(modelos))
        elif _usar_lmstudio():
            r = requests.get(f"{config.LMSTUDIO_URL}/v1/models", timeout=3)
            return [m["id"] for m in r.json().get("data", [])]
        else:
            r = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=1.5)
            return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


_CAP_CACHE = {}


def suporta_tools(nome):
    """Verifica se o modelo aceita function calling (com cache).

    ⚠️ Visão e tools são capacidades INDEPENDENTES no Ollama — vários
    modelos de visão (ex: qwen2.5vl) NÃO suportam function calling, mesmo
    sendo perfeitamente usáveis pra descrever imagem. Achar que "tem
    visão = tem tools" (bug antigo daqui) faz o Jarvis mandar tools=TOOLS
    pra um modelo que não aceita, o Ollama devolve erro 400, o código
    reage tentando de novo SEM tools, e aí o modelo — agora sem saber
    que ferramentas existem — inventa uma resposta tipo "vou escrever um
    código pra você" em vez de avisar que não pode. Por isso aqui SEMPRE
    checa a capacidade real via /api/show pros modelos locais."""
    if _eh_nuvem(nome):
        return True
    if _usar_koboldcpp():
        # KoboldCpp: as ferramentas entram como TEXTO na fase de execução
        # do Jarvis (não via function-calling do servidor) — então, pro
        # seletor, o modelo não "tem tools" nativas.
        return False
    if nome in _CAP_CACHE:
        return _CAP_CACHE[nome]
    try:
        if _usar_lmstudio():
            # LM Studio: tenta ler capabilities de /v1/models; se não
            # achar o campo, assume True (a API compatível OpenAI cuida
            # do fallback se o modelo não suportar de verdade)
            resultado = True
            try:
                r = requests.get(f"{config.LMSTUDIO_URL}/v1/models", timeout=3)
                for m in r.json().get("data", []):
                    if m.get("id") == nome:
                        caps = m.get("capabilities")
                        # só sobrescreve se o LM Studio realmente informou
                        if isinstance(caps, list) and caps:
                            resultado = "tools" in caps or "function_calling" in caps
                        break
            except Exception:
                pass
        else:
            # timeout maior que o de outras checagens (1.5s era curto demais:
            # logo após trocar de modelo, o Ollama pode estar ainda carregando
            # ele na memória e não responder a tempo — isso fazia essa função
            # cachear "não suporta ferramentas" pro resto da sessão inteira,
            # mesmo o modelo suportando de verdade — bug relatado em 19/08/2026)
            r = requests.post(f"{config.OLLAMA_URL}/api/show",
                              json={"model": nome}, timeout=5)
            resultado = "tools" in r.json().get("capabilities", [])
    except Exception:
        # NÃO cacheia falha (timeout/erro de rede) — só cacheia um
        # resultado que realmente veio do Ollama, positivo ou negativo.
        # Assim uma falha passageira não gruda "sem ferramentas" pra
        # sempre; a próxima chamada tenta checar de novo.
        return False
    _CAP_CACHE[nome] = resultado
    return resultado


_VISAO_CACHE = {}
_NOMES_VISAO = (
    "vl", "llava", "vision", "visao", "pixtral",
    # modelos de nuvem com suporte a imagem conhecidos por nome (a lista de
    # capacidades via /api/show só existe pro Ollama local — pra cérebros
    # de nuvem, a única forma de saber sem chamada extra por provedor é
    # reconhecer o nome do modelo)
    "gpt-4o", "gpt-4.1", "gpt-4-turbo", "gpt-5", "o1", "o3", "o4",
    "claude-3", "claude-4", "claude-opus", "claude-sonnet", "claude-haiku",
    "gemini", "scout", "maverick", "grok-4", "grok-2-vision",
)


def suporta_visao(nome):
    """Verifica se o modelo aceita imagem (multimodal) — local (Ollama,
    via /api/show) ou de nuvem (por heurística de nome, já que cada
    provedor expõe capacidades de um jeito diferente e checar cada um
    exigiria uma integração dedicada por API)."""
    if nome in _VISAO_CACHE:
        return _VISAO_CACHE[nome]

    if _usar_koboldcpp():
        # KoboldCpp: só heurística de nome (o motor não expõe capability
        # 'vision' de forma padronizada).
        base = os.path.basename(nome).split(":")[0].lower()
        resultado = any(marca in base for marca in _NOMES_VISAO)
        _VISAO_CACHE[nome] = resultado
        return resultado

    encontrado = _prefixo_cerebro(nome)
    if encontrado:
        _, modelo = encontrado
        base = modelo.split(":")[0].lower()
        resultado = any(marca in base for marca in _NOMES_VISAO)
        _VISAO_CACHE[nome] = resultado
        return resultado

    resultado = False
    try:
        if _usar_lmstudio():
            # LM Studio: tenta pegar capabilities via /v1/models;
            # se não informar, cai na heurística de nome abaixo
            r = requests.get(f"{config.LMSTUDIO_URL}/v1/models", timeout=3)
            for m in r.json().get("data", []):
                if m.get("id") == nome:
                    caps = m.get("capabilities")
                    if isinstance(caps, list) and caps:
                        resultado = "vision" in caps
                    break
        else:
            r = requests.post(f"{config.OLLAMA_URL}/api/show",
                              json={"model": nome}, timeout=1.5)
            resultado = "vision" in r.json().get("capabilities", [])
    except Exception:
        pass

    # heuristica de nome como reforco/fallback (ex: "qwen2.5vl:7b",
    # "llava:13b") - a API de capabilities pode nao existir em versoes
    # antigas do Ollama
    if not resultado:
        base = nome.split(":")[0].lower()
        resultado = any(marca in base for marca in _NOMES_VISAO)

    _VISAO_CACHE[nome] = resultado
    return resultado


def informacoes_modelo(nome=None):
    """Ficha técnica do modelo (tela 'Informações do Modelo' da interface)
    — junta /api/show (arquitetura, quantização, contexto de treino real)
    com /api/tags (tamanho em disco) e os parâmetros que o usuário
    configurou na aba Model (contexto/camadas GPU escolhidos, se algum).

    Cérebro de nuvem não tem nada disso pra mostrar (não é um arquivo
    .gguf local) — devolve só o básico.

    ⚠️ Os campos de arquitetura (contexto de treino, nº de camadas,
    embedding, vocabulário) vêm de model_info no /api/show, e o NOME da
    chave muda por família (ex: 'qwen3.context_length',
    'llama.context_length') — por isso procura pelo SUFIXO em vez de uma
    chave fixa, funciona pra qualquer arquitetura que o Ollama suportar."""
    nome = nome or MODELO_ATUAL

    if _eh_nuvem(nome):
        return {"local": False, "modelo": nome}

    if _usar_koboldcpp():
        # Ficha técnica direto do cabeçalho do GGUF (arquitetura, camadas,
        # contexto de treino, embeddings, MoE) + o que está configurado na
        # aba Model + se o motor já está no ar.
        caminho = _caminho_gguf(nome)
        if not caminho:
            return {"local": True, "modelo": nome, "backend": "koboldcpp",
                    "erro": "arquivo .gguf não encontrado nas pastas configuradas"}
        info = gguf_info.ler_metadados(caminho)
        params = model_params.obter(nome)
        st = kobold_manager.status()
        info.update({
            # a aba Model lê estes nomes canônicos (os MESMOS que o ramo
            # do Ollama devolve) — remapeados do cabeçalho do GGUF
            "familia": info.get("arquitetura"),
            "quantizacao": info.get("quant"),
            "contexto_treino": info.get("ctx_treino"),
            "expert_count": info.get("n_experts"),
            "expert_used_count": info.get("experts_ativos"),
            "local": True, "modelo": nome, "backend": "koboldcpp",
            "suporta_visao": False, "suporta_tools": False,
            "carregado": bool(st.get("modelo")),
            "contexto_configurado": params.get("num_ctx"),
            "num_gpu_configurado": params.get("num_gpu"),
            "use_mmap": params.get("use_mmap", True),
            "num_thread_configurado": params.get("num_thread"),
        })
        return info

    if _usar_lmstudio():
        # LM Studio não tem /api/show — devolve info básica
        return {"local": True, "modelo": nome, "backend": "lmstudio",
                "nota": "Informações detalhadas disponíveis apenas no painel do LM Studio."}

    try:
        show = requests.post(f"{config.OLLAMA_URL}/api/show",
                              json={"model": nome}, timeout=5).json()
    except Exception as e:
        return {"local": True, "modelo": nome, "erro": f"Ollama offline ou modelo não encontrado: {e}"}

    if "error" in show:
        return {"local": True, "modelo": nome, "erro": show["error"]}

    detalhes = show.get("details") or {}
    info = show.get("model_info") or {}

    def _por_sufixo(sufixo):
        for chave, valor in info.items():
            if chave.endswith(sufixo):
                return valor
        return None

    tamanho_bytes = None
    digest = None
    try:
        tags = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=3).json().get("models", [])
        for m in tags:
            if m.get("name") == nome or m.get("model") == nome:
                tamanho_bytes = m.get("size")
                digest = m.get("digest")
                break
    except Exception:
        pass

    params = model_params.obter()
    contexto_treino = _por_sufixo(".context_length")
    n_camadas = _por_sufixo(".block_count")

    return {
        "local": True,
        "modelo": nome,
        "digest": digest,  # não existe "caminho de arquivo .gguf" amigável
        # no Ollama (fica em blob com hash) — o digest é o mais próximo
        # disso que a API oferece.
        "formato": detalhes.get("format"),
        "familia": detalhes.get("family"),
        "parametros": detalhes.get("parameter_size"),
        "quantizacao": detalhes.get("quantization_level"),
        "tamanho_bytes": tamanho_bytes,
        "contexto_treino": contexto_treino,
        "embedding_size": _por_sufixo(".embedding_length"),
        "vocab_size": _por_sufixo(".vocab_size"),
        "n_camadas": n_camadas,  # teto real da GPU slider — NÃO é 999 fixo
        # 🧪 MoE (mixture-of-experts, ex: Qwen3.6-35B-A3B): quando o GGUF
        # tem essas duas chaves, o modelo é MoE de verdade — mais confiável
        # que adivinhar pelo nome (ver moe_tuner.eh_moe).
        "expert_count": _por_sufixo(".expert_count"),
        "expert_used_count": _por_sufixo(".expert_used_count"),
        "suporta_visao": "vision" in (show.get("capabilities") or []),
        "suporta_tools": "tools" in (show.get("capabilities") or []),
        # o que está configurado agora na aba Model (None = automático)
        "contexto_configurado": params.get("num_ctx"),
        "num_gpu_configurado": params.get("num_gpu"),
        "use_mmap": params.get("use_mmap", True),
        "use_mlock": params.get("use_mlock", False),
        "num_thread_configurado": params.get("num_thread"),
        "mtp_configurado": params.get("mtp_draft_num_predict"),
        "ssqp_ativo": params.get("ssqp_ativo", False),
    }


_PENSAMENTO_CACHE = {}
_NOMES_PENSAMENTO = (
    "deepseek-r1", "qwen3", "glm", "qwq", "o1", "o3", "o4",
    "claude-opus", "claude-sonnet",  # modelos com "extended thinking"
)


def suporta_pensamento(nome):
    """Verifica se o modelo tem modo de raciocínio nativo (thinking/
    reasoning) — usado pelo ícone 🧠 do painel de capacidades. Só por
    heurística de nome, igual o fallback de suporta_visao(): o Ollama não
    expõe uma capability 'thinking' padronizada via /api/show, e cada
    provedor de nuvem trata isso do jeito dele. A lista (_FAMILIA_RACIOCINIO
    logo acima) é a mesma usada pra troca automática de modelo em pedido
    de raciocínio/lógica — reaproveitada aqui pra não duplicar critério."""
    if nome in _PENSAMENTO_CACHE:
        return _PENSAMENTO_CACHE[nome]

    encontrado = _prefixo_cerebro(nome)
    base = encontrado[1].split(":")[0].lower() if encontrado else nome.split(":")[0].lower()
    resultado = any(marca in base for marca in _NOMES_PENSAMENTO)

    # LM Studio: tenta confirmar via /v1/models se disponível
    if not resultado and _usar_lmstudio() and not encontrado:
        try:
            r = requests.get(f"{config.LMSTUDIO_URL}/v1/models", timeout=3)
            for m in r.json().get("data", []):
                if m.get("id") == nome:
                    caps = m.get("capabilities", [])
                    if isinstance(caps, list) and ("reasoning" in caps or "thinking" in caps):
                        resultado = True
                    break
        except Exception:
            pass

    _PENSAMENTO_CACHE[nome] = resultado
    return resultado


# pontos fortes por família de modelo (a ordem importa: mais específico primeiro)
PONTOS_FORTES = [
    ("qwen2.5-coder", "especialista em código"),
    ("deepseek-coder", "especialista em código"),
    ("codellama", "especialista em código"),
    ("qwen3-visao", "entende imagens"),
    ("qwen2.5vl", "entende imagens (sem ferramentas)"),
    ("llava", "entende imagens"),
    ("qwen3", "raciocínio forte"),
    ("qwen2.5", "o melhor com ferramentas"),
    ("llama3.2", "leve e rápido"),
    ("llama3.1", "equilibrado, bom em tudo"),
    ("llama3", "conversa geral"),
    ("mistral", "rápido e direto"),
    ("gemma", "escrita e criatividade"),
    ("deepseek", "raciocínio"),
    ("phi", "leve, bom em lógica"),
]


def descrever_modelo(nome):
    n = nome.lower()
    for prefixo, desc in PONTOS_FORTES:
        if n.startswith(prefixo):
            return desc
    return "uso geral"


def listar_modelos_detalhado():
    """Lista modelos com suporte a ferramentas e ponto forte."""
    modelos = []

    # ☁️ cérebros de nuvem configurados em cerebros.json (Groq, 9router,
    # ou qualquer outro que você cadastrar) — aparecem primeiro
    for c in cerebros.listar():
        for m in c["modelos"]:
            modelos.append({"nome": f"{c['nome']}/{m}", "tools": True,
                            "descricao": f"nuvem ({c['tipo']}: {c['nome']})"})

    # 💻 modelos locais (Ollama ou LM Studio) — a checagem de "suporta ferramentas"
    # é 1 requisição HTTP por modelo; feita em paralelo (em vez de uma
    # de cada vez) pra não travar a tela quando há muitos modelos
    # instalados e o cache (_CAP_CACHE) ainda está frio (logo após
    # reiniciar o Jarvis).
    locais = listar_modelos()
    if locais:
        with ThreadPoolExecutor(max_workers=min(8, len(locais))) as pool:
            resultados = list(pool.map(suporta_tools, locais))
        modelos += [{"nome": n, "tools": tem_tools, "descricao": descrever_modelo(n)}
                    for n, tem_tools in zip(locais, resultados)]
    return modelos


# 🔀 troca automática de modelo LOCAL conforme o tipo de pedido — resolve
# o atrito de precisar lembrar de trocar manualmente pro modelo de visão
# quando anexa uma imagem, ou pro coder quando pede código. Só considera
# modelos JÁ INSTALADOS (via listar_modelos()), nunca sugere um pull.

_FAMILIA_CODIGO = ("qwen2.5-coder", "deepseek-coder", "codellama")
_FAMILIA_RACIOCINIO = ("deepseek-r1", "qwen3")

_PADRAO_CODIGO = re.compile(
    r"```|\b(c[óo]digo|fun[çc][ãa]o|script|bug|debug|refator|compil|"
    r"exce[çc][ãa]o|stack\s*trace|regex|algoritmo|classe\b|m[ée]todo\b|"
    r"python|javascript|typescript|java\b|c\+\+|\.py\b|\.js\b|\.ts\b|"
    r"\.java\b|\.cpp\b|\.cs\b|sql\b|api\b|endpoint|biblioteca|framework)\b",
    re.IGNORECASE
)

_PADRAO_RACIOCINIO = re.compile(
    r"\b(passo a passo|prove que|demonstre|resolva|equa[çc][ãa]o|deriv"
    r"|integral|teorema|l[óo]gica|raciocínio|raciocinio|calcule)\b",
    re.IGNORECASE
)

# mensagens só de conversa fiada (sem nenhuma pergunta/pedido) — se vier
# JUNTO com uma imagem anexada, o anexo provavelmente ficou esquecido no
# campo, não é o assunto da mensagem. Não vale a pena carregar um modelo
# de visão de 6-8GB só por causa disso.
_PADRAO_SEM_RELACAO_IMAGEM = re.compile(
    r"^\s*(oi+|ol[áa]|opa|e a[íi]|bom dia|boa tarde|boa noite|"
    r"obrigad[oa]|valeu|tchau|at[ée] mais|ok|blz|beleza|tudo bem\??|"
    r"como vai\??)[\s!.,?]*$",
    re.IGNORECASE
)


def _imagem_relevante(mensagem):
    """A imagem anexada tem a ver com o que o usuário está pedindo?
    Mensagem vazia conta como relevante (sem texto, presume-se que o
    pedido É a imagem: 'olha isso aqui'). Só descarta em saudação/
    despedida pura, sem nenhum pedido junto."""
    msg = (mensagem or "").strip()
    if not msg:
        return True
    return not _PADRAO_SEM_RELACAO_IMAGEM.match(msg)


def categoria_mensagem(mensagem, tem_imagem=False):
    """Classifica o pedido em VISAO/CODIGO/RACIOCINIO/GERAL — usado tanto
    pra escolher o modelo automático quanto pra decidir se vale a pena o
    modelo 'pensar' antes de responder (ver chat(pensar=...) mais abaixo).
    Público de propósito: agent.py reaproveita a MESMA classificação pras
    duas decisões, em vez de reclassificar a mensagem duas vezes."""
    if tem_imagem and _imagem_relevante(mensagem):
        return "VISAO"
    if _PADRAO_CODIGO.search(mensagem or ""):
        return "CODIGO"
    if _PADRAO_RACIOCINIO.search(mensagem or ""):
        return "RACIOCINIO"
    return "GERAL"


def _atual_serve_para(categoria, atual):
    """Já é bom o suficiente pra categoria? Evita trocar (e recarregar
    modelo na VRAM) sem necessidade real."""
    if categoria == "VISAO":
        return suporta_visao(atual)
    if categoria == "CODIGO":
        return atual.split(":")[0].lower().startswith(_FAMILIA_CODIGO)
    if categoria == "RACIOCINIO":
        return atual.split(":")[0].lower().startswith(_FAMILIA_RACIOCINIO)
    return True  # GERAL: qualquer modelo serve, nunca força troca


def _melhor_candidato(categoria, instalados):
    if categoria == "VISAO":
        candidatos = [m for m in instalados if suporta_visao(m)]
        # entre os de visão, prioriza os que TAMBÉM suportam ferramentas
        # (evita cair de novo no modo híbrido sem necessidade)
        candidatos.sort(key=lambda m: not suporta_tools(m))
        return candidatos[0] if candidatos else None
    familia = _FAMILIA_CODIGO if categoria == "CODIGO" else _FAMILIA_RACIOCINIO
    for prefixo in familia:
        for m in instalados:
            if m.split(":")[0].lower().startswith(prefixo):
                return m
    return None


def escolher_modelo_automatico(mensagem, tem_imagem=False):
    """Decide se troca de modelo LOCAL pra atender melhor o pedido atual.
    Nunca mexe num cérebro de nuvem (escolha manual, deliberada). Devolve
    o nome do modelo a usar — pode ser o mesmo de agora (sem troca)."""
    atual = MODELO_ATUAL
    if _eh_nuvem(atual):
        return atual

    categoria = categoria_mensagem(mensagem, tem_imagem)
    if categoria == "GERAL" or _atual_serve_para(categoria, atual):
        return atual

    instalados = listar_modelos()
    if not instalados:
        return atual

    candidato = _melhor_candidato(categoria, instalados)
    return candidato or atual


def _prefixo_cerebro(nome):
    """Se o modelo for "<cerebro>/<modelo>" e existir em cerebros.json,
    devolve (config_do_cerebro, nome_do_modelo_sem_prefixo). Senão None."""
    if "/" not in nome:
        return None
    prefixo, resto = nome.split("/", 1)
    c = cerebros.buscar(prefixo)
    if c:
        return c, resto
    return None


def _eh_nuvem(nome):
    return _prefixo_cerebro(nome) is not None


def eh_nuvem(nome=None):
    """Versão pública: True se o cérebro indicado (ou o atual) é de
    nuvem (cerebros.json); False = modelo local (Ollama)."""
    return _eh_nuvem(nome if nome is not None else MODELO_ATUAL)


def _caminho_gguf(nome):
    """Resolve um nome/caminho de modelo para o caminho ABSOLUTO do .gguf,
    procurando nas pastas configuradas (PASTAS_MODELOS_GGUF). Devolve None
    se não encontrar — usado pelo backend KoboldCpp, que precisa do
    arquivo real pra carregar."""
    if not nome:
        return None
    if os.path.isabs(nome) and os.path.exists(nome):
        return nome
    alvo = os.path.basename(nome)
    pastas = getattr(config, "PASTAS_MODELOS_GGUF", []) or \
        [os.path.join(config.BASE_DIR, "models")]
    for pasta in pastas:
        if not os.path.isdir(pasta):
            continue
        direto = os.path.join(pasta, alvo)
        if os.path.exists(direto):
            return direto
        for root, _dirs, files in os.walk(pasta):
            if alvo in files:
                return os.path.join(root, alvo)
    return None


def _resolver_tag_local(nome):
    """O Ollama sempre lista os modelos com a tag explícita (ex:
    'llama3.1:latest'), mas o padrão configurado pode vir sem tag
    ('llama3.1'). Sem isso o modelo funciona no chat (o Ollama aceita
    nome sem tag), mas nunca bate com a lista — some do seletor e o
    navegador escolhe outro cérebro sozinho. Resolve pra tag real."""
    if _eh_nuvem(nome):
        return nome
    instalados = listar_modelos()
    if nome in instalados:
        return nome
    for m in instalados:
        if m.split(":")[0] == nome:
            return m
    return nome


# resolve a tag real assim que o Ollama estiver disponível (não trava a
# inicialização se ele estiver offline — só tenta de novo depois)
MODELO_ATUAL = _resolver_tag_local(MODELO_ATUAL)


def _mime_de_base64(b64):
    """Adivinha o mime type a partir dos primeiros bytes (assinatura do
    formato) — necessário porque o formato "images" do Ollama é so uma
    lista de base64 sem mime, mas a API da OpenAI/Groq exige uma data URI
    completa (ex: "data:image/png;base64,...")."""
    try:
        cabeca = base64.b64decode(b64[:24] + "==")
    except Exception:
        return "image/png"
    if cabeca.startswith(b"\x89PNG"):
        return "image/png"
    if cabeca.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if cabeca.startswith(b"GIF8"):
        return "image/gif"
    if cabeca.startswith(b"BM"):
        return "image/bmp"
    if cabeca[:4] == b"RIFF":
        return "image/webp"
    return "image/png"


def _para_openai(messages):
    """Converte o histórico (formato Ollama) para o formato OpenAI/Groq,
    gerando os tool_call_id que a Groq exige. Mensagens com imagem
    (campo "images", lista de base64 — mesmo formato usado pro Ollama
    local) viram um content-array multimodal, formato que a API da
    OpenAI/Groq/9router espera pra enxergar a imagem de verdade."""
    saida = []
    ids_pendentes = []
    n = 0

    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            tcs = []
            ids_pendentes = []
            for tc in m["tool_calls"]:
                n += 1
                cid = tc.get("id") or f"call_{n}"
                ids_pendentes.append(cid)
                args = tc.get("function", {}).get("arguments", {})
                if not isinstance(args, str):
                    args = json.dumps(args)
                tcs.append({"id": cid, "type": "function",
                            "function": {"name": tc["function"]["name"],
                                         "arguments": args}})
            saida.append({"role": "assistant",
                          "content": m.get("content") or "",
                          "tool_calls": tcs})
        elif m.get("role") == "tool":
            cid = ids_pendentes.pop(0) if ids_pendentes else f"call_{n}"
            saida.append({"role": "tool", "tool_call_id": cid,
                          "content": m.get("content", "")})
        elif m.get("images"):
            partes = [{"type": "text", "text": m.get("content") or ""}]
            for img_b64 in m["images"]:
                mime = _mime_de_base64(img_b64)
                partes.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{img_b64}"},
                })
            saida.append({"role": m.get("role", "user"), "content": partes})
        else:
            saida.append({"role": m.get("role", "user"),
                          "content": m.get("content", "")})
    return saida


def _parsear_resposta_chat(texto):
    """Normalmente é 1 JSON só. Mas alguns gateways (ex: 9router) às
    vezes respondem em formato SSE ("data: {...}\\n\\ndata: {...}") mesmo
    com stream=False, ou até um hibrido: JSON valido com "data: [DONE]"
    colado direto no final, sem quebra de linha (visto na pratica com
    combos do 9router) — aqui juntamos/limpamos tudo antes de devolver,
    em vez de estourar em r.json()."""
    texto = (texto or "").strip()
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        pass

    # JSON valido seguido de lixo (ex: "{...}data: [DONE]") — pega só o
    # primeiro objeto JSON valido e ignora o resto, sem precisar saber
    # qual é exatamente o formato do lixo
    try:
        obj, _ = json.JSONDecoder().raw_decode(texto)
        if isinstance(obj, dict) and "choices" in obj:
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # formato SSE: uma ou mais linhas "data: {...}", terminando em [DONE]
    conteudo = ""
    tool_calls = {}  # index -> {id, function: {name, arguments}}
    ultimo = None
    for linha in texto.splitlines():
        linha = linha.strip()
        if not linha.startswith("data:"):
            continue
        bruto = linha[len("data:"):].strip()
        if not bruto or bruto == "[DONE]":
            continue
        try:
            pedaco = json.loads(bruto)
        except json.JSONDecodeError:
            continue
        ultimo = pedaco
        escolha = (pedaco.get("choices") or [{}])[0]
        delta = escolha.get("delta") or escolha.get("message") or {}
        if delta.get("content"):
            conteudo += delta["content"]
        for tc in delta.get("tool_calls") or []:
            i = tc.get("index", 0)
            atual = tool_calls.setdefault(
                i, {"id": tc.get("id"), "function": {"name": "", "arguments": ""}})
            if tc.get("id"):
                atual["id"] = tc["id"]
            f = tc.get("function") or {}
            if f.get("name"):
                atual["function"]["name"] += f["name"]
            if f.get("arguments"):
                atual["function"]["arguments"] += f["arguments"]

    mensagem = {"role": "assistant", "content": conteudo}
    if tool_calls:
        mensagem["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]

    base = ultimo or {}
    base["choices"] = [{"message": mensagem}]
    return base


def _chat_openai_compat(url, api_key, modelo, messages, tools=None,
                        timeout=TIMEOUT_RESPOSTA, on_token=None,
                        on_metrica=None,
                        opcoes_extra=None):
    """Cliente genérico para qualquer endpoint compatível com a API da
    OpenAI (/v1/chat/completions): usado por qualquer cérebro cadastrado
    em cerebros.json (Groq, 9router, ou outro) E pelo LM Studio local.
    Devolve a resposta já traduzida para o formato do Ollama.

    on_token: callback opcional pra streaming (LM Studio) — chamado com
    cada pedaço de texto conforme chega, igual ao on_token do Ollama."""
    global _ULTIMA_METRICA
    usar_stream = bool(on_token)
    payload = {
        "model": modelo,
        "messages": _para_openai(messages),
        "stream": usar_stream,  # sem isso alguns gateways (ex: 9router) mandam
                           # a resposta em pedaços SSE mesmo sem pedir,
                           # e r.json() quebra com "Extra data" (vários
                           # objetos JSON colados no corpo)
    }
    if tools:
        payload["tools"] = tools

    # 🎛️ aba Model (temperature/max_tokens) — mesmos parâmetros valem
    # tanto pra modelo local quanto pra cérebro de nuvem compatível com
    # OpenAI, só muda o formato do campo
    params = model_params.obter()
    payload["temperature"] = params.get("temperature", 0.7)
    # ⚠️ -1 (infinito) e -2 (preencher contexto) são convenção do
    # options.num_predict do PRÓPRIO OLLAMA — a API da OpenAI (usada por
    # todo cérebro de nuvem daqui) não entende max_tokens negativo, só
    # rejeitaria a chamada inteira. Só repassa quando for um teto
    # positivo de verdade; 0/-1/-2 aqui viram "não manda o campo", igual
    # já era o comportamento pra 0.
    if params.get("max_tokens", 0) > 0:
        payload["max_tokens"] = params["max_tokens"]

    # 🧊 override pontual (ex: KoboldCpp precisa de um max_tokens explícito,
    # porque o default do servidor pode ser curtinho)
    if opcoes_extra:
        for _k, _v in opcoes_extra.items():
            payload[_k] = _v

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    inicio = time.perf_counter()

    if usar_stream:
        # ── streaming: lê token a token via SSE ──
        r = requests.post(url, headers=headers, json=payload,
                          stream=True, timeout=timeout)
        r.raise_for_status()
        conteudo = ""
        tool_calls = {}
        tok_out = 0
        tok_in = 0
        primeiro_token_t = None
        for linha in r.iter_lines(decode_unicode=True):
            if not linha:
                continue
            if linha.startswith("data:"):
                bruto = linha[len("data:"):].strip()
                if not bruto or bruto == "[DONE]":
                    continue
                try:
                    pedaco = json.loads(bruto)
                except json.JSONDecodeError:
                    continue
                # usage pode vir no último chunk
                u = pedaco.get("usage")
                if u:
                    tok_in = u.get("prompt_tokens", 0) or 0
                    tok_out = u.get("completion_tokens", 0) or 0
                escolha = (pedaco.get("choices") or [{}])[0]
                delta = escolha.get("delta") or {}
                texto = delta.get("content") or ""
                if texto:
                    if primeiro_token_t is None:
                        primeiro_token_t = time.perf_counter()
                    conteudo += texto
                    try:
                        on_token(texto)
                    except Exception:
                        pass
                    # Emitir métrica parcial ao vivo
                    if on_metrica:
                        try:
                            on_metrica({
                                'tokens_saida': tok_out + 1,
                                'tokens_entrada': tok_in,
                                'tempo_s': time.perf_counter() - inicio,
                                'tokens_por_segundo': (tok_out / (time.perf_counter() - primeiro_token_t)) if (primeiro_token_t and (time.perf_counter() - primeiro_token_t) > 0) else 0,
                                'contexto_tokens': tok_in + tok_out,
                            })
                        except Exception:
                            pass
                # tool_calls em streaming
                for tc in delta.get("tool_calls") or []:
                    i = tc.get("index", 0)
                    atual = tool_calls.setdefault(
                        i, {"id": tc.get("id"), "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        atual["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        atual["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        atual["function"]["arguments"] += fn["arguments"]

        tempo_s = time.perf_counter() - inicio
        # tok/s baseado no tempo de GERAÇÃO (após 1º token), não no total
        tempo_geracao = (time.perf_counter() - primeiro_token_t) if primeiro_token_t else tempo_s
        if not tok_out:
            # fallback: estima pelo tamanho do texto (~4 chars/token)
            tok_out = max(1, len(conteudo) // 4)
        _ULTIMA_METRICA = {
            "tempo_s": tempo_s,
            "tokens_entrada": tok_in,
            "tokens_saida": tok_out,
            "tokens_por_segundo": (tok_out / tempo_geracao) if (tok_out and tempo_geracao > 0) else 0,
        }
        msg = {"role": "assistant", "content": conteudo}
        if tool_calls:
            msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        return msg

    # ── sem streaming (cérebros de nuvem) ──
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    tempo_s = time.perf_counter() - inicio
    r.raise_for_status()

    dados = _parsear_resposta_chat(r.text)
    m = dados["choices"][0]["message"]

    # formato OpenAI padrão: usage.prompt_tokens / completion_tokens —
    # nem todo gateway manda isso (ex: alguns proxies omitem), por isso
    # tudo tem fallback pra 0 em vez de quebrar
    usage = dados.get("usage") or {}
    tok_in = usage.get("prompt_tokens", 0) or 0
    tok_out = usage.get("completion_tokens", 0) or 0
    _ULTIMA_METRICA = {
        "tempo_s": tempo_s,
        "tokens_entrada": tok_in,
        "tokens_saida": tok_out,
        "tokens_por_segundo": (tok_out / tempo_s) if (tok_out and tempo_s > 0) else 0,
    }

    msg = {"role": "assistant", "content": m.get("content") or ""}
    if m.get("tool_calls"):
        msg["tool_calls"] = [
            {"id": tc.get("id"),
             "function": {"name": tc["function"]["name"],
                          "arguments": tc["function"]["arguments"]}}
            for tc in m["tool_calls"]
        ]
    return msg


def chat_direct(messages, on_token=None, opcoes_extra=None):
    """Caminho mais direto possível para o modelo local — usa llama_wrapper
    nativo (mesmo motor do KoboldCpp), SEM passar por Ollama/LM Studio/HTTP.
    Ideal para modo RAW: máxima velocidade, zero overhead de rede ou
    serialização JSON intermediária.
    Devolve o mesmo formato que chat(): {"role": "assistant", "content": ...}.
    Se llama_wrapper não estiver disponível, faz fallback silencioso para
    chat() normal (mantém compatibilidade)."""
    global _ULTIMA_METRICA
    # KoboldCpp: o modo RAW também vai pelo motor real (GPU), não pelo
    # llama-cpp-python nativo (que nesta máquina é CPU-only).
    if _usar_koboldcpp():
        return _chat_koboldcpp_native(messages, None, on_token)
    try:
        from llama_wrapper import get_wrapper, LoadConfig
    except ImportError:
        print("[RAW] fallback: llama_wrapper indisponível, usando HTTP")
        return chat(messages, tools=None, pensar=False, on_token=on_token)

    try:
        wrapper = get_wrapper()
        params = model_params.obter()
        model_path = None
        model_name = MODELO_ATUAL

        # Busca caminho do GGUF nas pastas configuradas
        if os.path.isabs(model_name) and os.path.exists(model_name):
            model_path = model_name
        else:
            pastas = getattr(config, "PASTAS_MODELOS_GGUF", [])
            if not pastas:
                pastas = [os.path.join(config.BASE_DIR, "models")]
            for pasta in pastas:
                if not os.path.exists(pasta):
                    continue
                caminho = os.path.join(pasta, model_name)
                if os.path.exists(caminho):
                    model_path = caminho
                    break
                if not model_path:
                    for root, dirs, files in os.walk(pasta):
                        if model_name in files:
                            model_path = os.path.join(root, model_name)
                            break
                if model_path:
                    break

        if not model_path:
            print(f"[RAW] fallback: modelo não encontrado ({model_name}), usando HTTP")
            return chat(messages, tools=None, pensar=False, on_token=on_token)

        n_gpu_layers = params.get("num_gpu", -1)
        if params.get("gpu_backend_autofit", True):
            n_gpu_layers = -1

        load_config = LoadConfig(
            model_path=model_path,
            n_gpu_layers=n_gpu_layers,
            n_ctx=params.get("num_ctx", 16384),
            kv_cache_dtype=params.get("quant_kv_cache", "f16") or "f16",
            flash_attention=params.get("flash_attention", True),
            batch_size=params.get("batch_size", 512),
            low_vram=params.get("low_vram", False),
            n_threads=params.get("num_thread"),
            use_mmap=params.get("use_mmap", True),
            use_mlock=params.get("use_mlock", False),
            f16_kv=(params.get("quant_kv_cache") in (None, "f16", "bf16")),
        )

        # Garante que o modelo está carregado — reutiliza se já estiver com
        # a mesma config (evita recarregar a VRAM a cada mensagem).
        wrapper.ensure_model(load_config)

        # Parâmetros de geração
        max_tokens = params.get("max_tokens", 512) or 512
        temperature = params.get("temperature", 0.7)

        # chat() do wrapper agora aceita on_token para streaming
        texto, metrics = wrapper.chat(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            on_token=on_token,
        )

        _ULTIMA_METRICA = {
            "tempo_s": metrics.get("tempo_segundos", 0),
            "tokens_entrada": 0,  # llama_wrapper não expõe prompt_tokens
            "tokens_saida": metrics.get("tokens_gerados", 0),
            "tokens_por_segundo": metrics.get("tokens_por_segundo", 0),
        }
        return {"role": "assistant", "content": texto}
    except Exception as e:
        print(f"[RAW] fallback: erro no wrapper nativo ({e}), usando HTTP")
        return chat(messages, tools=None, pensar=False, on_token=on_token)


def carga_koboldcpp(modelo=None):
    """kwargs de carga que o KoboldCpp entende (ver kobold_manager._montar_argv).
    Função separada de propósito: o teste teste_lfm25.py manda EXATAMENTE
    este mesmo payload, senão o motor reinicia entre o teste e o chat real."""
    params = model_params.obter(modelo or MODELO_ATUAL)
    return {
        "num_ctx": params.get("num_ctx") or 8192,
        "num_gpu": params.get("num_gpu"),
        "quant_kv_cache": params.get("quant_kv_cache") or "f16",
        "flash_attention": params.get("flash_attention", True),
        "num_thread": params.get("num_thread"),
        "batch_size": params.get("batch_size"),
        "low_vram": params.get("low_vram", False),
        # repassa pro gerenciador: jinja ligado (template do GGUF) e o
        # pensar_modo ("nunca") que vira --jinjathink false lá no argv
        "pensar_modo": params.get("pensar_modo"),
        "jinja": params.get("jinja", True),
        # autofit: deixa o KoboldCpp escolher as camadas (mesmo sentido
        # do gpu_backend_autofit já usado no caminho RAW do llama_wrapper)
        "gpu_backend_autofit": params.get("gpu_backend_autofit", True),
    }


def _chat_koboldcpp_native(messages, tools=None, on_token=None, opcoes_extra=None):
    """Chat Livre pelo KoboldCpp REAL: sobe (se preciso) o koboldcpp.exe
    gerenciado com o GGUF atual e conversa pela API compatível com OpenAI
    dele (porta 5001, com os kwargs escolhidos: contexto, camadas na GPU,
    quant do KV, Flash Attention, threads). Zero injeção de ferramentas
    aqui — as ferramentas só entram na fase de execução do agente, como
    TEXTO. Devolve o mesmo formato do chat() e, quando há streaming, chama
    on_token a cada pedaço."""
    global _ULTIMA_METRICA

    caminho = _caminho_gguf(MODELO_ATUAL)
    if not caminho:
        raise RuntimeError(
            f"Arquivo GGUF não encontrado para '{MODELO_ATUAL}'.\n"
            f"Confira as pastas configuradas em PASTAS_MODELOS_GGUF.")

    params = model_params.obter(MODELO_ATUAL)
    carga = carga_koboldcpp(MODELO_ATUAL)
    # troca pontual de contexto/camadas sem mexer no global (ex: testes)
    if opcoes_extra:
        for _k in ("num_ctx", "num_gpu"):
            if _k in opcoes_extra:
                carga[_k] = opcoes_extra[_k]

    ok, msg = kobold_manager.garantir(caminho, carga, esperar=True)
    if not ok:
        raise RuntimeError(f"KoboldCpp não subiu: {msg}")

    # o default do servidor pode ser curto; garante um teto razoável
    max_tokens = params.get("max_tokens")
    if not max_tokens or max_tokens <= 0:
        max_tokens = 1024

    return _chat_openai_compat(
        f"{config.KOBOLDCPP_URL}/v1/chat/completions",
        "",  # KoboldCpp local não precisa de chave
        MODELO_ATUAL, messages, tools=None,
        timeout=TIMEOUT_RESPOSTA_LOCAL,
        on_token=on_token,
        opcoes_extra={"max_tokens": max_tokens})


def chat(messages, tools=None, pensar=None, on_token=None, opcoes_extra=None):
    """Envia a conversa + lista de ferramentas. O modelo decide:
    - responder texto, ou
    - pedir chamadas de ferramenta (tool_calls).

    pensar: controla o modo "thinking" nativo do Ollama (suportado por
    qwen3, deepseek-r1 e outros modelos híbridos) — None = deixa o
    padrão do modelo; True/False = força ligado/desligado. Serve pra
    evitar que um modelo de raciocínio "pense" (às vezes em chinês, no
    caso do qwen3 — comportamento conhecido, não é bug) numa pergunta
    trivial tipo "bom dia", gastando tempo e tokens à toa. Cérebro de
    nuvem ignora esse parâmetro (cada provedor trata thinking do jeito
    dele; fica pra outra hora).

    on_token: callback opcional chamado com cada pedaço de texto assim
    que chega do modelo (streaming), pra interface mostrar a resposta
    "nascendo" em vez de esperar tudo pronto. Só funciona com modelo
    LOCAL (Ollama) — cérebro de nuvem por enquanto ignora e responde do
    jeito de sempre (sem streaming). Independente de streaming ou não,
    a função sempre devolve a MESMA estrutura no final: {"role":
    "assistant", "content": ..., "tool_calls": [...]}.

    opcoes_extra: dict opcional que sobrescreve/complementa o options{}
    do Ollama SÓ nesta chamada, sem mexer na config global da aba Model
    (ex: tradução se beneficia de temperature mais baixa que o valor
    salvo pelo usuário para conversa normal — ver tools.py,
    traduzir_documento_em_blocos). Ignorado por cérebro de nuvem (só
    afeta o caminho local/Ollama)."""

    encontrado = _prefixo_cerebro(MODELO_ATUAL)
    if encontrado:
        c, modelo = encontrado
        return _chat_openai_compat(c["url"], c["chave"], modelo, messages, tools,
                                    timeout=c.get("timeout", TIMEOUT_RESPOSTA))

    # ── KoboldCpp nativo: usa llama_wrapper para otimizações de VRAM ──
    if _usar_koboldcpp():
        return _chat_koboldcpp_native(messages, tools, on_token, opcoes_extra)

    # ── LM Studio: usa a mesma API compatível com OpenAI dos cérebros ──
    if _usar_lmstudio():
        return _chat_openai_compat(
            f"{config.LMSTUDIO_URL}/v1/chat/completions",
            "",  # LM Studio local não precisa de chave
            MODELO_ATUAL, messages, tools,
            timeout=TIMEOUT_RESPOSTA_LOCAL,
            on_token=on_token)

    payload = {
        "model": MODELO_ATUAL,
        "messages": messages,
        "stream": bool(on_token),
        "keep_alive": "60m"  # mantém o modelo na RAM (evita recarga lenta)
    }

    if tools:
        payload["tools"] = tools

    # 🎛️ aba Model — Ollama usa "options" pra temperature/num_predict
    # (num_predict é o nome do Ollama pra max_tokens). -1 = geração
    # infinita (para só no stop natural do modelo), -2 = gera até encher
    # a janela de contexto inteira. 0 = não manda o campo, usa o padrão
    # do Modelfile — por isso a verificação acima exclui explicitamente 0.
    params = model_params.obter()
    options = {"temperature": params.get("temperature", 0.7)}
    # max_tokens=-1 = geração infinita (Ollama), -2 = preenche contexto,
    # 0 = não manda o campo (padrão do modelo). Valores -1/-2 são falsy
    # em Python, por isso a verificação explícita.
    if params.get("max_tokens") not in (None, 0):
        options["num_predict"] = params["max_tokens"]

    # 🖥️ carga do modelo local (camadas GPU, contexto, mmap/mlock, threads)
    # — None = "automático", não manda o campo e deixa o Ollama decidir
    # sozinho, igual sempre funcionou. Só sobrescreve quando o usuário
    # mexeu explicitamente na aba Model (ver comentário longo em
    # model_params.py sobre cada um desses).
    if params.get("num_gpu") is not None:
        options["num_gpu"] = params["num_gpu"]
    if params.get("num_ctx") is not None:
        options["num_ctx"] = params["num_ctx"]

    # 🧪 SSQP — nome dado pelo usuário pro sistema de auto-ajuste (ver
    # moe_tuner.py). Quando ligado, e existe um preset TESTADO E
    # CONFIRMADO pro modelo ATUAL (não é estimativa, foi testado de
    # verdade), ele tem prioridade sobre os campos gerais de num_gpu/
    # num_ctx acima — que são globais, valem pra qualquer modelo.
    # Desligado = ignora o preset e usa só a config manual/automática de
    # sempre, pra dar pra comparar o mesmo modelo nos dois modos.
    if params.get("ssqp_ativo"):
        import moe_tuner  # import local: evita import circular (moe_tuner importa llm)
        preset = moe_tuner.obter_preset(MODELO_ATUAL)
        if preset:
            options["num_gpu"] = preset["num_gpu"]
            options["num_ctx"] = preset["num_ctx"]

    if params.get("num_thread") is not None:
        options["num_thread"] = params["num_thread"]
    # mmap/mlock têm default próprio do Ollama (mmap ligado, mlock
    # desligado) — só valem a pena mandar quando DIFERENTES do padrão,
    # mas mandar sempre não quebra nada e deixa explícito o que está
    # em uso; mais simples de depurar.
    options["use_mmap"] = params.get("use_mmap", True)
    options["use_mlock"] = params.get("use_mlock", False)

    # 🚀 MTP (Multi-Token Prediction) — None = auto (não manda o campo,
    # deixa o Ollama/modelo decidir sozinho); 0 = desliga explicitamente;
    # 1-8 = liga com esse nº de tokens de rascunho. Só tem efeito real em
    # GGUF que já traz o draft head de MTP embutido (ex: quants
    # "...-MTP-..."); em modelo sem isso o Ollama ignora o campo.
    if params.get("mtp_draft_num_predict") is not None:
        options["draft_num_predict"] = params["mtp_draft_num_predict"]

    # 🎛️ Parâmetros avançados de inferência — passa pro Ollama se tiver
    # valor explícito (None = usa default do modelo/Ollama)
    if params.get("num_batch") is not None:
        options["num_batch"] = params["num_batch"]
    if params.get("num_ubatch") is not None:
        options["num_ubatch"] = params["num_ubatch"]
    if params.get("seed") is not None:
        options["seed"] = params["seed"]
    if params.get("top_k") is not None:
        options["top_k"] = params["top_k"]
    if params.get("top_p") is not None:
        options["top_p"] = params["top_p"]
    if params.get("repeat_penalty") is not None:
        options["repeat_penalty"] = params["repeat_penalty"]
    if params.get("num_keep") is not None:
        options["num_keep"] = params["num_keep"]
    if params.get("mirostat") is not None:
        options["mirostat"] = params["mirostat"]
    if params.get("mirostat_tau") is not None:
        options["mirostat_tau"] = params["mirostat_tau"]
    if params.get("mirostat_eta") is not None:
        options["mirostat_eta"] = params["mirostat_eta"]

    if opcoes_extra:
        options.update(opcoes_extra)

    payload["options"] = options

    if pensar is not None:
        payload["think"] = pensar

    if on_token:
        return _chat_stream(payload, on_token, tools)

    inicio = time.perf_counter()
    response = requests.post(
        f"{config.OLLAMA_URL}/api/chat",
        json=payload,
        timeout=TIMEOUT_RESPOSTA_LOCAL
    )

    # modelo sem suporte a ferramentas (erro 400)? tenta de novo em modo chat
    if response.status_code == 400 and tools:
        _log_fallback_tools(response, payload.get("model"))
        payload.pop("tools", None)
        inicio = time.perf_counter()  # reconta o tempo, é uma chamada nova
        response = requests.post(
            f"{config.OLLAMA_URL}/api/chat",
            json=payload,
            timeout=TIMEOUT_RESPOSTA_LOCAL
        )

    tempo_s = time.perf_counter() - inicio
    _levantar_erro_ollama(response)

    data = response.json()

    # o Ollama devolve essas métricas nativamente em cada resposta:
    # prompt_eval_count = tokens de entrada, eval_count = tokens gerados,
    # eval_duration = tempo (nanossegundos) gasto só gerando (mais preciso
    # que o tempo_s da requisição inteira, que inclui fila/carregamento)
    tok_in = data.get("prompt_eval_count", 0) or 0
    tok_out = data.get("eval_count", 0) or 0
    eval_duration_s = (data.get("eval_duration", 0) or 0) / 1e9

    global _ULTIMA_METRICA
    _ULTIMA_METRICA = {
        "tempo_s": tempo_s,
        "tokens_entrada": tok_in,
        "tokens_saida": tok_out,
        "tokens_por_segundo": (tok_out / eval_duration_s) if (tok_out and eval_duration_s > 0) else 0,
    }

    return data["message"]  # {"role": "assistant", "content": ..., "tool_calls": [...]}


def _levantar_erro_ollama(response):
    """response.raise_for_status() sozinho só mostra "500 Server Error:
    Internal Server Error for url: ..." — inútil pra diagnosticar. O
    Ollama manda o motivo de verdade no corpo (JSON {"error": "..."}),
    tipo "model requires more system memory than is available" ou um
    erro de arquitetura não suportada por essa versão do Ollama. Isso
    troca a mensagem genérica pela real, ou mantém a genérica se o
    corpo não vier em JSON."""
    if response.status_code < 400:
        return
    detalhe = None
    try:
        corpo = response.json()
        detalhe = corpo.get("error") if isinstance(corpo, dict) else None
    except Exception:
        pass
    if not detalhe:
        detalhe = (response.text or "").strip()[:300]
    if detalhe:
        raise RuntimeError(
            f"Ollama respondeu {response.status_code}: {detalhe}")
    response.raise_for_status()


def _chat_stream(payload, on_token, tools):
    """Mesma coisa que o corpo principal de chat(), só que lendo a resposta
    do Ollama em pedaços (NDJSON, uma linha por token/grupo de tokens) e
    chamando on_token a cada pedaço de texto, em vez de esperar a resposta
    inteira. No final, monta e devolve a MESMA estrutura de sempre, e
    guarda as métricas do jeito de sempre — o resto do Jarvis (loop de
    ferramentas, métricas, histórico) não percebe diferença nenhuma."""
    inicio = time.perf_counter()
    response = requests.post(
        f"{config.OLLAMA_URL}/api/chat",
        json=payload,
        stream=True,
        timeout=TIMEOUT_RESPOSTA_LOCAL
    )

    if response.status_code == 400 and tools:
        _log_fallback_tools(response, payload.get("model"))
        payload = dict(payload)
        payload.pop("tools", None)
        inicio = time.perf_counter()
        response = requests.post(
            f"{config.OLLAMA_URL}/api/chat",
            json=payload,
            stream=True,
            timeout=TIMEOUT_RESPOSTA_LOCAL
        )

    _levantar_erro_ollama(response)

    conteudo_total = ""
    tool_calls_finais = None
    chunk_final = {}

    for linha in response.iter_lines():
        if not linha:
            continue
        try:
            chunk = json.loads(linha)
        except ValueError:
            continue

        msg = chunk.get("message") or {}
        pedaco = msg.get("content") or ""
        if pedaco:
            conteudo_total += pedaco
            try:
                on_token(pedaco)
            except Exception:
                pass

        if msg.get("tool_calls"):
            tool_calls_finais = msg["tool_calls"]

        if chunk.get("done"):
            chunk_final = chunk

    tempo_s = time.perf_counter() - inicio

    tok_in = chunk_final.get("prompt_eval_count", 0) or 0
    tok_out = chunk_final.get("eval_count", 0) or 0
    eval_duration_s = (chunk_final.get("eval_duration", 0) or 0) / 1e9

    global _ULTIMA_METRICA
    _ULTIMA_METRICA = {
        "tempo_s": tempo_s,
        "tokens_entrada": tok_in,
        "tokens_saida": tok_out,
        "tokens_por_segundo": (tok_out / eval_duration_s) if (tok_out and eval_duration_s > 0) else 0,
    }

    resultado = {"role": "assistant", "content": conteudo_total}
    if tool_calls_finais:
        resultado["tool_calls"] = tool_calls_finais
    return resultado


def verificar_ollama():
    """Confere se o backend local (Ollama ou LM Studio) está online e se o modelo existe."""
    if _eh_nuvem(MODELO_ATUAL):
        return None  # cérebro de nuvem/roteador não depende do backend local
    if _usar_koboldcpp():
        if kobold_manager.esta_rodando():
            return None
        return ("INFO: KoboldCpp ainda não está no ar. Ele sobe sozinho "
                f"na primeira mensagem, carregando "
                f"'{os.path.basename(MODELO_ATUAL)}'.")
    backend = "LM Studio" if _usar_lmstudio() else "Ollama"
    url_base = _url_local()
    try:
        modelos = listar_modelos()

        if not modelos:
            return f"AVISO: {backend} respondeu mas sem modelos carregados em {url_base}"

        if not any(m.startswith(MODELO_ATUAL) for m in modelos):
            return (f"AVISO: modelo '{MODELO_ATUAL}' nao encontrado no {backend}. "
                    f"Modelos disponiveis: {modelos}. "
                    + ("Rode: ollama pull " + MODELO_ATUAL if not _usar_lmstudio()
                       else "Carregue o modelo no LM Studio."))

        return None  # tudo ok
    except Exception as e:
        return f"ERRO: {backend} offline em {url_base} ({e})"
