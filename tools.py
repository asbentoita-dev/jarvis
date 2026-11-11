import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import requests

# 🧹 Monitoramento de espaço em disco — evita falhas silenciosas quando
# o HD/encher. Se faltar menos de 500 MB livres no workspace, avisa
# o usuário antes de tentar qualquer operação de escrita.
import psutil

DISCO_MINIMO_MB = 500


def _espaco_suficiente():
    """Verifica se há espaço livre suficiente no workspace.
    Retorna True se OK, False se criticamente baixo."""
    try:
        usage = psutil.disk_usage(WORKSPACE)
        livre_mb = usage.free / (1024 * 1024)
        return livre_mb >= DISCO_MINIMO_MB
    except Exception:
        return True  # em caso de erro, assume que tá ok

# 🪟 Windows: todo subprocess.run() que abre um interpretador/console
# (python.exe, cmd.exe via shell=True) pisca uma janela de terminal preta
# na tela por uma fração de segundo, mesmo com capture_output=True — o
# CREATE_NO_WINDOW só existe no Windows e evita isso. Bug relatado em
# 22/08/2026: "apareceram 3 terminais rapidamente na tela" toda vez que
# o Jarvis chamava rodar_python/rodar_comando dentro de um loop de
# ferramentas (ex: criar_arquivo -> rodar_python -> rodar_python de novo
# pra corrigir). Sem efeito nenhum em Linux/Mac (getattr cai pra 0).
_SEM_JANELA = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

import config
from config import (WORKSPACE, SEARX_URL, SD_API_URL,
                    SUPABASE_URL, SUPABASE_KEY, VERCEL_TOKEN,
                    ANTHROPIC_API_KEY, NUVEM_MODELO, TAVILY_API_KEY,
                    BRAVE_SEARCH_KEY, YANDEX_SEARCH_KEY)
import pastas_config
import desempenho
import desktop_agent
import model_params
import sandbox_tools


# 📂 liberações temporárias: valem SÓ até fechar o Jarvis
_PASTAS_SESSAO = []


def _bases_permitidas():
    # pastas.json é lido a cada chamada (permite adicionar pela interface
    # web sem precisar reiniciar) — lista pequena, custo desprezível
    return [WORKSPACE] + pastas_config.listar() + _PASTAS_SESSAO


def _resolver_caminho(caminho):
    """Nome simples → workspace. Caminho completo → só se estiver na
    pasta padrão (PASTAS_PERMITIDAS, árvore toda) ou liberado na sessão."""
    caminho = str(caminho).strip().strip('"')

    if os.path.isabs(caminho):
        real = os.path.abspath(caminho).lower()
        for base in _bases_permitidas():
            if real.startswith(os.path.abspath(base).lower()):
                return os.path.abspath(caminho)
        return None

    # caminho relativo: procura no workspace E nas pastas permitidas
    for base in _bases_permitidas():
        candidato = os.path.join(base, caminho)
        if os.path.exists(candidato):
            return candidato

    return os.path.join(WORKSPACE, caminho)


def liberar_pasta(caminho):
    """Libera uma pasta extra para leitura — só nesta sessão."""
    caminho = str(caminho).strip().strip('"')

    if not os.path.isabs(caminho):
        return "ERRO: informe o caminho completo, ex: C:\\Users\\Casa\\Documents"
    if not os.path.isdir(caminho):
        return f"ERRO: pasta nao existe: {caminho}"

    real = os.path.abspath(caminho)

    if any(real.lower().startswith(os.path.abspath(b).lower())
           for b in _bases_permitidas()):
        return f"Pasta ja liberada: {real}"

    _PASTAS_SESSAO.append(real)
    return (f"Pasta liberada ate o Jarvis fechar: {real} "
            "(inclui toda a arvore de subpastas).")
import memory


def _resumir(texto, limite=2000):
    """Corta respostas longas para não estourar o contexto do modelo."""
    texto = str(texto)
    if len(texto) > limite:
        return texto[:limite] + f"\n...(cortado, {len(texto)} caracteres no total)"
    return texto

os.makedirs(WORKSPACE, exist_ok=True)


# ─────────────────────────────────────────
# 🧠 IMPLEMENTAÇÃO DAS FERRAMENTAS
# ─────────────────────────────────────────

def criar_arquivo(nome, conteudo):
    # Verifica espaço em disco antes de escrever
    if not _espaco_suficiente():
        usage = psutil.disk_usage(WORKSPACE)
        livre_mb = usage.free / (1024 * 1024)
        return f"ERRO: Espaço em disco insuficiente. Livre: {livre_mb:.0f} MB. Limite mínimo: {DISCO_MINIMO_MB} MB."

    caminho = os.path.join(WORKSPACE, nome)
    os.makedirs(os.path.dirname(caminho), exist_ok=True)

    conteudo = _corrigir_escapes_literais(conteudo)

    with open(caminho, "w", encoding="utf-8") as f:
        f.write(conteudo)

    return f"Arquivo criado: {caminho}"


def _corrigir_escapes_literais(conteudo):
    """Alguns modelos locais (principalmente quantizados/menores), ao montar
    o JSON da chamada de função pra escrever código com várias linhas,
    escapam errado — o campo "conteudo" chega aqui com "\\n"/"\\t" LITERAIS
    (backslash + letra, 2 caracteres) em vez de quebra de linha/tab de
    verdade. Resultado: o arquivo escrito vira uma linha só, e ao tentar
    rodar dá SyntaxError ("unexpected character after line continuation
    character") — bug flagrado em 22/08/2026 testando qwen3/qwen3.5 via
    Jarvis (o terminal Ollama cru, sem essa ferramenta, nunca produz esse
    problema, porque nunca escreve arquivo).
    Heurística: se não tem NENHUMA quebra de linha real mas tem \\n literal
    sobrando, é quase certeza que foi um escape mal feito do modelo — troca.
    Se o conteúdo já tem quebra de linha real, não mexe (não quero estragar
    um arquivo que legitimamente contém a sequência \\n em texto, tipo um
    exemplo de regex)."""
    if not isinstance(conteudo, str):
        return conteudo
    tem_quebra_real = "\n" in conteudo
    tem_escape_literal = "\\n" in conteudo
    if not tem_quebra_real and tem_escape_literal:
        conteudo = (conteudo
                    .replace("\\r\\n", "\n")
                    .replace("\\n", "\n")
                    .replace("\\t", "\t"))
    return conteudo


def mover_arquivo(origem, destino):
    """Move (ou renomeia) um arquivo. Origem precisa existir dentro do
    workspace ou de uma pasta permitida/liberada. Destino pode ser um nome
    simples (vai para o workspace), uma pasta existente (mantem o nome
    original) ou um caminho completo com nome novo — nos tres casos, se for
    caminho absoluto, precisa estar dentro das pastas permitidas (chame
    liberar_pasta antes se for uma pasta nova pedida pelo usuario)."""
    caminho_origem = _resolver_caminho(origem)
    if caminho_origem is None or not os.path.exists(caminho_origem):
        return (f"ERRO: arquivo de origem nao encontrado ou fora das pastas "
                f"permitidas: {origem}")

    destino = str(destino).strip().strip('"')

    if os.path.isabs(destino):
        destino_real = os.path.abspath(destino)
        permitido = any(
            destino_real.lower().startswith(os.path.abspath(b).lower())
            for b in _bases_permitidas()
        )
        if not permitido:
            return (f"ERRO: destino fora das pastas permitidas: {destino}. "
                    "Se o usuario pediu essa pasta, chame liberar_pasta com "
                    "ela primeiro.")
        caminho_destino = destino_real
    else:
        caminho_destino = os.path.join(WORKSPACE, destino)

    # destino é uma pasta existente -> mantem o nome original do arquivo
    if os.path.isdir(caminho_destino):
        caminho_destino = os.path.join(caminho_destino, os.path.basename(caminho_origem))

    if os.path.abspath(caminho_destino) == os.path.abspath(caminho_origem):
        return f"ERRO: origem e destino sao o mesmo arquivo: {caminho_origem}"

    os.makedirs(os.path.dirname(caminho_destino), exist_ok=True)

    if os.path.exists(caminho_destino):
        return (f"ERRO: ja existe um arquivo em {caminho_destino}. "
                "Escolha outro nome ou apague o existente primeiro.")

    try:
        shutil.move(caminho_origem, caminho_destino)
    except Exception as e:
        return f"ERRO ao mover: {e}"

    return f"Arquivo movido: {caminho_origem} -> {caminho_destino}"


def ler_arquivo(nome):
    caminho = _resolver_caminho(nome)

    if caminho is None:
        return (f"ERRO: '{nome}' esta fora das pastas permitidas. "
                "Se o usuario pediu este arquivo, chame liberar_pasta "
                "com a pasta dele primeiro.")
    if not os.path.exists(caminho):
        return f"ERRO: arquivo nao existe: {nome}"

    ext = os.path.splitext(caminho)[1].lower()

    # 📄 PDF
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return "ERRO: para ler PDF, instale: pip install pypdf"
        try:
            leitor = PdfReader(caminho)
            texto = "\n".join((p.extract_text() or "") for p in leitor.pages[:20])
            return _resumir(texto, 4000) or "(PDF sem texto extraivel)"
        except Exception as e:
            return f"ERRO ao ler PDF: {e}"

    # 📄 Word
    if ext == ".docx":
        try:
            import docx
        except ImportError:
            return "ERRO: para ler Word, instale: pip install python-docx"
        try:
            d = docx.Document(caminho)
            return _resumir("\n".join(p.text for p in d.paragraphs), 4000)
        except Exception as e:
            return f"ERRO ao ler Word: {e}"

    # 📊 Excel
    if ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            return "ERRO: para ler Excel, instale: pip install openpyxl"
        try:
            wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
            partes = []
            for aba in wb.sheetnames[:3]:
                ws = wb[aba]
                linhas = []
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i >= 20:
                        linhas.append("...(mais linhas)")
                        break
                    linhas.append(" | ".join("" if c is None else str(c)
                                             for c in row))
                partes.append(f"[Aba: {aba}]\n" + "\n".join(linhas))
            return _resumir("\n\n".join(partes), 4000)
        except Exception as e:
            return f"ERRO ao ler Excel: {e}"

    # 🖼 imagem: só metadados (para "enxergar" é preciso cérebro de visão)
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
        try:
            from PIL import Image
            with Image.open(caminho) as im:
                return (f"[Imagem {ext}] {im.width}x{im.height} pixels, "
                        f"modo {im.mode}, "
                        f"{os.path.getsize(caminho)//1024} KB. "
                        "(nao consigo ver o conteudo; posso apenas descrever os metadados)")
        except Exception:
            return f"[Imagem {ext}] {os.path.getsize(caminho)//1024} KB."

    # 📦 binários conhecidos: só identifica
    if ext in (".exe", ".dll", ".zip", ".rar", ".7z", ".mp3", ".mp4",
               ".avi", ".safetensors", ".gguf", ".pyc", ".db"):
        return (f"[Arquivo binario {ext}] {os.path.getsize(caminho)//1024} KB. "
                "Nao e legivel como texto.")

    # 📄 texto (txt, py, md, json, csv...)
    try:
        with open(caminho, "r", encoding="utf-8", errors="replace") as f:
            return _resumir(f.read(), 4000)
    except Exception as e:
        return f"ERRO ao ler: {e}"


def listar_arquivos(pasta=""):
    base = _resolver_caminho(pasta) if pasta else WORKSPACE

    if base is None:
        return (f"ERRO: '{pasta}' esta fora das pastas permitidas. "
                "Se o usuario pediu esta pasta, chame liberar_pasta primeiro.")
    if not os.path.isdir(base):
        return f"ERRO: pasta nao existe: {pasta}"

    itens = []
    for raiz, _, arquivos in os.walk(base):
        for a in arquivos:
            caminho = os.path.join(raiz, a)
            try:
                kb = os.path.getsize(caminho) // 1024
            except OSError:
                kb = 0
            itens.append(f"{os.path.relpath(caminho, base)} ({kb} KB)")
            if len(itens) >= 200:
                itens.append("...(lista cortada em 200 itens)")
                return "\n".join(itens)

    return "\n".join(itens) if itens else "(pasta vazia)"


