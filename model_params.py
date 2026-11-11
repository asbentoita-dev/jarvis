# 🎛️ Parâmetros do Model — controles essenciais de geração (Prompt do
# Sistema extra, Temperature, Chamada de função, max_tokens), configuráveis
# pela aba "⚙️ Model" da interface sem precisar mexer em código. Persiste
# em model_params.json, mesmo padrão de pastas_config.py/mcp_config.py.
#
# Esses 4 foram escolhidos por serem os que mais afetam modelo local via
# Ollama no dia a dia do Jarvis:
# - prompt_sistema_extra: some ao final do SYSTEM_PROMPT fixo do agente
#   (não substitui — o SYSTEM_PROMPT tem a lógica do loop agêntico, perfil
#   do usuário etc; isso aqui é só um adicional, tipo "responda sempre em
#   tom mais formal").
# - temperature: passado pro Ollama (options.temperature) e pros cérebros
#   de nuvem compatíveis com OpenAI (campo "temperature" direto).
# - function_calling: liga/desliga chamada de ferramentas. Desligado força
#   o Jarvis a responder só em texto, sem tools — útil pra debugar um
#   modelo que não lida bem com tool_calls, ou só pra bater papo sem
#   custo extra de contexto (o catálogo de tools ocupa espaço no prompt).
# - max_tokens: teto de tokens gerados (Ollama: options.num_predict;
#   cérebro de nuvem: max_tokens). 0 = deixa o padrão do modelo/provedor.
#
# 🖥️ Carga do modelo LOCAL (Ollama) — só se aplica a modelo Ollama, cérebro
# de nuvem ignora (esses parâmetros não existem em API OpenAI-compat).
# Todos com default None = "auto" (deixa o Ollama decidir sozinho, igual
# hoje). Só entram no payload de llm.chat() quando o usuário definir um
# valor explícito (ver model_info() em llm.py pra saber o teto real de
# cada modelo, ex: nº de camadas / contexto de treino — o teto NÃO é fixo
# tipo 999, é lido do próprio modelo carregado):
# - num_gpu: nº de camadas offloaded pra GPU ("ngl" no llama.cpp). None =
#   automático (Ollama calcula pela VRAM livre). 0 = força tudo na CPU.
#   Um valor alto demais (maior que o nº real de camadas do modelo, ou
#   maior do que a VRAM livre aguenta) não quebra nada — o Ollama trava
#   no máximo possível — mas pode estourar VRAM se for MUITO acima do
#   que cabe; a interface mostra um valor "seguro" sugerido.
# - num_ctx: tamanho da janela de contexto em tokens. None = usa o padrão
#   do modelo/Modelfile. Contexto maior consome mais VRAM (cache KV).
# - use_mmap: True (padrão) = mapeia o arquivo do modelo em memória em vez
#   de carregar tudo de uma vez (mais rápido pra recarregar, usa menos
#   RAM de pico). False = desliga o mmap ("no_mmap" do llama.cpp) — carga
#   inicial mais lenta, mas evita I/O de disco durante a geração; só vale
#   a pena em discos muito lentos ou pra forçar tudo residente em RAM.
# - use_mlock: False (padrão) = SO pode colocar páginas do modelo em swap
#   sob pressão de memória. True = trava o modelo em RAM física (não
#   deixa o SO fazer swap dele) — reduz stutter, mas exige RAM sobrando.
# - num_thread: nº de threads de CPU usadas na parte que roda fora da
#   GPU. None = Ollama decide (geralmente nº de núcleos físicos).
#
# 🚀 mtp_draft_num_predict: liga/desliga/ajusta o MTP (Multi-Token
# Prediction) — só funciona em GGUFs que já trazem o "draft head" de MTP
# embutido (ex: os quants "...-MTP-..." tipo Qwen3.5-9B-MTP-Q4_K_M). Vira
# o campo "draft_num_predict" no options{} mandado pro Ollama
# (/api/chat, /api/generate). NÃO adiciona MTP a um modelo que não tem —
# só controla quantos tokens de rascunho usar num modelo que JÁ tem.
# - None (padrão): não manda o campo — deixa o Ollama/modelo decidir
#   sozinho (equivalente a "auto", igual aos outros campos de carga).
# - 0: desliga o MTP explicitamente, mesmo em modelo que suporta.
# - 1 a 8: liga com esse nº de tokens de rascunho por passo. Comece em 1
#   (~26% mais rápido em testes) e vá testando até 4; acima disso o
#   ganho tende a cair e pode até piorar (mais rejeição de rascunho).
# É um parâmetro de GERAÇÃO (não de carga do modelo), então não precisa
# descarregar o modelo da VRAM pra aplicar — vale já na próxima mensagem.