def analisar_pasta(pasta=""):
    """Inventário real de uma pasta: cada arquivo com tipo, tamanho e
    uma amostra verdadeira do conteúdo (nada é inventado)."""
    base = _resolver_caminho(pasta) if pasta else WORKSPACE

    if base is None:
        return (f"ERRO: '{pasta}' esta fora das pastas permitidas. "
                "Se o usuario pediu esta pasta, chame liberar_pasta primeiro.")
    if not os.path.isdir(base):
        return f"ERRO: pasta nao existe: {pasta}"

    fichas = []
    total = 0

    for raiz, dirs, arquivos in os.walk(base):
        # pula pastas de sistema/cache
        dirs[:] = [d for d in dirs if d not in
                   ("__pycache__", "venv", "node_modules", ".git", ".cache")]

        for a in sorted(arquivos):
            if total >= 25:
                fichas.append("...(analise limitada aos primeiros 25 arquivos; "
                              "peça pastas específicas para ver o resto)")
                return "\n\n".join(fichas)

            caminho = os.path.join(raiz, a)
            rel = os.path.relpath(caminho, base)
            amostra = ler_arquivo(caminho)

            # amostra curta para caber muitos arquivos no contexto
            amostra = str(amostra)[:400].replace("\n", " ")
            fichas.append(f"── {rel} ──\n{amostra}")
            total += 1

    return "\n\n".join(fichas) if fichas else "(pasta vazia)"


def _localizar_script(arquivo):
    """Acha o caminho real de um script .py a partir só do nome/caminho
    parcial que o usuário deu. Ordem de busca:
    1. Caminho absoluto, se já existir.
    2. Direto dentro do WORKSPACE.
    3. Busca recursiva dentro do WORKSPACE e de cada pasta em
       PASTAS_PERMITIDAS (config.py) — cobre projetos como o
       Claude_Auto que ficam fora do workspace padrão do Jarvis."""
    if os.path.isabs(arquivo) and os.path.exists(arquivo):
        return arquivo

    direto = os.path.join(WORKSPACE, arquivo)
    if os.path.exists(direto):
        return direto

    nome_alvo = os.path.basename(arquivo)
    try:
        extras = pastas_config.listar()
    except Exception:
        extras = []
    for base in [WORKSPACE] + list(extras):
        if not os.path.isdir(base):
            continue
        for raiz, _dirs, arquivos in os.walk(base):
            if nome_alvo in arquivos:
                return os.path.join(raiz, nome_alvo)

    return None


def rodar_python(arquivo, argumentos=""):
    caminho = _localizar_script(arquivo)

    if not caminho:
        return f"ERRO: arquivo nao encontrado: {arquivo} (procurado no workspace e nas pastas permitidas)"

    comando = ["python", caminho]
    if argumentos:
        comando += shlex.split(argumentos, posix=False)

    try:
        result = subprocess.run(
            comando,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=os.path.dirname(caminho),
            creationflags=_SEM_JANELA
        )
    except subprocess.TimeoutExpired:
        return "ERRO: o script demorou demais e foi interrompido (timeout de 120s)."

    return (result.stdout or "") + (result.stderr or "") or "(sem saida)"


def rodar_comando(comando):
    result = subprocess.run(
        comando,
        shell=True,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=WORKSPACE,
        creationflags=_SEM_JANELA
    )

    return (result.stdout or "") + (result.stderr or "") or "(sem saida)"


def _busca_tavily(consulta):
    """Tavily: busca de alta qualidade para IAs (se tiver chave no .env)."""
    if not TAVILY_API_KEY:
        return None
    try:
        r = requests.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {TAVILY_API_KEY}",
                     "Content-Type": "application/json"},
            json={"query": consulta, "max_results": 5,
                  "include_answer": True},
            timeout=15
        )
        if r.status_code == 200:
            dados = r.json()
            partes = []

            if dados.get("answer"):
                partes.append(f"Resumo: {dados['answer']}")

            for x in dados.get("results", []):
                partes.append(f"{x.get('title')}\n{x.get('url')}\n"
                              f"{(x.get('content') or '')[:300]}")

            if partes:
                return "\n\n".join(partes)
    except Exception:
        pass
    return None


def _busca_searx(consulta):
    """SearxNG local."""
    try:
        r = requests.get(
            SEARX_URL,
            params={"q": consulta, "format": "json"},
            timeout=5
        )
        dados = r.json()
        resultados = dados.get("results", [])[:5]

        if resultados:
            return "\n\n".join(
                f"{x.get('title')}\n{x.get('url')}\n{x.get('content', '')}"
                for x in resultados
            )
    except Exception:
        pass
    return None


def _busca_brave(consulta):
    """Brave Search — índice próprio, sem Google/Bing."""
    if not BRAVE_SEARCH_KEY:
        return None
    try:
        r = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": consulta, "count": 5, "search_lang": "pt"},
            headers={"Accept": "application/json",
                     "X-Subscription-Token": BRAVE_SEARCH_KEY},
            timeout=10
        )
        if r.status_code == 200:
            resultados = r.json().get("web", {}).get("results", [])[:5]
            if resultados:
                return "\n\n".join(
                    f"{x.get('title')}\n{x.get('url')}\n{x.get('description','')}"
                    for x in resultados
                )
    except Exception:
        pass
    return None


def _busca_yandex(consulta):
    """Yandex XML API — motor russo independente."""
    if not YANDEX_SEARCH_KEY:
        return None
    try:
        import xml.etree.ElementTree as ET
        r = requests.get(
            "https://yandex.com/search/xml",
            params={"apikey": YANDEX_SEARCH_KEY, "query": consulta,
                    "l10n": "pt", "page": 0},
            timeout=10
        )
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            partes = []
            for doc in root.findall(".//doc")[:5]:
                title   = doc.findtext("title") or ""
                url     = doc.findtext("url") or ""
                passage = doc.findtext(".//passage") or ""
                partes.append(f"{title}\n{url}\n{passage}")
            if partes:
                return "\n\n".join(partes)
    except Exception:
        pass
    return None


def _busca_ddg_html(consulta):
    """DuckDuckGo HTML (funciona para noticias e buscas gerais)."""
    try:
        r = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": consulta},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        achados = re.findall(
            r'class="result__a"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</',
            r.text, re.S
        )
        if achados:
            def limpar(s):
                return re.sub(r"<[^>]+>", "", s).strip()
            return "\n\n".join(
                f"{limpar(t)}\n{limpar(sn)}" for t, sn in achados[:5]
            )
    except Exception:
        pass
    return None


def _busca_ddg_instant(consulta):
    """DuckDuckGo instant answers — último fallback, sempre responde algo."""
    try:
        r = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": consulta, "format": "json", "no_html": 1},
            timeout=10
        )
        dados = r.json()

        partes = []
        if dados.get("AbstractText"):
            partes.append(dados["AbstractText"])

        for t in dados.get("RelatedTopics", [])[:5]:
            if isinstance(t, dict) and t.get("Text"):
                partes.append(t["Text"])

        return "\n\n".join(partes) if partes else "(nenhum resultado)"
    except Exception as e:
        return f"ERRO na busca: {e}"


# Ordem padrão da cascata automática (0 = maior prioridade). SearxNG e os
# dois fallbacks de DuckDuckGo não são escolhíveis manualmente (não têm
# chave/API própria pra "preferir") — só entram como rede de segurança.
_MOTORES_BUSCA_CASCATA = [
    ("tavily", _busca_tavily),
    ("searx",  _busca_searx),
    ("brave",  _busca_brave),
    ("yandex", _busca_yandex),
]


def busca_web(consulta):
    cabecalho = f"[Termos buscados: \"{consulta}\"]\n\n"

    # 🔎 motor_busca (model_params.json): "auto" mantém a ordem de sempre;
    # escolher um motor específico só muda a ORDEM (tenta esse primeiro),
    # nunca restringe — se ele falhar, cai pro resto da cascata igual
    # antes, pra nunca devolver busca vazia só por preferência manual.
    try:
        preferido = model_params.obter().get("motor_busca", "auto")
    except Exception:
        preferido = "auto"

    cascata = list(_MOTORES_BUSCA_CASCATA)
    if preferido != "auto":
        cascata.sort(key=lambda par: 0 if par[0] == preferido else 1)

    for _nome, funcao in cascata:
        resultado = funcao(consulta)
        if resultado:
            return cabecalho + resultado

    resultado = _busca_ddg_html(consulta)
    if resultado:
        return cabecalho + resultado

    return _busca_ddg_instant(consulta)


# ─────────────────────────────────────────
# ☁️ CONSULTORIA NA NUVEM (Claude)
# ─────────────────────────────────────────

def consultar_nuvem(pergunta, contexto=""):
    """Consulta um modelo de nuvem muito mais capaz, para tarefas difíceis."""
    if not ANTHROPIC_API_KEY:
        return ("ERRO: preencha ANTHROPIC_API_KEY no arquivo .env "
                "(crie a chave em console.anthropic.com > API Keys).")

    conteudo = pergunta
    if contexto:
        conteudo = f"Contexto:\n{contexto}\n\nTarefa:\n{pergunta}"

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": NUVEM_MODELO,
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": conteudo}]
            },
            timeout=120
        )

        if r.status_code != 200:
            return f"ERRO na nuvem: HTTP {r.status_code}\n{_resumir(r.text, 500)}"

        blocos = r.json().get("content", [])
        texto = "".join(b.get("text", "") for b in blocos)
        return texto or "(resposta vazia da nuvem)"

    except Exception as e:
        return f"ERRO na consultoria: {e}"


# ─────────────────────────────────────────
# 🌐 INTEGRAÇÕES EXTERNAS (APIs)
# ─────────────────────────────────────────

def chamar_api(metodo, url, corpo=None, cabecalhos=None):
    """Ferramenta genérica: qualquer API REST."""
    try:
        if isinstance(corpo, str) and corpo.strip():
            try:
                corpo = json.loads(corpo)
            except Exception:
                pass
        if isinstance(cabecalhos, str) and cabecalhos.strip():
            try:
                cabecalhos = json.loads(cabecalhos)
            except Exception:
                cabecalhos = None

        r = requests.request(
            metodo.upper(),
            url,
            json=corpo if isinstance(corpo, (dict, list)) else None,
            data=corpo if isinstance(corpo, str) else None,
            headers=cabecalhos or {},
            timeout=30
        )
        return f"HTTP {r.status_code}\n{_resumir(r.text)}"
    except Exception as e:
        return f"ERRO na chamada: {e}"


def _sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }


def supabase_consultar(tabela, filtro="", limite=20):
    if not (SUPABASE_URL and SUPABASE_KEY):
        return "ERRO: preencha SUPABASE_URL e SUPABASE_KEY no arquivo .env"

    url = f"{SUPABASE_URL}/rest/v1/{tabela}?limit={limite}"
    if filtro:
        url += "&" + filtro.lstrip("&?")

    try:
        r = requests.get(url, headers=_sb_headers(), timeout=30)
        return f"HTTP {r.status_code}\n{_resumir(r.text)}"
    except Exception as e:
        return f"ERRO no Supabase: {e}"


def supabase_inserir(tabela, dados):
    if not (SUPABASE_URL and SUPABASE_KEY):
        return "ERRO: preencha SUPABASE_URL e SUPABASE_KEY no arquivo .env"

    if isinstance(dados, str):
        try:
            dados = json.loads(dados)
        except Exception:
            return "ERRO: 'dados' precisa ser JSON valido, ex: {\"nome\": \"x\"}"

    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{tabela}",
            headers={**_sb_headers(), "Prefer": "return=representation"},
            json=dados,
            timeout=30
        )
        return f"HTTP {r.status_code}\n{_resumir(r.text)}"
    except Exception as e:
        return f"ERRO no Supabase: {e}"


def _vc_headers():
    return {"Authorization": f"Bearer {VERCEL_TOKEN}"}


def vercel_projetos():
    if not VERCEL_TOKEN:
        return "ERRO: preencha VERCEL_TOKEN no arquivo .env"

    try:
        r = requests.get("https://api.vercel.com/v9/projects",
                         headers=_vc_headers(), timeout=30)
        if r.status_code != 200:
            return f"HTTP {r.status_code}\n{_resumir(r.text)}"

        projetos = r.json().get("projects", [])
        if not projetos:
            return "Nenhum projeto encontrado."

        linhas = []
        for p in projetos:
            linhas.append(f"- {p.get('name')} (framework: {p.get('framework')})")
        return "Projetos na Vercel:\n" + "\n".join(linhas)
    except Exception as e:
        return f"ERRO na Vercel: {e}"


def vercel_deploys(projeto=""):
    if not VERCEL_TOKEN:
        return "ERRO: preencha VERCEL_TOKEN no arquivo .env"

    url = "https://api.vercel.com/v6/deployments?limit=10"
    if projeto:
        url += f"&app={projeto}"

    try:
        r = requests.get(url, headers=_vc_headers(), timeout=30)
        if r.status_code != 200:
            return f"HTTP {r.status_code}\n{_resumir(r.text)}"

        deploys = r.json().get("deployments", [])
        if not deploys:
            return "Nenhum deploy encontrado."

        linhas = []
        for d in deploys:
            linhas.append(f"- {d.get('name')} | {d.get('state')} | "
                          f"https://{d.get('url')}")
        return "Últimos deploys:\n" + "\n".join(linhas)
    except Exception as e:
        return f"ERRO na Vercel: {e}"


def gerar_imagem(descricao, nome_arquivo=None):
    """Gera imagem via sd-api (Stable Diffusion 3.5) e salva no workspace."""
    if not nome_arquivo:
        nome_arquivo = f"imagem_{int(time.time())}.png"
    if not nome_arquivo.lower().endswith(".png"):
        nome_arquivo += ".png"

    def _gerar():
        r = requests.post(
            f"{SD_API_URL}/v1/images/generations",
            json={"prompt": descricao, "size": "512x512"},
            timeout=600  # a primeira geração carrega o modelo (demora)
        )
        r.raise_for_status()
        return r.json()["data"][0]["b64_json"]

    try:
        b64 = _gerar()

    except requests.exceptions.ConnectionError:
        # SD-API pode estar pausado de propósito (modo desempenho, ver
        # desempenho.py) porque o modelo local em uso já ocupa a GPU.
        # Em vez de devolver erro pedindo pro usuário mexer em .bat na
        # mão, sobe o serviço agora e espera ele ficar pronto.
        if not desempenho.iniciar_sd_api_se_preciso():
            return ("ERRO: não consegui iniciar o serviço de geração de "
                    "imagens automaticamente. Avise o usuário.")

        if not desempenho.esperar_sd_api_pronto(timeout_s=90):
            return ("ERRO: o serviço de geração de imagens está subindo "
                    "mas ainda não respondeu (pode levar ~1 min na "
                    "primeira vez, principalmente se estiver carregando "
                    "o modelo). Peça ao usuário para tentar de novo em "
                    "instantes.")

        try:
            b64 = _gerar()
        except Exception as e:
            return f"ERRO ao gerar imagem (depois de iniciar o serviço): {e}"

    except Exception as e:
        return f"ERRO ao gerar imagem: {e}"

    caminho = os.path.join(WORKSPACE, nome_arquivo)
    with open(caminho, "wb") as f:
        f.write(base64.b64decode(b64))

    return f"IMAGEM_GERADA: {nome_arquivo} (salva em {caminho})"


def gerar_video(descricao, nome_arquivo=None, num_frames=16, fps=8):
    """Gera um clipe curto (AnimateDiff, via sd-api) e salva no workspace.

    IMPORTANTE sobre tempo: isso pode levar de 5 a 10 minutos numa GPU de
    8GB (bem mais lento que gerar imagem) — por isso o timeout desta
    chamada é bem maior que o padrão. Isso NÃO tem relação com o
    TIMEOUT_RESPOSTA (120s) do chat: aquele controla quanto tempo o
    Jarvis espera o MODELO responder; este aqui é só a chamada de rede
    desta ferramenta específica pro sd-api, independente."""
    if not nome_arquivo:
        nome_arquivo = f"video_{int(time.time())}.mp4"
    if not nome_arquivo.lower().endswith(".mp4"):
        nome_arquivo += ".mp4"

    def _gerar():
        r = requests.post(
            f"{SD_API_URL}/v1/video/generations",
            json={
                "prompt": descricao,
                "num_frames": num_frames,
                "fps": fps,
            },
            timeout=900  # geração de vídeo é bem mais lenta que imagem
        )
        r.raise_for_status()
        return r.json()["data"][0]["b64_json"]

    try:
        b64 = _gerar()

    except requests.exceptions.ConnectionError:
        # mesmo esquema do gerar_imagem: sd-api pode estar pausado
        # (modo desempenho) porque um modelo local grande está ocupando
        # a GPU no momento.
        if not desempenho.iniciar_sd_api_se_preciso():
            return ("ERRO: não consegui iniciar o serviço de geração de "
                    "vídeo automaticamente. Avise o usuário.")

        if not desempenho.esperar_sd_api_pronto(timeout_s=90):
            return ("ERRO: o serviço de geração de vídeo está subindo "
                    "mas ainda não respondeu (pode levar ~1 min na "
                    "primeira vez). Peça ao usuário para tentar de novo "
                    "em instantes.")

        try:
            b64 = _gerar()
        except Exception as e:
            return f"ERRO ao gerar vídeo (depois de iniciar o serviço): {e}"

    except Exception as e:
        return f"ERRO ao gerar vídeo: {e}"

    caminho = os.path.join(WORKSPACE, nome_arquivo)
    with open(caminho, "wb") as f:
        f.write(base64.b64decode(b64))

    return f"VIDEO_GERADO: {nome_arquivo} (salvo em {caminho})"


def salvar_memoria(texto, categoria="general"):
    ok = memory.salvar_memoria(texto, categoria)
    return "Memoria salva." if ok else "ERRO: ChromaDB indisponivel."


def buscar_memoria(consulta):
    memorias = memory.buscar_memoria(consulta)
    return "\n".join(memorias) if memorias else "(nenhuma memoria encontrada)"


def apagar_memoria(consulta):
    return memory.apagar_memorias(consulta)


def listar_memorias():
    return memory.listar_todas()


# ─────────────────────────────────────────
# 📚 DOCUMENTOS GRANDES (livros/ebooks) — tradução/processamento em blocos
#
# Um livro de 100+ páginas não cabe de uma vez no contexto do modelo (e
# mesmo cabendo, uma chamada de horas prenderia o chat inteiro). A saída
# é dividir em blocos pequenos (N "páginas" por vez) e processar cada
# bloco com uma chamada separada ao modelo LOCAL ativo, GRAVANDO O
# RESULTADO NO DISCO A CADA BLOCO — se travar ou for interrompido no
# meio, o que já foi feito não se perde.
#
# Roda em thread separada (não trava a resposta do chat esperando o
# livro inteiro) — por isso existe uma ferramenta companheira,
# status_documento, pra acompanhar o progresso.
# ─────────────────────────────────────────

import threading
import llm

_JOBS_DOCUMENTO = {}  # job_id -> {"status", "bloco_atual", "total_blocos", "arquivo_saida", "erro"}


def _extrair_paginas(caminho):
    """Lista de strings, uma por 'página'. PDF: página real do arquivo
    (via pypdf). DOCX/TXT/outros: sem conceito nativo de página no
    arquivo, então fatia em pedaços de ~1800 caracteres (tamanho
    aproximado de uma página A5) só pra ter uma unidade de bloco."""
    ext = os.path.splitext(caminho)[1].lower()

    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise RuntimeError("para ler PDF, instale: pip install pypdf")
        leitor = PdfReader(caminho)
        return [p.extract_text() or "" for p in leitor.pages]

    if ext == ".docx":
        try:
            import docx
        except ImportError:
            raise RuntimeError("para ler Word, instale: pip install python-docx")
        d = docx.Document(caminho)
        texto_completo = "\n".join(p.text for p in d.paragraphs)
    else:
        with open(caminho, "r", encoding="utf-8", errors="replace") as f:
            texto_completo = f.read()

    tam = 1800
    paginas = [texto_completo[i:i + tam] for i in range(0, len(texto_completo), tam)]
    return paginas or [""]


# 🌐 Regras de qualidade de tradução — extraídas do tradutor_livro.py
# (projeto separado em Claude_Auto/, testado e aprovado antes deste). São
# DOIS níveis de prompt, igual lá:
# 1) _REGRAS_QUALIDADE_TRADUCAO: prompt principal, completo, anexado à
#    instrução do usuário — regras de formatação/naturalidade/termos.
# 2) _traduzir_com_fallback(): se o bloco inteiro falhar (timeout, erro
#    de rede, resposta vazia), cai pro prompt de emergência — mais curto,
#    aplicado parágrafo por parágrafo (bloco menor = menos chance de
#    travar de novo, exatamente como no tradutor_livro.py original).
# Temperature 0.3 / top_p 0.9 em ambos os níveis (valor testado lá) —
# mais baixo que a temperature "de conversa" do usuário, porque tradução
# se beneficia de saída mais consistente/literal, não criativa.
_REGRAS_QUALIDADE_TRADUCAO = (
    "Regras de qualidade para esta tradução:\n"
    "- Mantenha a formatação original, a estrutura de parágrafos e "
    "caracteres especiais.\n"
    "- Seja preciso mas natural — evite tradução literal palavra por "
    "palavra.\n"
    "- Mantenha termos técnicos, nomes próprios e nomes de pessoas/marcas "
    "na forma original quando fizer sentido.\n"
    "- Preserve as quebras de parágrafo e de linha exatamente como "
    "aparecem no original."
)
_OPCOES_TRADUCAO = {"temperature": 0.3, "top_p": 0.9}


def _traduzir_com_fallback(texto_bloco, instrucao, indice, total):
    """Traduz um bloco com o prompt principal (rico, com regras de
    qualidade). Se falhar (exceção — timeout, rede — ou vier vazio), cai
    pro prompt de emergência, traduzindo parágrafo por parágrafo (bloco
    bem menor tem muito mais chance de não travar de novo). Nunca deixa
    o job inteiro morrer por causa de UM bloco ruim — na pior das
    hipóteses, devolve o parágrafo original sem traduzir, com um aviso."""
    prompt_principal = (
        f"{instrucao}\n\n{_REGRAS_QUALIDADE_TRADUCAO}\n\n"
        f"Abaixo está o bloco {indice} de {total} de um documento maior, "
        f"sendo processado em partes por causa do tamanho. Devolva SOMENTE "
        f"o resultado (sem comentar que é um trecho, sem adicionar título "
        f"ou nota sua):\n\n{texto_bloco}"
    )
    try:
        resposta = llm.chat([{"role": "user", "content": prompt_principal}],
                             opcoes_extra=_OPCOES_TRADUCAO)
        resultado = (resposta.get("content") or "").strip()
        if resultado:
            return resultado
    except Exception:
        pass  # cai pro fallback abaixo

    # 🆘 fallback: bloco inteiro falhou — tenta de novo em pedaços
    # menores (parágrafo a parágrafo), com prompt mais curto e direto
    partes = []
    for paragrafo in texto_bloco.split("\n\n"):
        if not paragrafo.strip():
            partes.append(paragrafo)
            continue
        try:
            prompt_emergencia = (
                f"{instrucao}\n\nTraduza mantendo a formatação e a "
                f"estrutura:\n\n{paragrafo}"
            )
            resposta = llm.chat([{"role": "user", "content": prompt_emergencia}],
                                 opcoes_extra=_OPCOES_TRADUCAO)
            traduzido = (resposta.get("content") or "").strip()
            partes.append(traduzido if traduzido else paragrafo)
        except Exception:
            partes.append(paragrafo)  # último recurso: mantém original
    return "\n\n".join(partes)