# 🤔 pensar_modo: liga/desliga o "thinking" nativo (qwen3, deepseek-r1,
# glm hybrid-thinking...) na mão, em vez de deixar o agent.py decidir
# sozinho pela categoria da mensagem (RACIOCINIO liga, resto desliga).
# - "auto" (padrão): comportamento de sempre — decide pela categoria.
# - "sempre": força thinking ligado em toda mensagem (mesmo "bom dia"),
#   pode deixar a resposta mais lenta/cara em token à toa.
# - "nunca": força thinking desligado sempre — útil pra debugar um
#   modelo que "pensa" longo demais ou em outro idioma (comportamento
#   conhecido do qwen3), ou só pra respostas mais rápidas.
# Só tem efeito em modelo LOCAL que suporta thinking (llm.suporta_pensamento);
# nos outros casos o campo "think" nem é mandado pro Ollama.

import json
import os

import config

MODEL_PARAMS_PATH = os.path.join(config.BASE_DIR, "model_params.json")
# 🗂️ config POR MODELO (ex: cada .gguf do KoboldCpp com seu próprio
# contexto/camadas) — uma seção por arquivo de modelo, por cima dos
# padrões globais de model_params.json. Ver obter()/salvar(modelo=...).
MODEL_CONFIGS_PATH = os.path.join(config.BASE_DIR, "model_configs.json")