def _rodar_job_documento(job_id, caminho, instrucao, paginas_por_bloco, caminho_saida):
    job = _JOBS_DOCUMENTO[job_id]
    try:
        paginas = _extrair_paginas(caminho)
        blocos = [paginas[i:i + paginas_por_bloco]
                  for i in range(0, len(paginas), paginas_por_bloco)]
        job["total_blocos"] = len(blocos)

        # começa do zero: se já existia uma tentativa anterior nesse
        # arquivo de saída, sobrescreve (evita misturar resultado velho
        # com novo se o usuário rodar de novo com outra instrução)
        with open(caminho_saida, "w", encoding="utf-8") as f:
            f.write("")

        for i, bloco in enumerate(blocos, 1):
            texto_bloco = "\n\n".join(bloco).strip()
            if texto_bloco:
                # cada bloco tem seu próprio try/except — um bloco ruim
                # não derruba o job inteiro (os blocos já processados
                # continuam salvos no arquivo de saída)
                try:
                    resultado = _traduzir_com_fallback(texto_bloco, instrucao, i, len(blocos))
                except Exception as e:
                    resultado = f"[ERRO neste bloco, texto original mantido: {e}]\n\n{texto_bloco}"
                with open(caminho_saida, "a", encoding="utf-8") as f:
                    f.write(resultado + "\n\n")

            job["bloco_atual"] = i

        job["status"] = "concluido"
    except Exception as e:
        job["status"] = "erro"
        job["erro"] = str(e)


def traduzir_documento_em_blocos(nome_arquivo, instrucao=None, paginas_por_bloco=3, nome_saida=None):
    """Processa um livro/ebook/documento grande (PDF, DOCX ou TXT) que
    NÃO caberia de uma vez no contexto do modelo: divide em blocos de N
    'páginas' e manda cada bloco pro modelo local ativo, em sequência,
    gravando o resultado incrementalmente em workspace — assim nada se
    perde se travar no meio. Roda em segundo plano; use status_documento
    com o job_id devolvido aqui pra acompanhar o progresso."""
    caminho = _resolver_caminho(nome_arquivo)
    if caminho is None:
        return f"ERRO: '{nome_arquivo}' esta fora das pastas permitidas."
    if not os.path.exists(caminho):
        return f"ERRO: arquivo nao existe: {nome_arquivo}"

    try:
        paginas_por_bloco = max(1, int(paginas_por_bloco))
    except (TypeError, ValueError):
        paginas_por_bloco = 3

    instrucao = instrucao or "Traduza o texto para ingles, mantendo um estilo nativo e natural (nao literal)."

    nome_saida = nome_saida or (
        os.path.splitext(os.path.basename(caminho))[0] + "_processado.txt")
    caminho_saida = os.path.join(WORKSPACE, nome_saida)

    job_id = f"doc{len(_JOBS_DOCUMENTO) + 1}"
    _JOBS_DOCUMENTO[job_id] = {
        "status": "rodando", "bloco_atual": 0, "total_blocos": None,
        "arquivo_saida": caminho_saida, "erro": None,
    }

    thread = threading.Thread(
        target=_rodar_job_documento,
        args=(job_id, caminho, instrucao, paginas_por_bloco, caminho_saida),
        daemon=True,
    )
    thread.start()

    return (f"Iniciei o processamento em segundo plano, job '{job_id}'. "
            f"Blocos de {paginas_por_bloco} pagina(s) cada. Resultado sendo "
            f"salvo aos poucos em workspace/{nome_saida}. Use "
            f"status_documento('{job_id}') daqui a pouco pra ver o progresso "
            f"(o total de blocos só aparece depois que o arquivo é lido).")


def status_documento(job_id):
    """Consulta o progresso de um job iniciado por
    traduzir_documento_em_blocos (bloco atual, total, se já terminou ou
    deu erro, e o caminho do arquivo de saída)."""
    job = _JOBS_DOCUMENTO.get(job_id)
    if not job:
        ativos = ", ".join(_JOBS_DOCUMENTO.keys()) or "(nenhum)"
        return f"ERRO: job '{job_id}' nao encontrado. Jobs desta sessao: {ativos}"

    total = job["total_blocos"]
    if total is None:
        return f"Job '{job_id}': lendo o arquivo e dividindo em blocos..."

    linha = f"Job '{job_id}': {job['status']} - bloco {job['bloco_atual']}/{total}. Arquivo: {job['arquivo_saida']}"
    if job["erro"]:
        linha += f". Erro: {job['erro']}"
    return linha


# ─────────────────────────────────────────
# 🧾 NFe (nota fiscal eletronica) XML → PDF (DANFE simplificado)
#
# IMPORTANTE: isto gera uma representacao LEGIVEL dos dados da nota,
# nao o DANFE oficial byte-a-byte (que segue um layout rigido definido
# pela SEFAZ, com posicionamento exato de campos e codigo de barras
# Code-128 padronizado). O documento fiscal de verdade e o proprio XML
# autorizado pela SEFAZ — o DANFE (oficial ou este) e so uma
# representacao auxiliar pra leitura humana. Use este PDF pra conferencia
# e arquivo pessoal; para emitir o DANFE oficial de impressao/entrega,
# use o sistema emissor de NFe ou o portal da SEFAZ.
# ─────────────────────────────────────────

def _sem_namespace_xml(elem):
    for e in elem.iter():
        if "}" in e.tag:
            e.tag = e.tag.split("}", 1)[1]
    return elem


def _num_moeda(txt, casas=2):
    try:
        return f"{float(txt):.{casas}f}"
    except (TypeError, ValueError):
        return txt or "0,00"


def nfe_xml_para_pdf(nome_arquivo, nome_saida=None):
    """Converte um XML de NFe/NFC-e (nota fiscal eletronica brasileira) num
    PDF legivel com os dados principais: emitente, destinatario, itens,
    totais e chave de acesso com codigo de barras. E uma versao
    SIMPLIFICADA do DANFE (nao e o layout oficial pixel-a-pixel da SEFAZ),
    util pra conferencia e arquivo — o documento fiscal valido continua
    sendo o proprio XML."""
    caminho = _resolver_caminho(nome_arquivo)
    if caminho is None:
        return f"ERRO: '{nome_arquivo}' esta fora das pastas permitidas."
    if not os.path.exists(caminho):
        return f"ERRO: arquivo nao existe: {nome_arquivo}"

    try:
        import xml.etree.ElementTree as ET
    except ImportError:
        return "ERRO: modulo xml.etree nao disponivel (deveria vir com o Python padrao)."

    try:
        from fpdf import FPDF
    except ImportError:
        return "ERRO: para gerar PDF, instale: pip install fpdf2"

    try:
        root = _sem_namespace_xml(ET.parse(caminho).getroot())
    except Exception as e:
        return f"ERRO ao ler XML: {e}"

    inf = root.find(".//infNFe")
    if inf is None:
        return "ERRO: esse XML nao parece ser uma NFe (nao achei a tag <infNFe>)."

    chave = (inf.get("Id") or "").replace("NFe", "").strip()

    ide = root.find(".//ide")
    nNF = ide.findtext("nNF", "") if ide is not None else ""
    serie = ide.findtext("serie", "") if ide is not None else ""
    dhEmi = (ide.findtext("dhEmi") or ide.findtext("dEmi", "")) if ide is not None else ""
    natOp = ide.findtext("natOp", "") if ide is not None else ""

    emit = root.find(".//emit")
    emit_nome = emit.findtext("xNome", "") if emit is not None else ""
    emit_doc = (emit.findtext("CNPJ") or emit.findtext("CPF", "")) if emit is not None else ""
    emit_ie = emit.findtext("IE", "") if emit is not None else ""
    ender_emit = emit.find("enderEmit") if emit is not None else None

    def _endereco(el):
        if el is None:
            return ""
        partes = [el.findtext("xLgr", ""), el.findtext("nro", ""),
                  el.findtext("xBairro", ""), el.findtext("xMun", ""),
                  el.findtext("UF", "")]
        return ", ".join(p for p in partes if p)

    dest = root.find(".//dest")
    dest_nome = dest.findtext("xNome", "") if dest is not None else ""
    dest_doc = (dest.findtext("CNPJ") or dest.findtext("CPF", "")) if dest is not None else ""
    ender_dest = dest.find("enderDest") if dest is not None else None

    itens = []
    for det in root.findall(".//det"):
        prod = det.find("prod")
        if prod is None:
            continue
        itens.append([
            prod.findtext("cProd", ""), prod.findtext("xProd", ""),
            prod.findtext("NCM", ""), prod.findtext("CFOP", ""),
            prod.findtext("uCom", ""), _num_moeda(prod.findtext("qCom", "0"), 4),
            _num_moeda(prod.findtext("vUnCom", "0"), 4),
            _num_moeda(prod.findtext("vProd", "0")),
        ])
    if not itens:
        return "ERRO: nao encontrei nenhum item (<det>/<prod>) nesse XML."

    total = root.find(".//ICMSTot")
    vProd = _num_moeda(total.findtext("vProd", "0")) if total is not None else "0,00"
    vFrete = _num_moeda(total.findtext("vFrete", "0")) if total is not None else "0,00"
    vDesc = _num_moeda(total.findtext("vDesc", "0")) if total is not None else "0,00"
    vICMS = _num_moeda(total.findtext("vICMS", "0")) if total is not None else "0,00"
    vNF = _num_moeda(total.findtext("vNF", "0")) if total is not None else "0,00"

    try:
        pdf = FPDF(format="A4")
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()

        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 8, "DANFE - Documento Auxiliar da Nota Fiscal Eletronica", ln=True, align="C")
        pdf.set_font("Helvetica", "", 8)
        pdf.cell(0, 5, "(versao simplificada gerada pelo Jarvis - nao e o layout oficial da SEFAZ)", ln=True, align="C")
        pdf.ln(3)

        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, f"NF-e no {nNF}   Serie {serie}   Emissao: {dhEmi}", ln=True)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, f"Natureza da operacao: {natOp}", ln=True)
        pdf.ln(2)

        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "EMITENTE", ln=True)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, f"{emit_nome}   CNPJ/CPF: {emit_doc}   IE: {emit_ie}", ln=True)
        pdf.cell(0, 5, _endereco(ender_emit), ln=True)
        pdf.ln(2)

        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "DESTINATARIO", ln=True)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, f"{dest_nome}   CNPJ/CPF: {dest_doc}", ln=True)
        pdf.cell(0, 5, _endereco(ender_dest), ln=True)
        pdf.ln(4)

        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "ITENS", ln=True)
        pdf.set_font("Helvetica", "", 8)
        cabecalho = ["Cod", "Descricao", "NCM", "CFOP", "Un", "Qtd", "Vl.Unit", "Vl.Total"]
        with pdf.table(text_align="LEFT") as tabela:
            row = tabela.row()
            for c in cabecalho:
                row.cell(c)
            for linha_item in itens:
                row = tabela.row()
                for c in linha_item:
                    row.cell(str(c))

        pdf.ln(4)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "TOTAIS", ln=True)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, f"Produtos: R$ {vProd}   Frete: R$ {vFrete}   Desconto: R$ {vDesc}   ICMS: R$ {vICMS}", ln=True)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, f"VALOR TOTAL DA NOTA: R$ {vNF}", ln=True)
        pdf.ln(4)

        pdf.set_font("Helvetica", "", 8)
        pdf.cell(0, 5, f"Chave de acesso: {chave}", ln=True)
        if chave and len(chave) == 44 and chave.isdigit():
            pdf.interleaved2of5(chave, x=pdf.get_x(), y=pdf.get_y(), w=0.6, h=12)
            pdf.ln(16)
        else:
            pdf.cell(0, 5, "(chave de acesso ausente ou com formato inesperado - sem codigo de barras)", ln=True)

        nome_saida = nome_saida or (os.path.splitext(os.path.basename(caminho))[0] + "_danfe.pdf")
        caminho_saida = os.path.join(WORKSPACE, nome_saida)
        pdf.output(caminho_saida)
    except Exception as e:
        return f"ERRO ao gerar o PDF: {e}"

    return (f"PDF gerado: {caminho_saida} ({len(itens)} item(ns), valor total R$ {vNF}). "
            f"Lembrete: e uma versao simplificada do DANFE, nao o layout oficial da SEFAZ.")