_PADRAO = {
    "prompt_sistema_extra": "",
    "temperature": 0.7,
    "function_calling": True,
    "max_tokens": 0,  # 0 = sem limite explícito (usa o padrão do modelo)
    "num_gpu": None,     # None = automático (Ollama decide pela VRAM livre)
    "num_ctx": None,     # None = usa o padrão do modelo/Modelfile
    "use_mmap": True,
    "use_mlock": False,
    "num_thread": None,  # None = Ollama decide
    # 🎛️ Parâmetros avançados de inferência (Ollama llama.cpp)
    "num_batch": None,       # Tamanho do lote de avaliação (internal batch size)
    "num_ubatch": None,      # Tamanho do ubatch físico (physical batch size)
    "seed": None,            # Seed aleatória (None = aleatório)
    "top_k": None,           # Top-k sampling (40 = padrão Ollama)
    "top_p": None,           # Top-p sampling (0.9 = padrão Ollama)
    "repeat_penalty": None,  # Penalidade de repetição (1.1 = padrão Ollama)
    "num_keep": None,        # Tokens a manter no contexto (0 = default do modelo)
    "mirostat": None,        # Mirostat (0 = desligado, 1 = v1, 2 = v2)
    "mirostat_tau": None,    # Mirostat tau (5.0 = padrão)
    "mirostat_eta": None,    # Mirostat eta (0.1 = padrão)
    "pensar_modo": "auto",  # "auto" | "sempre" | "nunca"
    "mtp_draft_num_predict": None,  # None = auto | 0 = desligado | 1-8 = ligado
    # 🧪 SSQP (nome dado pelo usuário em 25/08/2026) — liga/desliga a
    # aplicação automática do preset de camadas GPU/CPU testado e salvo
    # pelo auto-ajuste (ver moe_tuner.py) pro modelo ATUALMENTE ativo,
    # SEM sobrescrever os campos globais num_gpu/num_ctx acima. Desligado
    # = comportamento normal (usa só os campos manuais/automático de
    # sempre) — dá pra alternar ligado/desligado com o mesmo modelo pra
    # comparar na prática se o preset testado realmente ajuda ou não.
    "ssqp_ativo": False,
    # 🔎 motor_busca: qual motor a busca_web() (tools.py) deve priorizar.
    # "auto" (padrão) = mantém a cascata automática de sempre (Tavily ->
    # SearxNG -> Brave -> Yandex -> DuckDuckGo), tentando um depois do
    # outro até um funcionar. Escolher um motor específico ("tavily",
    # "yandex" ou "brave") tenta ESSE primeiro; se ele falhar (sem chave,
    # erro de rede, etc.) ainda cai pro resto da cascata na mesma ordem de
    # sempre — nunca trava numa busca vazia só por preferência manual.
    "motor_busca": "auto",  # "auto" | "tavily" | "yandex" | "brave"

    # ⚡ modo_raw: MODE RAW DIRECT - sem overhead para performance máxima
    # Quando True, o Jarvis usa o modelo DIRETAMENTE via llama.cpp sem:
    # - Loop agêntico (sem ferramentas, sem planejamento)
    # - Troca automática de modelo
    # - Pausa de serviços (sd-api, etc.)
    # - Memória vetorial
    # - Linha de produção
    # - Tudo é uma chamada direta ao modelo: prompt → resposta
    # Ideal para performance máxima e respostas rápidas.
    "modo_raw": False,

    # 🧊 Otimizações do KoboldCpp (só aplicam quando SERVIDOR_LOCAL=koboldcpp)
    # quant_kv_cache: tipo de quantização do KV cache
    # - None (padrão): f16 (sem economia)
    # - "q8_0": ~53% do tamanho original (recomendado para balanço)
    # - "q4_0": ~28% do tamanho original (máxima economia, leve perda de qualidade)
    "quant_kv_cache": None,  # None | "f16" | "bf16" | "q8_0" | "q5_1" | "q4_0"

    # flash_attention: usa Flash Attention para processamento mais eficiente
    # - True (padrão): ligado, -10% a -20% de VRAM + mais rápido
    # - False: desligado (compatibilidade com hardware muito antigo)
    "flash_attention": True,

    # batch_size: tamanho do lote de processamento
    # - None (padrão): usa padrão do llama.cpp (512)
    # - Menor valor = menos VRAM, mais lento
    # - Maior valor = mais VRAM, mais rápido (até certo ponto)
    "batch_size": None,  # None | int (recomendado: 256-512)

    # low_vram: modo de baixa VRAM (último recurso)
    # - False (padrão): KV cache na VRAM (normal)
    # - True: KV cache na RAM (muito mais lento, mas economiza VRAM)
    "low_vram": False,

    # gpu_backend_autofit: calcula automaticamente camadas ótimas para GPU
    # - True (padrão): autofit igual ao KoboldCpp (--gpulayers -1)
    # - False: usa valor manual de num_gpu
    "gpu_backend_autofit": True,

    # jinja: formatação de chat via template Jinja do próprio GGUF (KoboldCpp --jinja)
    # - True (padrão, decisão do usuário 19/10/2026): usa o chat_template
    #   embutido no modelo (preserva tool_calls, thinking etc). Se o
    #   template falhar, o KoboldCpp cai sozinho no formato universal.
    # - False: formato universal do KoboldCpp (exige num_ctx múltiplo de 64
    #   e ignora o template do modelo). Só desligue se um modelo específico
    #   estiver quebrando COM o template (pode ser configurado por modelo).
    "jinja": True,
}

_PENSAR_MODOS_VALIDOS = ("auto", "sempre", "nunca")
_MOTORES_BUSCA_VALIDOS = ("auto", "tavily", "yandex", "brave")

# 🧊 Tipos de quantização do KV cache REALMENTE aceitos pelo llama.cpp/
# KoboldCpp (--quantkv). Antes esta lista trazia vários tipos que NÃO são
# de KV cache (q2_0, iq*, tq*, mxfp4_moe...) e teria feito o motor abortar
# ao receber um valor inválido. Só estes 5 existem de verdade.
# Fonte: `koboldcpp.exe --help` (--quantkv [f16|bf16|q8_0|q5_1|q4_0]).
_QUANT_KV_VALIDOS = (None, "f16", "bf16", "q8_0", "q5_1", "q4_0")


def _ler():
    if not os.path.exists(MODEL_PARAMS_PATH):
        _salvar(_PADRAO)
        return dict(_PADRAO)
    try:
        with open(MODEL_PARAMS_PATH, "r", encoding="utf-8") as f:
            dados = json.load(f)
        if isinstance(dados, dict):
            # preenche com o padrão qualquer chave que falte (versão antiga
            # do arquivo, ou chave nova adicionada depois)
            saida = dict(_PADRAO)
            saida.update(dados)
            return saida
    except Exception:
        pass
    return dict(_PADRAO)


def _salvar(dados):
    with open(MODEL_PARAMS_PATH, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)


def _chave_modelo(nome):
    """Chave do bloco por-modelo em model_configs.json — usa só o nome do
    arquivo (basename), pra bater tanto com caminho absoluto quanto com
    nome curto."""
    return os.path.basename(nome).strip() if nome else ""


def _ler_configs():
    if not os.path.exists(MODEL_CONFIGS_PATH):
        return {}
    try:
        with open(MODEL_CONFIGS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _salvar_configs(d):
    with open(MODEL_CONFIGS_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def obter(modelo=None):
    """Lê o arquivo toda vez (arquivo é pequeno, não vale a pena cachear
    e arriscar servir valor desatualizado se salvar em outra aba/processo).

    Com `modelo` informado, mescla (por cima) o bloco específico daquele
    modelo salvo em model_configs.json — é o "config por modelo" (ex: cada
    .gguf do KoboldCpp com seu próprio contexto/camadas), sem mexer nos
    padrões globais."""
    base = _ler()
    if modelo:
        sec = _ler_configs().get(_chave_modelo(modelo))
        if isinstance(sec, dict):
            base.update({k: v for k, v in sec.items() if k in _PADRAO})
    return base


def salvar(valores, modelo=None):
    if modelo:
        atual = dict(_PADRAO)
        atual.update(_ler_configs().get(_chave_modelo(modelo)) or {})
    else:
        atual = _ler()

    if "prompt_sistema_extra" in valores:
        atual["prompt_sistema_extra"] = str(valores.get("prompt_sistema_extra") or "").strip()

    if "temperature" in valores:
        try:
            t = float(valores.get("temperature"))
        except (TypeError, ValueError):
            raise ValueError("temperature precisa ser um número")
        if not (0 <= t <= 2):
            raise ValueError("temperature precisa estar entre 0 e 2")
        atual["temperature"] = t

    if "function_calling" in valores:
        atual["function_calling"] = bool(valores.get("function_calling"))

    if "ssqp_ativo" in valores:
        atual["ssqp_ativo"] = bool(valores.get("ssqp_ativo"))

    if "max_tokens" in valores:
        try:
            mt = int(valores.get("max_tokens") or 0)
        except (TypeError, ValueError):
            raise ValueError("max_tokens precisa ser um número inteiro")
        # convenção do próprio Ollama (options.num_predict), não é regra
        # nossa: -1 = geração infinita (só para no stop natural do
        # modelo), -2 = gera até encher a janela de contexto inteira.
        # 0 = não manda o campo, usa o padrão do Ollama/provedor (ver
        # llm.chat(): "if params.get('max_tokens')" só entra pra valor
        # != 0, e -1/-2 são truthy em Python, então já iam funcionar —
        # só faltava a validação aqui não bloquear negativo.
        if mt < -2:
            raise ValueError("max_tokens só aceita -2 (preencher contexto), -1 (infinito), 0 (padrão) ou um número positivo")
        atual["max_tokens"] = mt

    # 🖥️ carga do modelo local — "" ou null do formulário vira None (auto),
    # nunca 0 por engano (0 em num_gpu é um valor válido e diferente de
    # "automático": força tudo na CPU).
    if "num_gpu" in valores:
        bruto = valores.get("num_gpu")
        if bruto in (None, "", "auto"):
            atual["num_gpu"] = None
        else:
            try:
                ng = int(bruto)
            except (TypeError, ValueError):
                raise ValueError("num_gpu precisa ser um número inteiro (ou vazio p/ automático)")
            if ng < 0:
                raise ValueError("num_gpu não pode ser negativo")
            atual["num_gpu"] = ng

    if "num_ctx" in valores:
        bruto = valores.get("num_ctx")
        if bruto in (None, "", "auto"):
            atual["num_ctx"] = None
        else:
            try:
                nc = int(bruto)
            except (TypeError, ValueError):
                raise ValueError("num_ctx precisa ser um número inteiro (ou vazio p/ automático)")
            if nc <= 0:
                raise ValueError("num_ctx precisa ser maior que zero")
            atual["num_ctx"] = nc

    if "use_mmap" in valores:
        atual["use_mmap"] = bool(valores.get("use_mmap"))

    if "use_mlock" in valores:
        atual["use_mlock"] = bool(valores.get("use_mlock"))

    if "num_thread" in valores:
        bruto = valores.get("num_thread")
        if bruto in (None, "", "auto"):
            atual["num_thread"] = None
        else:
            try:
                nt = int(bruto)
            except (TypeError, ValueError):
                raise ValueError("num_thread precisa ser um número inteiro (ou vazio p/ automático)")
            if nt <= 0:
                raise ValueError("num_thread precisa ser maior que zero")
            atual["num_thread"] = nt

    if "pensar_modo" in valores:
        pm = (valores.get("pensar_modo") or "auto").strip().lower()
        if pm not in _PENSAR_MODOS_VALIDOS:
            raise ValueError(f"pensar_modo precisa ser um de: {', '.join(_PENSAR_MODOS_VALIDOS)}")
        atual["pensar_modo"] = pm

    if "motor_busca" in valores:
        mb = (valores.get("motor_busca") or "auto").strip().lower()
        if mb not in _MOTORES_BUSCA_VALIDOS:
            raise ValueError(f"motor_busca precisa ser um de: {', '.join(_MOTORES_BUSCA_VALIDOS)}")
        atual["motor_busca"] = mb

    # 🚀 MTP — "" ou null do formulário vira None (auto), igual num_gpu/
    # num_ctx/num_thread. 0 é um valor válido e diferente de None: força
    # DESLIGADO, em vez de deixar o Ollama/modelo decidir sozinho.
    if "mtp_draft_num_predict" in valores:
        bruto = valores.get("mtp_draft_num_predict")
        if bruto in (None, "", "auto"):
            atual["mtp_draft_num_predict"] = None
        else:
            try:
                mtp = int(bruto)
            except (TypeError, ValueError):
                raise ValueError("mtp_draft_num_predict precisa ser um número inteiro (0 = desligado, vazio = automático)")
            if not (0 <= mtp <= 8):
                raise ValueError("mtp_draft_num_predict precisa estar entre 0 (desligado) e 8")
            atual["mtp_draft_num_predict"] = mtp

    # 🎛️ Parâmetros avançados de inferência — None = usa default do Ollama
    if "num_batch" in valores:
        bruto = valores.get("num_batch")
        if bruto in (None, "", "auto"):
            atual["num_batch"] = None
        else:
            try:
                nb = int(bruto)
            except (TypeError, ValueError):
                raise ValueError("num_batch precisa ser um número inteiro (ou vazio p/ automático)")
            if nb <= 0:
                raise ValueError("num_batch precisa ser maior que zero")
            atual["num_batch"] = nb

    if "num_ubatch" in valores:
        bruto = valores.get("num_ubatch")
        if bruto in (None, "", "auto"):
            atual["num_ubatch"] = None
        else:
            try:
                nub = int(bruto)
            except (TypeError, ValueError):
                raise ValueError("num_ubatch precisa ser um número inteiro (ou vazio p/ automático)")
            if nub <= 0:
                raise ValueError("num_ubatch precisa ser maior que zero")
            atual["num_ubatch"] = nub

    if "seed" in valores:
        bruto = valores.get("seed")
        if bruto in (None, "", "auto"):
            atual["seed"] = None
        else:
            try:
                atual["seed"] = int(bruto)
            except (TypeError, ValueError):
                raise ValueError("seed precisa ser um número inteiro (ou vazio p/ aleatório)")

    if "top_k" in valores:
        bruto = valores.get("top_k")
        if bruto in (None, "", "auto"):
            atual["top_k"] = None
        else:
            try:
                tk = int(bruto)
            except (TypeError, ValueError):
                raise ValueError("top_k precisa ser um número inteiro (ou vazio p/ padrão)")
            if tk <= 0:
                raise ValueError("top_k precisa ser maior que zero")
            atual["top_k"] = tk

    if "top_p" in valores:
        bruto = valores.get("top_p")
        if bruto in (None, "", "auto"):
            atual["top_p"] = None
        else:
            try:
                tp = float(bruto)
            except (TypeError, ValueError):
                raise ValueError("top_p precisa ser um número (ou vazio p/ padrão)")
            if not (0 < tp <= 1):
                raise ValueError("top_p precisa estar entre 0 (exclusivo) e 1 (inclusivo)")
            atual["top_p"] = tp

    if "repeat_penalty" in valores:
        bruto = valores.get("repeat_penalty")
        if bruto in (None, "", "auto"):
            atual["repeat_penalty"] = None
        else:
            try:
                rp = float(bruto)
            except (TypeError, ValueError):
                raise ValueError("repeat_penalty precisa ser um número (ou vazio p/ padrão)")
            if rp <= 0:
                raise ValueError("repeat_penalty precisa ser maior que zero")
            atual["repeat_penalty"] = rp

    if "num_keep" in valores:
        bruto = valores.get("num_keep")
        if bruto in (None, "", "auto"):
            atual["num_keep"] = None
        else:
            try:
                nk = int(bruto)
            except (TypeError, ValueError):
                raise ValueError("num_keep precisa ser um número inteiro (ou vazio p/ padrão)")
            if nk < 0:
                raise ValueError("num_keep não pode ser negativo")
            atual["num_keep"] = nk

    if "mirostat" in valores:
        bruto = valores.get("mirostat")
        if bruto in (None, "", "auto"):
            atual["mirostat"] = None
        else:
            try:
                ms = int(bruto)
            except (TypeError, ValueError):
                raise ValueError("mirostat precisa ser 0 (desligado), 1 ou 2")
            if ms not in (0, 1, 2):
                raise ValueError("mirostat só aceita 0 (desligado), 1 ou 2")
            atual["mirostat"] = ms

    if "mirostat_tau" in valores:
        bruto = valores.get("mirostat_tau")
        if bruto in (None, "", "auto"):
            atual["mirostat_tau"] = None
        else:
            try:
                mt = float(bruto)
            except (TypeError, ValueError):
                raise ValueError("mirostat_tau precisa ser um número")
            if mt <= 0:
                raise ValueError("mirostat_tau precisa ser positivo")
            atual["mirostat_tau"] = mt

    if "mirostat_eta" in valores:
        bruto = valores.get("mirostat_eta")
        if bruto in (None, "", "auto"):
            atual["mirostat_eta"] = None
        else:
            try:
                me = float(bruto)
            except (TypeError, ValueError):
                raise ValueError("mirostat_eta precisa ser um número")
            if me <= 0:
                raise ValueError("mirostat_eta precisa ser positivo")
            atual["mirostat_eta"] = me

    # 🧊 Otimizações do KoboldCpp
    if "quant_kv_cache" in valores:
        bruto = valores.get("quant_kv_cache")
        if bruto in (None, "", "auto"):
            atual["quant_kv_cache"] = None
        else:
            # Sistema flexível: aceita qualquer string de quantização
            # Se for um valor conhecido, valida; se não, permite (para novas tecnologias)
            bruto_str = str(bruto).lower().strip()
            if bruto_str in _QUANT_KV_VALIDOS:
                atual["quant_kv_cache"] = bruto_str
            else:
                # Aceita valor desconhecido (para suportar novas tecnologias)
                # Avisa mas não bloqueia
                atual["quant_kv_cache"] = bruto_str

    if "flash_attention" in valores:
        atual["flash_attention"] = bool(valores.get("flash_attention"))

    if "batch_size" in valores:
        bruto = valores.get("batch_size")
        if bruto in (None, "", "auto"):
            atual["batch_size"] = None
        else:
            try:
                bs = int(bruto)
            except (TypeError, ValueError):
                raise ValueError("batch_size precisa ser um número inteiro (ou vazio p/ automático)")
            if bs <= 0:
                raise ValueError("batch_size precisa ser maior que zero")
            atual["batch_size"] = bs

    if "low_vram" in valores:
        atual["low_vram"] = bool(valores.get("low_vram"))

    if "gpu_backend_autofit" in valores:
        atual["gpu_backend_autofit"] = bool(valores.get("gpu_backend_autofit"))

    if "modo_raw" in valores:
        atual["modo_raw"] = bool(valores.get("modo_raw"))

    if modelo:
        secs = _ler_configs()
        secs[_chave_modelo(modelo)] = atual
        _salvar_configs(secs)
    else:
        _salvar(atual)
    return atual