# ─────────────────────────────────────────
# 📝 WORD COM AUTOMATION (win32com)
# ─────────────────────────────────────────

_MD_NEGRITO_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)


def _digitar_com_markdown(sel, texto, negrito_base=False):
    """Digita `texto` numa Selection do Word convertendo **negrito**
    estilo markdown em Font.Bold de verdade, em vez de deixar os
    asteriscos literais no documento. `negrito_base` é o estado de
    negrito a manter fora dos trechos **marcados** (o que já veio de
    formatacao.negrito, se o chamador passou algum)."""
    pos = 0
    sel.Font.Bold = negrito_base
    for m in _MD_NEGRITO_RE.finditer(texto):
        antes = texto[pos:m.start()]
        if antes:
            sel.TypeText(antes)
        sel.Font.Bold = True
        sel.TypeText(m.group(1))
        sel.Font.Bold = negrito_base
        pos = m.end()
    resto = texto[pos:]
    if resto:
        sel.TypeText(resto)


def word_escrever(caminho="", texto="", posicao="fim", formatacao=None):
    """Controla o Microsoft Word via COM automation.

    caminho   -- arquivo .docx para abrir (ou vazio = documento ativo).
    texto     -- conteudo a inserir.
    posicao   -- "fim" (append), "inicio", "cursor" (posicao atual),
                "substituir:ANTIGO" (busca e substitui ANTIGO pelo texto).
    formatacao -- dict opcional: {negrito, italico, tamanho, fonte, titulo}.
                titulo=1..9 insere como Heading daquele nivel.
    """
    if sys.platform != "win32":
        return "ERRO: COM automation so funciona no Windows."

    try:
        import win32com.client
        import pythoncom
    except ImportError:
        return ("ERRO: pywin32 nao instalado. "
                "Rode:  pip install pywin32")

    pythoncom.CoInitialize()
    word_app = None
    doc = None
    abriu_word = False

    try:
        # Tenta conectar ao Word ja aberto
        try:
            word_app = win32com.client.GetActiveObject("Word.Application")
        except Exception:
            word_app = win32com.client.Dispatch("Word.Application")
            abriu_word = True

        # SEMPRE força visível, mesmo conectando numa instância já aberta —
        # bug relatado em 22/08/2026 ("o Word não abre"): se sobrar uma
        # instância "fantasma" do Word registrada no Running Object Table
        # (processo que travou/fechou errado antes, mas deixou o objeto
        # COM vivo sem janela), GetActiveObject conecta nela silenciosamente
        # e o código antigo só forçava Visible=True no caminho do Dispatch
        # novo — então o Jarvis escrevia e salvava numa instância invisível,
        # sem nada aparecer na tela pro usuário.
        try:
            word_app.Visible = True
        except Exception:
            pass

        # Abrir documento ou usar o ativo
        if caminho:
            caminho_abs = _resolver_caminho(caminho)
            if caminho_abs is None:
                return f"ERRO: caminho fora das pastas permitidas: {caminho}"
            if not os.path.isfile(caminho_abs):
                # Cria documento novo. SaveAs2 só existe a partir do Word
                # 2010 — no Word 2007 (versão 12.0) o método não existe e
                # derruba a chamada inteira com erro COM. SaveAs (sem o
                # "2") existe em todas as versões, então tenta ele
                # primeiro e só usa SaveAs2 como fallback se algum dia
                # precisar de um recurso exclusivo dele.
                doc = word_app.Documents.Add()
                try:
                    doc.SaveAs(caminho_abs)
                except AttributeError:
                    doc.SaveAs2(caminho_abs)
            else:
                # Verifica se ja esta aberto
                doc = None
                for d in word_app.Documents:
                    try:
                        if os.path.normcase(os.path.abspath(d.FullName)) == \
                           os.path.normcase(os.path.abspath(caminho_abs)):
                            doc = d
                            break
                    except Exception:
                        pass
                if doc is None:
                    doc = word_app.Documents.Open(caminho_abs)
        else:
            if word_app.Documents.Count == 0:
                doc = word_app.Documents.Add()
            else:
                doc = word_app.ActiveDocument

        # Posicionar cursor
        sel = word_app.Selection

        if posicao == "fim":
            sel.EndKey(Unit=6)  # wdStory = 6
        elif posicao == "inicio":
            sel.HomeKey(Unit=6)
        elif posicao == "cursor":
            pass  # mantem onde esta
        elif posicao.startswith("substituir:"):
            antigo = posicao[len("substituir:"):]
            find = sel.Find
            find.ClearFormatting()
            find.Text = antigo
            find.Replacement.Text = texto
            find.Execute(Replace=2)  # wdReplaceAll = 2
            doc.Save()
            return (f"Substituido \'{antigo}\' por \'{texto}\' em todas as ocorrencias. "
                    f"Documento: {doc.FullName}")

        # Aplicar formatacao antes de digitar
        fmt = formatacao or {}

        if fmt.get("titulo"):
            nivel = int(fmt["titulo"])
            # wdStyleHeading1 = -2, wdStyleHeading2 = -3, ...
            sel.Style = word_app.ActiveDocument.Styles(-1 - nivel)

        if fmt.get("negrito"):
            sel.Font.Bold = True
        if fmt.get("italico"):
            sel.Font.Italic = True
        if fmt.get("tamanho"):
            sel.Font.Size = int(fmt["tamanho"])
        if fmt.get("fonte"):
            sel.Font.Name = str(fmt["fonte"])

        # Inserir texto — convertendo **negrito** estilo markdown em
        # negrito de verdade do Word, em vez de deixar os asteriscos
        # literais na tela. Bug flagrado em 22/08/2026: o modelo escreve
        # naturalmente em markdown (é o formato universal de chat), mas
        # o Word não interpreta markdown — só formatação via COM. Sem
        # isso, títulos como "**1. INTRODUÇÃO**" apareciam com os
        # asteriscos duplos visíveis no documento final.
        _digitar_com_markdown(sel, texto, negrito_base=bool(fmt.get("negrito")))

        # Reset formatacao para nao "vazar" pro texto seguinte
        sel.Font.Bold = False
        sel.Font.Italic = False
        if fmt.get("titulo"):
            sel.Style = word_app.ActiveDocument.Styles(-1)  # Normal

        doc.Save()

        info = f"Texto inserido no Word ({posicao}). Documento: {doc.FullName}"
        if abriu_word:
            info += " (Word foi aberto automaticamente)"
        return info

    except Exception as e:
        return f"ERRO Word COM: {e}"
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass

# ─────────────────────────────────────────
# 🧠 DESCRIÇÃO DAS FERRAMENTAS EM JSON
# (é isso que o modelo "enxerga" e usa para decidir)
# ─────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "criar_arquivo",
            "description": "Cria ou sobrescreve um arquivo de texto no workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nome": {"type": "string", "description": "Nome do arquivo, ex: notas.txt"},
                    "conteudo": {"type": "string", "description": "Conteudo do arquivo"}
                },
                "required": ["nome", "conteudo"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mover_arquivo",
            "description": "Move ou renomeia um arquivo existente. Use para organizar arquivos entre pastas do usuario (ex: tirar um arquivo do workspace e colocar em outra pasta do projeto), ou para renomear (mesma pasta, nome novo). Origem e destino aceitam nome simples (workspace) ou caminho completo dentro das pastas permitidas. Se o destino for uma pasta fora das permitidas, chame liberar_pasta com ela ANTES de chamar esta ferramenta.",
            "parameters": {
                "type": "object",
                "properties": {
                    "origem": {"type": "string", "description": "Nome ou caminho completo do arquivo a mover, ex: notas.txt ou C:\\IA\\Jarvis\\notas.txt"},
                    "destino": {"type": "string", "description": "Nome, pasta ou caminho completo de destino, ex: notas_antigas.txt ou C:\\Users\\Casa\\Documents\\notas.txt"}
                },
                "required": ["origem", "destino"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ler_arquivo",
            "description": "Le o conteudo de um arquivo: texto (.txt .py .md .json .csv), PDF (.pdf) e Word (.docx). Aceita nome simples (workspace) ou caminho completo dentro das pastas permitidas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nome": {"type": "string", "description": "Nome do arquivo ou caminho completo, ex: C:\\IA\\Jarvis\\README.txt"}
                },
                "required": ["nome"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "listar_arquivos",
            "description": "Lista os arquivos de uma pasta (com tamanhos reais). Sem argumento = workspace; ou passe um caminho completo dentro das pastas permitidas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pasta": {"type": "string", "description": "Caminho completo da pasta (opcional), ex: C:\\IA\\Jarvis"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "liberar_pasta",
            "description": "Libera uma pasta EXTRA para leitura, valida somente ate o Jarvis fechar. Use APENAS quando o usuario pedir explicitamente para ler/analisar uma pasta fora das permitidas. NUNCA use por iniciativa propria.",
            "parameters": {
                "type": "object",
                "properties": {
                    "caminho": {"type": "string", "description": "Caminho completo da pasta que o usuario pediu"}
                },
                "required": ["caminho"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analisar_pasta",
            "description": "Analisa uma pasta inteira: para CADA arquivo retorna tipo, tamanho e uma amostra REAL do conteudo. Use quando pedirem para entender, revisar ou melhorar um projeto/pasta. Baseie conclusoes SOMENTE nas amostras retornadas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pasta": {"type": "string", "description": "Caminho completo da pasta, ex: C:\\IA\\Jarvis"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "rodar_python",
            "description": "Executa um script Python (procurado no workspace e nas pastas de trabalho cadastradas) e retorna a saida.",
            "parameters": {
                "type": "object",
                "properties": {
                    "arquivo": {"type": "string", "description": "Nome ou caminho do script, ex: teste.py ou gerar_prompt_etapa.py"},
                    "argumentos": {"type": "string", "description": "Argumentos de linha de comando para o script, ex: 'B2 1'"}
                },
                "required": ["arquivo"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "rodar_comando",
            "description": "Executa um comando de shell no workspace e retorna a saida.",
            "parameters": {
                "type": "object",
                "properties": {
                    "comando": {"type": "string", "description": "Comando, ex: dir ou pip list"}
                },
                "required": ["comando"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "busca_web",
            "description": "Pesquisa um TEMA na internet (varios sites, sem site especifico). USE para: noticias, esportes, placares, eventos recentes, clima, precos, ou perguntas sobre 'hoje'/'ontem'/'este ano'. NAO USE se o usuario citou um site especifico (nome ou URL, ex: 'abra o g1.com', 'veja no globo.com', 'acesse tal site') — nesse caso use abrir_pagina para entrar NAQUELE site, mesmo que a pergunta seja sobre noticias/manchetes de hoje.",
            "parameters": {
                "type": "object",
                "properties": {
                    "consulta": {"type": "string", "description": "Termos da busca"}
                },
                "required": ["consulta"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "consultar_nuvem",
            "description": "Consulta uma IA de nuvem MUITO mais inteligente (Claude) para tarefas DIFICEIS de verdade: textos cientificos ou tecnicos longos, analises complexas, revisao rigorosa, matematica avancada, codigo dificil. Custa dinheiro por uso. NUNCA use para perguntas simples, contas basicas (ex: 2+2), saudacoes, definicoes triviais ou qualquer coisa que voce mesmo sabe responder — nesses casos responda direto, sem chamar ferramenta nenhuma.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pergunta": {"type": "string", "description": "A tarefa ou pergunta completa e detalhada"},
                    "contexto": {"type": "string", "description": "Informacoes de apoio (opcional): dados, trechos, resultados de buscas"}
                },
                "required": ["pergunta"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "chamar_api",
            "description": "Chama uma API REST (GET, POST, PUT, DELETE) numa URL especifica e conhecida (ex: um endpoint que o usuario passou, ou de um servico ja integrado). NUNCA use para responder perguntas de conhecimento geral, gramatica, definicoes de palavras, traducao ou duvidas de linguagem — essas voce responde direto, sem ferramenta nenhuma. So use quando houver uma URL real e especifica pra chamar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "metodo": {"type": "string", "description": "GET, POST, PUT ou DELETE"},
                    "url": {"type": "string", "description": "URL completa"},
                    "corpo": {"type": "string", "description": "Corpo JSON (opcional)"},
                    "cabecalhos": {"type": "string", "description": "Headers em JSON (opcional)"}
                },
                "required": ["metodo", "url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "supabase_consultar",
            "description": "Consulta registros de uma tabela do banco Supabase do usuario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tabela": {"type": "string", "description": "Nome da tabela"},
                    "filtro": {"type": "string", "description": "Filtro PostgREST opcional, ex: id=eq.1 ou select=nome,idade"},
                    "limite": {"type": "integer", "description": "Maximo de registros (padrao 20)"}
                },
                "required": ["tabela"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "supabase_inserir",
            "description": "Insere um registro numa tabela do banco Supabase do usuario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tabela": {"type": "string", "description": "Nome da tabela"},
                    "dados": {"type": "string", "description": "Registro em JSON, ex: {\"nome\": \"Bento\"}"}
                },
                "required": ["tabela", "dados"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "vercel_projetos",
            "description": "Lista os projetos do usuario na Vercel.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "vercel_deploys",
            "description": "Lista os ultimos deploys na Vercel (estado, URL).",
            "parameters": {
                "type": "object",
                "properties": {
                    "projeto": {"type": "string", "description": "Nome do projeto (opcional, vazio = todos)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "gerar_imagem",
            "description": "Gera uma imagem com Stable Diffusion a partir de uma descricao em texto e salva no workspace. Descreva a imagem em ingles para melhor resultado.",
            "parameters": {
                "type": "object",
                "properties": {
                    "descricao": {"type": "string", "description": "Descricao detalhada da imagem (prompt), preferencialmente em ingles"},
                    "nome_arquivo": {"type": "string", "description": "Nome do arquivo .png (opcional)"}
                },
                "required": ["descricao"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "gerar_video",
            "description": "Gera um clipe curto (2-3 segundos, AnimateDiff) a partir de uma descricao em texto e salva no workspace. Descreva a cena em ingles para melhor resultado. AVISO: demora bem mais que gerar_imagem (5 a 10 minutos numa GPU comum) — avise o usuario disso antes de chamar, e nao repita a chamada so por demorar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "descricao": {"type": "string", "description": "Descricao detalhada da cena/movimento (prompt), preferencialmente em ingles"},
                    "nome_arquivo": {"type": "string", "description": "Nome do arquivo .mp4 (opcional)"},
                    "num_frames": {"type": "integer", "description": "Numero de frames, 8 a 24 (padrao 16, ~2s a 8fps)"},
                    "fps": {"type": "integer", "description": "Quadros por segundo do clipe final (padrao 8)"}
                },
                "required": ["descricao"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "listar_janelas",
            "description": "Lista os titulos de todas as janelas abertas no Windows agora (Estado da Maquina, so leitura, sem risco). Use para descobrir o titulo exato antes de chamar clicar_elemento/digitar_em_elemento/fechar_janela.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "listar_programas_instalados",
            "description": "Lista os programas instalados no Windows (igual a 'Programas e Recursos' do Painel de Controle). So leitura, sem risco, nao precisa de admin. Use pra descobrir o nome exato de um app antes de liberar ele com liberar_app_desktop.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "janela_ativa",
            "description": "Diz qual janela esta em foco (a da frente) agora no Windows. So leitura, sem risco.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "listar_allowlist",
            "description": "Mostra quais aplicativos/janelas estao liberados para o agente desktop clicar/digitar (allowlist). So leitura.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "liberar_app_desktop",
            "description": "Libera um aplicativo/janela para o agente desktop poder clicar e digitar nele. SO chame quando o USUARIO pedir isso explicitamente no chat (ex: 'libera o Bloco de Notas pro Jarvis mexer') -- nunca decida sozinho liberar um app.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nome_processo_ou_titulo": {"type": "string", "description": "Nome do processo (ex: notepad.exe) ou parte do titulo da janela (ex: 'Visual Studio Code')"}
                },
                "required": ["nome_processo_ou_titulo"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "abrir_aplicativo",
            "description": "Abre um aplicativo/programa via comando (ex: 'notepad', 'code C:\\pasta'). Nao precisa de allowlist para abrir -- so para clicar/digitar depois.",
            "parameters": {
                "type": "object",
                "properties": {
                    "comando": {"type": "string", "description": "Comando pra abrir o programa"},
                    "titulo_esperado": {"type": "string", "description": "Parte do titulo da janela esperada, pra confirmar que abriu (opcional)"}
                },
                "required": ["comando"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "listar_elementos_janela",
            "description": "Lista os botoes/campos/abas com texto de uma janela (UI Automation, so leitura). Use antes de clicar_elemento ou digitar_em_elemento pra saber o nome exato do elemento.",
            "parameters": {
                "type": "object",
                "properties": {
                    "titulo_janela": {"type": "string", "description": "Titulo (ou parte dele) da janela"}
                },
                "required": ["titulo_janela"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "clicar_elemento",
            "description": "Clica num botao/elemento pelo nome dentro de uma janela (UI Automation). A janela precisa estar liberada na allowlist (liberar_app_desktop). Se o nome do elemento parecer irreversivel (excluir, confirmar, enviar, etc.), a ferramenta BLOQUEIA e pede confirmar=true -- so mande confirmar=true depois que o USUARIO confirmar em texto no chat, nunca decida sozinho.",
            "parameters": {
                "type": "object",
                "properties": {
                    "titulo_janela": {"type": "string", "description": "Titulo (ou parte dele) da janela"},
                    "nome_elemento": {"type": "string", "description": "Texto visivel do elemento a clicar"},
                    "confirmar": {"type": "boolean", "description": "true SO depois que o usuario confirmar uma acao irreversivel no chat"}
                },
                "required": ["titulo_janela", "nome_elemento"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "digitar_em_elemento",
            "description": "Digita um texto num campo dentro de uma janela (UI Automation). A janela precisa estar liberada na allowlist (liberar_app_desktop).",
            "parameters": {
                "type": "object",
                "properties": {
                    "titulo_janela": {"type": "string", "description": "Titulo (ou parte dele) da janela"},
                    "nome_elemento": {"type": "string", "description": "Nome/label do campo a preencher"},
                    "texto": {"type": "string", "description": "Texto a digitar"}
                },
                "required": ["titulo_janela", "nome_elemento", "texto"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fechar_janela",
            "description": "Fecha uma janela. SEMPRE tratado como acao irreversivel (pode perder trabalho nao salvo) -- exige confirmar=true, que so deve vir depois do USUARIO confirmar em texto no chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "titulo_janela": {"type": "string", "description": "Titulo (ou parte dele) da janela"},
                    "confirmar": {"type": "boolean", "description": "true SO depois que o usuario confirmar no chat"}
                },
                "required": ["titulo_janela"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "salvar_memoria",
            "description": "Salva uma informacao importante na memoria de longo prazo (ChromaDB). Use quando o usuario contar algo que deve ser lembrado no futuro.",
            "parameters": {
                "type": "object",
                "properties": {
                    "texto": {"type": "string", "description": "Informacao a memorizar"},
                    "categoria": {"type": "string", "description": "Categoria, ex: projects, pessoal, general"}
                },
                "required": ["texto"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_memoria",
            "description": "Busca na memoria de longo prazo informacoes relacionadas a uma consulta.",
            "parameters": {
                "type": "object",
                "properties": {
                    "consulta": {"type": "string", "description": "O que procurar na memoria"}
                },
                "required": ["consulta"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "listar_memorias",
            "description": "Lista TODAS as memorias de longo prazo gravadas (auditoria). Use quando o usuario perguntar o que voce lembra.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "apagar_memoria",
            "description": "Apaga da memoria de longo prazo as lembrancas parecidas com a consulta. Use APENAS quando o usuario mandar esquecer algo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "consulta": {"type": "string", "description": "Tema ou texto das memorias a apagar"}
                },
                "required": ["consulta"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "traduzir_documento_em_blocos",
            "description": "Traduz ou processa um livro/ebook/documento GRANDE (PDF, DOCX ou TXT) que nao caberia inteiro no contexto do modelo de uma vez. Divide o arquivo em blocos de N paginas e processa cada bloco em sequencia com o modelo local ativo, salvando o resultado aos poucos em workspace (nada se perde se travar no meio). Roda em segundo plano e devolve um job_id — use status_documento para acompanhar. Use isso em vez de tentar colar o livro inteiro na conversa.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nome_arquivo": {"type": "string", "description": "Nome ou caminho do arquivo (PDF, DOCX ou TXT), ex: livro.pdf"},
                    "instrucao": {"type": "string", "description": "O que fazer com cada bloco, ex: 'Traduza para ingles mantendo estilo nativo'. Se omitido, traduz para ingles por padrao."},
                    "paginas_por_bloco": {"type": "integer", "description": "Quantas paginas processar por chamada ao modelo (2 ou 3 e um bom padrao). Default: 3"},
                    "nome_saida": {"type": "string", "description": "Nome do arquivo de saida em workspace (opcional; default: <nome>_processado.txt)"}
                },
                "required": ["nome_arquivo"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "status_documento",
            "description": "Consulta o progresso (bloco atual/total, status, arquivo de saida) de um processamento de documento iniciado por traduzir_documento_em_blocos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "O job_id devolvido por traduzir_documento_em_blocos, ex: doc1"}
                },
                "required": ["job_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "nfe_xml_para_pdf",
            "description": "Converte um XML de NFe/NFC-e (nota fiscal eletronica brasileira) num PDF legivel (DANFE simplificado) com emitente, destinatario, itens, totais e codigo de barras da chave de acesso. NAO e o layout oficial pixel-a-pixel da SEFAZ, e uma versao pra conferencia/arquivo pessoal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nome_arquivo": {"type": "string", "description": "Nome ou caminho do arquivo XML da NFe, ex: nota.xml"},
                    "nome_saida": {"type": "string", "description": "Nome do PDF de saida em workspace (opcional; default: <nome>_danfe.pdf)"}
                },
                "required": ["nome_arquivo"]
            }
        }
    }
,
    {
        "type": "function",
        "function": {
            "name": "word_escrever",
            "description": "Digita texto no Microsoft Word via COM automation. Abre o documento indicado (ou usa o ativo), insere texto na posicao escolhida e salva. Suporta formatacao (negrito, italico, tamanho, fonte, titulo/heading). Tambem faz busca-e-substituicao com posicao='substituir:TEXTO_ANTIGO'. Se o arquivo nao existir, cria um documento novo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "caminho": {"type": "string", "description": "Caminho do .docx (vazio = documento ativo no Word)"},
                    "texto": {"type": "string", "description": "Texto a inserir no documento"},
                    "posicao": {"type": "string", "description": "Onde inserir: fim (default), inicio, cursor, substituir:TEXTO_ANTIGO", "default": "fim"},
                    "formatacao": {
                        "type": "object",
                        "description": "Formatacao opcional: negrito (bool), italico (bool), tamanho (int), fonte (str), titulo (int 1-9 = Heading)",
                        "properties": {
                            "negrito": {"type": "boolean"},
                            "italico": {"type": "boolean"},
                            "tamanho": {"type": "integer"},
                            "fonte": {"type": "string"},
                            "titulo": {"type": "integer", "description": "Nivel do heading (1-9)"}
                        }
                    }
                },
                "required": ["texto"]
            }
        }
    }
,
    {
        "type": "function",
        "function": {
            "name": "criar_projeto_sandbox",
            "description": "Cria um projeto base no sandbox (10GB alocados). Templates: 'web' (HTML/CSS/JS), 'python', 'nodejs', 'react', 'flask'. Retorna caminho dos arquivos criados.",
            "parameters": {"type": "object", "properties": {
                "nome": {"type": "string", "description": "Nome do projeto (pasta em sandbox/projetos/)"},
                "template": {"type": "string", "description": "Tipo: 'web', 'python', 'nodejs', 'react', 'flask', 'vazio'", "default": "vazio"}},
                "required": ["nome"]}}
    },
    {
        "type": "function",
        "function": {
            "name": "instalar_dependencias",
            "description": "Instala dependências do projeto no sandbox (npm install, pip install, yarn, pnpm). Detecta automaticamente package.json ou requirements.txt.",
            "parameters": {"type": "object", "properties": {
                "projeto": {"type": "string", "description": "Nome do projeto em sandbox/projetos/"},
                "tipo": {"type": "string", "description": "Forçar tipo: 'auto', 'npm', 'pip', 'yarn', 'pnpm'", "default": "auto"}},
                "required": ["projeto"]}}
    },
    {
        "type": "function",
        "function": {
            "name": "git_inicializar",
            "description": "Inicializa repositório Git no projeto do sandbox.",
            "parameters": {"type": "object", "properties": {
                "projeto": {"type": "string", "description": "Nome do projeto em sandbox/projetos/"}},
                "required": ["projeto"]}}
    },
    {
        "type": "function",
        "function": {
            "name": "git_adicionar",
            "description": "Adiciona arquivos ao staging do Git (git add).",
            "parameters": {"type": "object", "properties": {
                "projeto": {"type": "string", "description": "Nome do projeto em sandbox/projetos/"},
                "arquivos": {"type": "array", "items": {"type": "string"}, "description": "Lista de arquivos (opcional, default: todos)"}},
                "required": ["projeto"]}}
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Faz commit no Git (git commit).",
            "parameters": {"type": "object", "properties": {
                "projeto": {"type": "string", "description": "Nome do projeto em sandbox/projetos/"},
                "mensagem": {"type": "string", "description": "Mensagem do commit", "default": "Auto-commit do Jarvis"}},
                "required": ["projeto"]}}
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Mostra status do Git (git status --short).",
            "parameters": {"type": "object", "properties": {
                "projeto": {"type": "string", "description": "Nome do projeto em sandbox/projetos/"}},
                "required": ["projeto"]}}
    },
    {
        "type": "function",
        "function": {
            "name": "rodar_testes",
            "description": "Roda testes do projeto (pytest, jest, npm test). Retorna se passou e a saída.",
            "parameters": {"type": "object", "properties": {
                "projeto": {"type": "string", "description": "Nome do projeto em sandbox/projetos/"},
                "tipo": {"type": "string", "description": "Forçar tipo: 'auto', 'pytest', 'jest', 'npm'", "default": "auto"}},
                "required": ["projeto"]}}
    },
    {
        "type": "function",
        "function": {
            "name": "rodar_python_sandbox",
            "description": "Executa script Python no sandbox (sandbox/projetos/).",
            "parameters": {"type": "object", "properties": {
                "arquivo": {"type": "string", "description": "Caminho relativo do script, ex: 'projeto/script.py'"},
                "argumentos": {"type": "string", "description": "Argumentos de linha de comando", "default": ""}},
                "required": ["arquivo"]}}
    },
    {
        "type": "function",
        "function": {
            "name": "rodar_comando_sandbox",
            "description": "Executa comando shell no sandbox (sandbox/projetos/).",
            "parameters": {"type": "object", "properties": {
                "comando": {"type": "string", "description": "Comando shell completo"}},
                "required": ["comando"]}}
    },
    {
        "type": "function",
        "function": {
            "name": "abrir_navegador",
            "description": "Abre arquivo local (index.html) ou URL no navegador padrão.",
            "parameters": {"type": "object", "properties": {
                "caminho": {"type": "string", "description": "Caminho do arquivo no sandbox (ex: 'projeto/index.html') ou URL (http://...)"}},
                "required": ["caminho"]}}
    }
]

# 🔒 consultar_nuvem só é oferecida ao modelo se a chave da Anthropic
# estiver configurada — sem isso, o modelo nem vê a ferramenta (evita
# ele tentar chamar algo que vai só devolver erro de chave faltando).
if not ANTHROPIC_API_KEY:
    TOOLS = [t for t in TOOLS if t.get("function", {}).get("name") != "consultar_nuvem"]


# ─────────────────────────────────────────
# 🧠 DISPATCHER: executa a ferramenta que o modelo pediu
# ─────────────────────────────────────────

FERRAMENTAS = {
    "criar_arquivo": criar_arquivo,
    "mover_arquivo": mover_arquivo,
    "ler_arquivo": ler_arquivo,
    "listar_arquivos": listar_arquivos,
    "analisar_pasta": analisar_pasta,
    "liberar_pasta": liberar_pasta,
    "rodar_python": rodar_python,
    "rodar_comando": rodar_comando,
    "busca_web": busca_web,
    "consultar_nuvem": consultar_nuvem,
    "chamar_api": chamar_api,
    "supabase_consultar": supabase_consultar,
    "supabase_inserir": supabase_inserir,
    "vercel_projetos": vercel_projetos,
    "vercel_deploys": vercel_deploys,
    "gerar_imagem": gerar_imagem,
    "gerar_video": gerar_video,
    "listar_janelas": desktop_agent.listar_janelas,
    "listar_programas_instalados": desktop_agent.listar_programas_instalados,
    "janela_ativa": desktop_agent.janela_ativa,
    "listar_allowlist": desktop_agent.listar_allowlist,
    "liberar_app_desktop": desktop_agent.liberar_app_desktop,
    "abrir_aplicativo": desktop_agent.abrir_aplicativo,
    "listar_elementos_janela": desktop_agent.listar_elementos_janela,
    "clicar_elemento": desktop_agent.clicar_elemento,
    "digitar_em_elemento": desktop_agent.digitar_em_elemento,
    "fechar_janela": desktop_agent.fechar_janela,
    "salvar_memoria": salvar_memoria,
    "buscar_memoria": buscar_memoria,
    "apagar_memoria": apagar_memoria,
    "listar_memorias": listar_memorias,
    "traduzir_documento_em_blocos": traduzir_documento_em_blocos,
    "status_documento": status_documento,
    "nfe_xml_para_pdf": nfe_xml_para_pdf,
    "word_escrever": word_escrever,
    # Sandbox tools para desenvolvimento de apps
    "criar_projeto_sandbox": sandbox_tools.criar_projeto,
    "instalar_dependencias": sandbox_tools.instalar_dependencias,
    "git_inicializar": sandbox_tools.git_inicializar,
    "git_adicionar": sandbox_tools.git_adicionar,
    "git_commit": sandbox_tools.git_commit,
    "git_status": sandbox_tools.git_status,
    "rodar_testes": sandbox_tools.rodar_testes,
    "rodar_python_sandbox": sandbox_tools.rodar_python_sandbox,
    "abrir_navegador": sandbox_tools.abrir_navegador,
}


def executar_ferramenta(nome, argumentos):
    funcao = FERRAMENTAS.get(nome)

    if funcao is None:
        return f"ERRO: ferramenta desconhecida: {nome}"

    try:
        return str(funcao(**(argumentos or {})))
    except Exception as e:
        return f"ERRO ao executar {nome}: {e}"


# ─────────────────────────────────────────
# 🔌 PLUGINS: ferramentas extras da pasta plugins/
# Cada arquivo .py ali deve definir:
#   TOOLS       = [ ...descrições JSON... ]
#   FERRAMENTAS = { "nome": funcao, ... }
# O Jarvis carrega tudo sozinho na inicialização.
#
# plugins.json (na pasta do Jarvis Claude) liga/desliga cada plugin sem
# apagar o arquivo — true = carrega, false = ignora. Gerado automaticamente
# na primeira vez, com tudo ligado.
# ─────────────────────────────────────────

PLUGINS_DIR = os.path.join(config.BASE_DIR, "plugins")
PLUGINS_CONFIG_PATH = os.path.join(config.BASE_DIR, "plugins.json")


def _plugins_disponiveis():
    if not os.path.isdir(PLUGINS_DIR):
        return []
    return sorted(
        arquivo[:-3] for arquivo in os.listdir(PLUGINS_DIR)
        if arquivo.endswith(".py") and not arquivo.startswith("_")
    )


def _ler_config_plugins():
    """Lê plugins.json; cria (tudo ligado) se não existir; acrescenta
    plugins novos encontrados na pasta com ligado=true por padrão."""
    disponiveis = _plugins_disponiveis()

    estado = {}
    if os.path.exists(PLUGINS_CONFIG_PATH):
        try:
            with open(PLUGINS_CONFIG_PATH, "r", encoding="utf-8") as f:
                estado = json.load(f) or {}
        except Exception:
            estado = {}

    mudou = False
    for nome in disponiveis:
        if nome not in estado:
            estado[nome] = True
            mudou = True

    if mudou or not os.path.exists(PLUGINS_CONFIG_PATH):
        try:
            with open(PLUGINS_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(estado, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    return estado


def plugin_ligado(nome, estado=None):
    estado = estado if estado is not None else _ler_config_plugins()
    return estado.get(nome, True) is not False


def listar_plugins():
    """{"nome_do_plugin": True/False} — para a interface (bandeja/web)."""
    return _ler_config_plugins()


def definir_plugin(nome, ligado):
    """Liga/desliga um plugin em plugins.json (precisa reiniciar p/ aplicar)."""
    estado = _ler_config_plugins()
    estado[nome] = bool(ligado)
    try:
        with open(PLUGINS_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


PLUGINS_LOG_PATH = os.path.join(config.BASE_DIR, "plugins_erro.log")


def _log_plugin(msg):
    """Alem do print() (invisivel no .exe empacotado, console=False),
    grava num arquivo persistente — sem isso, uma falha de carregamento
    de plugin no build empacotado nao deixa NENHUM rastro visivel (foi
    exatamente o que aconteceu com o tirar_print sumindo em producao,
    sem forma de diagnosticar a causa real)."""
    print(msg)
    try:
        import datetime
        carimbo = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(PLUGINS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{carimbo}] {msg}\n")
    except Exception:
        pass


def _carregar_plugins():
    if not os.path.isdir(PLUGINS_DIR):
        return

    import importlib.util
    import traceback

    estado = _ler_config_plugins()

    for arquivo in sorted(os.listdir(PLUGINS_DIR)):
        if not arquivo.endswith(".py") or arquivo.startswith("_"):
            continue

        nome = arquivo[:-3]
        if not plugin_ligado(nome, estado):
            print(f"[PLUGIN] {arquivo}: desligado em plugins.json — pulado")
            continue

        caminho = os.path.join(PLUGINS_DIR, arquivo)
        try:
            spec = importlib.util.spec_from_file_location(
                f"plugin_{arquivo[:-3]}", caminho)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            novas = getattr(mod, "TOOLS", [])
            funcs = getattr(mod, "FERRAMENTAS", {})

            TOOLS.extend(novas)
            FERRAMENTAS.update(funcs)

            print(f"[PLUGIN] {arquivo}: carregou {list(funcs.keys())}")
        except Exception as e:
            _log_plugin(
                f"[PLUGIN] ERRO em {arquivo}: {e}\n{traceback.format_exc()}")


_carregar_plugins()


# ─────────────────────────────────────────
# 🎯 PREFILTRAGEM: reduz o catálogo de ferramentas mandado ao modelo
# de acordo com a mensagem, sem tirar capacidade real. O esquema das
# 35 ferramentas (núcleo + plugins) custa uns 3 mil tokens sozinho —
# pra uma pergunta tipo "qual a capital da França?" isso é desperdício
# puro. Categorias sem NENHUM sinal na mensagem são cortadas; se a
# mensagem não bater com nada reconhecido, manda o catálogo INTEIRO
# (fail-open — nunca arrisca faltar uma ferramenta que precisava).
# ─────────────────────────────────────────

_SEMPRE_INCLUIDAS = {
    "busca_web", "salvar_memoria", "buscar_memoria", "listar_memorias",
    "apagar_memoria", "criar_arquivo", "mover_arquivo", "ler_arquivo",
    "listar_arquivos",
}

_PALAVRAS_CATEGORIA = {
    # 🔑 cada categoria tem no mínimo 10 palavras/gatilhos (a maioria dobrada
    # em relação à versão anterior) — quanto mais sinônimos, menor a chance
    # de uma mensagem com intenção clara não acionar a categoria certa.
    "execucao": (
        re.compile(
            r"\b(execut[ae]r?|executando|execu[çc][ãa]o|roda[r]?|rodando|rode|"
            r"corr[ae]|correndo|comando|cmd|terminal|shell|powershell|bash|"
            r"batch|compilar|interpretar)\b|\.(py|bat|sh|ps1)\b",
            re.IGNORECASE),
        ["rodar_python", "rodar_comando"],
    ),
    "pastas": (
        re.compile(
            r"\b(pastas?|diret[óo]rios?|subpasta|subdiret[óo]rio|"
            r"libera(r)?\s+pasta|permit(e|ir)\s+pasta|acessar\s+pasta|"
            r"abrir\s+pasta|conte[úu]do\s+da\s+pasta|dentro\s+da\s+pasta)\b",
            re.IGNORECASE),
        ["liberar_pasta", "analisar_pasta"],
    ),
    "nuvem": (
        re.compile(
            r"\b(supabase|vercel|deploy(ment)?|n8n|webhook|api\s|"
            r"chamar\s+api|integra[çc][ãa]o|integrar|nuvem|cloud|"
            r"banco\s+de\s+dados|hospedagem|servidor\s+remoto|endpoint|"
            r"requisi[çc][ãa]o|publicar|subir\s+pro\s+ar)\b",
            re.IGNORECASE),
        ["consultar_nuvem", "chamar_api", "supabase_consultar",
         "supabase_inserir", "vercel_projetos", "vercel_deploys", "chamar_n8n"],
    ),
    "imagem": (
        re.compile(
            # verbos autossuficientes (desenhar/ilustrar já implicam imagem,
            # não precisam de "imagem"/"foto" na frase: "desenhe um cachorro")
            r"\b(desenh\w*|ilustr\w*|pint\w*|renderiz\w*)\b"
            r"|"
            # verbos ambíguos (criar/gerar/fazer também servem pra arquivo,
            # relatório etc.) — só conta como pedido de imagem se vier
            # junto com uma palavra de imagem por perto
            r"\b(ger\w*|cri\w*|fa[çc]\w*)\b.{0,20}"
            r"\b(imagem|foto|figura|desenho|ilustra[çc][ãa]o|arte|pintura|"
            r"retrato|wallpaper|banner|logotipo|capa)\b",
            re.IGNORECASE,
        ),
        ["gerar_imagem"],
    ),
    "video": (
        re.compile(
            r"\b(v[íi]deos?|clipe|videoclipe|anima[çc][ãa]o|animar|anime|"
            r"filmagem|trailer|motion|gif\s+animado)\b",
            re.IGNORECASE,
        ),
        ["gerar_video"],
    ),
    "desktop": (
        re.compile(
            # gatilhos específicos do agente desktop — "libera\w*" sozinho
            # já basta (cobre "libera o Bloco de Notas", "libera o VS Code",
            # sem depender de a frase citar literalmente "app"/"programa")
            r"\b(janelas?|aplicativos?|programas?|apps?\b|clic\w*|bot[ãa]o|"
            r"campo|allowlist|desktop|[áa]rea\s+de\s+trabalho|libera\w*|"
            r"mexer\s+n(o|a|ele|ela)|abr\w*\s+o\s+\w+|interface|minimizar|"
            r"maximizar|foco\s+na\s+janela|atalho|execut[áa]vel|"
            r"digitar\s+no\s+campo)\b",
            re.IGNORECASE,
        ),
        ["listar_janelas", "listar_programas_instalados", "janela_ativa", "listar_allowlist",
         "liberar_app_desktop", "abrir_aplicativo",
         "listar_elementos_janela", "clicar_elemento",
         "digitar_em_elemento", "fechar_janela"],
    ),
    "codigo": (
        re.compile(
            r"\b(c[óo]digo|bug|fun[çc][ãa]o|classe|script|programar|"
            r"debugar|refatorar|m[ée]todo|vari[áa]vel|biblioteca|"
            r"depend[êe]ncia|sintaxe|stack\s+trace|ide|algoritmo)\b",
            re.IGNORECASE),
        ["identificar_arquivo", "analisar_codigo", "delegar_codigo"],
    ),
    "missao": (
        re.compile(
            r"\b(miss[ãa]o|projeto\s+completo|v[áa]rias\s+etapas|"
            r"multiagente|plano\s+completo|tarefa\s+complexa|"
            r"fluxo\s+completo|v[áa]rias\s+fases|orquestrar|"
            r"equipe\s+de\s+agentes)\b", re.IGNORECASE),
        ["executar_missao", "executar_multiagente"],
    ),
    "sandbox": (
        re.compile(
            r"\b(sandbox|projeto\s+web|app\s+web|calculadora|site\s+simples|"
            r"html\s+css|javascript|nodejs|npm|yarn|pnpm|"
            r"git\s+init|git\s+add|git\s+commit|commitar|"
            r"instalar\s+depend[êe]ncia|instalar\s+libs|package\.json|"
            r"requirements\.txt|rodar\s+teste|testar\s+app|"
            r"abrir\s+no\s+navegador|servidor\s+local|dev\s+server)\b",
            re.IGNORECASE),
        ["criar_projeto_sandbox", "instalar_dependencias", "git_inicializar",
         "git_adicionar", "git_commit", "git_status", "rodar_testes",
         "rodar_python_sandbox", "rodar_comando_sandbox", "abrir_navegador"],
    ),
    "navegador": (
        re.compile(
            r"\b(navegador|abrir\s+(o\s+)?site|abrir\s+link|clique|clicar|"
            r"preencher\s+formul[áa]rio|captura(r)?\s+(de\s+)?tela|"
            r"p[áa]gina\s+web|https?|url|link|chrome|edge|firefox|"
            r"rolar\s+a\s+p[áa]gina)\b",
            re.IGNORECASE),
        ["abrir_pagina", "ler_pagina", "clicar", "preencher", "capturar_tela"],
    ),
    "pdf": (
        re.compile(
            r"\b(pdfs?|arquivo\s+pdf|documento\s+pdf|juntar\s+pdf|"
            r"dividir\s+pdf|extrair\s+p[áa]gina|girar\s+pdf|"
            r"marca\s+d['´]?[áa]gua|assinar\s+pdf|converter\s+.{0,15}pdf)\b",
            re.IGNORECASE),
        ["juntar_pdfs", "dividir_pdf", "extrair_paginas_pdf", "girar_pdf",
         "adicionar_marca_dagua_pdf", "extrair_texto_pdf"],
    ),
    "nfe": (
        re.compile(
            r"\b(nfe|nf-e|nfc-e|danfe|nota\s+fiscal|"
            r"nota\s+fiscal\s+eletr[ôo]nica|xml\s+da\s+nota|"
            r"cupom\s+fiscal|sped|imposto)\b",
            re.IGNORECASE,
        ),
        ["nfe_xml_para_pdf"],
    ),
    "documento_grande": (
        re.compile(
            r"\b(livro|ebook|e-book|apostila|manual|tese|monografia|"
            r"cap[íi]tulos|texto\s+longo|documento\s+extenso)\b"
            # "traduzir/traduz/traduza/traduzindo/tradução" perto de um
            # documento/arquivo/pdf/páginas — cobre as conjugações comuns,
            # não só o infinitivo puro
            r"|\b(traduz\w*|tradu[çc][ãa]o)\b.{0,20}"
            r"\b(documento|arquivo|pdf|paginas?|livro|apostila)\b"
            r"|\b(documento|arquivo)\b.{0,20}\b(paginas?|blocos?)\b",
            re.IGNORECASE,
        ),
        ["traduzir_documento_em_blocos", "status_documento"],
    ),
    "word": (
        re.compile(
            r"\b(word|docx|documento\s+word|abr[aei]r?\s+word|"
            r"digit[ae]r?\s+(no|em|pro)\s+word|"
            r"escrever?\s+(no|em|pro)\s+word|"
            r"inserir?\s+(no|em|pro)\s+word|"
            r"formatar?\s+(no|em|pro)\s+word|"
            r"preencher?\s+(no|em|pro)\s+word|"
            r"cole\s+(no|em)\s+word|"
            r"substituir?\s+(no|em)\s+word|"
            r"salvar?\s+(no|em)\s+word|"
            r"microsoft\s+word)\b",
            re.IGNORECASE,
        ),
        ["word_escrever"],
    ),
    "captura_tela": (
        re.compile(
            r"\b(print(e|ar)?|screenshot|screencast|screen\s*capture|"
            r"captur(a|e|ar)\s+(a\s+)?tela|"
            r"tira(r)?\s+(um\s+)?print|"
            r"grava(r)?\s+(a\s+)?tela|"
            r"grava(r)?\s+(a\s+)?(minha\s+)?(tela|área\s+de\s+trabalho)|"
            r"gravação\s+(de\s+)?tela|"
            r"para(r)?\s+(a\s+)?grava[çc][ãa]o|"
            r"para(r)?\s+de\s+gravar|"
            r"salvar\s+print|"
            r"capturar\s+regi[ãa]o|"
            r"print\s+da\s+janela|"
            r"v[íi]deo\s+da\s+tela)\b",
            re.IGNORECASE,
        ),
        ["tirar_print", "gravar_tela", "parar_gravacao_tela"],
    ),
    # 📋 "todos" — quando o usuário pergunta quais ferramentas/skills/
    # capacidades o Jarvis tem, o modelo precisa ver O CATÁLOGO INTEIRO
    # pra poder listá-las. Sem isso, "liste todas as skills do ambiente"
    # caía no chão seguro (9 ferramentas genéricas) e o modelo inventava
    # uma lista de ferramentas que não existia de verdade no catálogo.
    "todos": (
        re.compile(
            r"\b(?:"
            r"quais\s+(?:ferramentas|skills|capacidades|habilidades"
            r"|ferramentas?\s+que\s+voc[êe]|ferramentas?\s+dispon[íi]veis)"
            r"|list(?:a|e)\s+(?:todas?\s+)?(?:as?\s+)?(?:suas?\s+)?"
            r"(?:ferramentas|skills|capacidades|habilidades"
            r"|ferramentas?\s+do\s+ambiente)"
            r"|o\s+que\s+voc[êe]\s+(?:pode|sabe)\s+fazer"
            r"|quais?\s+(?:as\s+)?(?:suas?\s+)?(?:ferramentas|capacidades)"
            r"|mostre?\s+(?:as\s+)?(?:ferramentas|skills)"
            r"|descreva?\s+(?:as\s+)?(?:suas?\s+)?(?:ferramentas)"
            r"|cat[aá]logo\s+(?:de\s+)?(?:ferramentas|skills)"
            r"|ferramentas?\s+(?:do\s+)?(?:jarvis|ambiente|sistema)"
            r")\b",
            re.IGNORECASE,
        ),
        None,  # None = todas as ferramentas (ver lógica em filtrar_tools)
    ),
}


def filtrar_tools(mensagem):
    """Devolve um subconjunto de TOOLS relevante pra mensagem. Sempre mantém
    as ferramentas de uso genérico (web, memória, arquivo básico) — esse é
    o "chão seguro". Cada categoria especializada (PDF, nuvem, navegador,
    código, execução, imagem, missão) só entra se a mensagem tiver um sinal
    claro dela. Mensagem sem nenhum sinal especializado (ex: pergunta geral
    tipo "qual a capital da França?") fica só com o chão seguro — é
    justamente o caso mais comum, e o que mais se beneficia de um catálogo
    pequeno.

    A categoria "todos" é especial: quando acionada (pergunta do tipo "quais
    ferramentas você tem?"), devolve O CATÁLOGO INTEIRO — o modelo precisa
    ver tudo pra poder listá-lo, e o chão seguro de 9 ferramentas não daria
    uma resposta completa."""
    msg = mensagem or ""
    incluidas = set(_SEMPRE_INCLUIDAS)

    for _categoria, (padrao, nomes) in _PALAVRAS_CATEGORIA.items():
        if padrao.search(msg):
            if nomes is None:
                # categoria "todos": devolve tudo
                return list(TOOLS)
            incluidas.update(nomes)

    resultado = [t for t in TOOLS if t.get("function", {}).get("name") in incluidas]
    return resultado if resultado else TOOLS
