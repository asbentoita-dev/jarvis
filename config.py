import os
import sys

# 🧠 configuração central do Jarvis Claude

# 🔑 carrega o arquivo .env (chaves de APIs externas)
# As chaves ficam SÓ aqui — nunca no código nem na memória.
#
# 📦 quando empacotado como .exe (PyInstaller), __file__ aponta para a
# pasta temporária de extração (_MEIPASS), não para a pasta do executável.
# Por isso .env, plugins/ e workspace/ usam a pasta REAL do .exe, para
# continuarem editáveis por fora sem precisar reempacotar.
if getattr(sys, "frozen", False):
    _PASTA = os.path.dirname(sys.executable)
else:
    _PASTA = os.path.dirname(os.path.abspath(__file__))

BASE_DIR = _PASTA


def _carregar_env():
    caminho = os.path.join(_PASTA, ".env")
    if not os.path.exists(caminho):
        return
    with open(caminho, "r", encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, valor = linha.split("=", 1)
            os.environ.setdefault(chave.strip(), valor.strip())


_carregar_env()


def atualizar_env(valores):
    """Escreve/atualiza chaves no .env preservando o resto do arquivo —
    usado pela interface web (ex: painel do Ollama) para salvar sem
    precisar abrir o arquivo na mão. Precisa reiniciar o Jarvis p/ aplicar."""
    caminho = os.path.join(_PASTA, ".env")
    linhas = []
    if os.path.exists(caminho):
        with open(caminho, "r", encoding="utf-8") as f:
            linhas = f.readlines()

    pendentes = dict(valores)
    saida = []
    for linha in linhas:
        bruta = linha.rstrip("\n")
        chave = (bruta.split("=", 1)[0].strip()
                 if "=" in bruta and not bruta.strip().startswith("#") else None)
        if chave and chave in pendentes:
            saida.append(f"{chave}={pendentes.pop(chave)}\n")
        else:
            saida.append(linha if linha.endswith("\n") else linha + "\n")

    for chave, valor in pendentes.items():
        saida.append(f"{chave}={valor}\n")

    with open(caminho, "w", encoding="utf-8") as f:
        f.writelines(saida)

# integrações externas (preencha no arquivo .env)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
VERCEL_TOKEN = os.environ.get("VERCEL_TOKEN", "")

# ☁️ consultoria na nuvem (Claude) — para tarefas que exigem alta qualidade
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NUVEM_MODELO = os.environ.get("NUVEM_MODELO", "claude-sonnet-5")

# 🔎 Tavily — busca de alta qualidade feita para IAs (usada pelo busca_web)
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

# 🦁 Brave Search — índice próprio, sem Google/Bing (2000 buscas/mês grátis)
# Chave em: https://api.search.brave.com/
BRAVE_SEARCH_KEY = os.environ.get("BRAVE_SEARCH_KEY", "")

# 🔍 Yandex XML API — motor russo independente
# Chave em: https://yandex.com/dev/xml/ (cadastro gratuito)
YANDEX_SEARCH_KEY = os.environ.get("YANDEX_SEARCH_KEY", "")

# ⚡ Groq — cérebros de nuvem velozes, aparecem no seletor como groq/...
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODELOS = [m.strip() for m in os.environ.get(
    "GROQ_MODELOS", "llama-3.3-70b-versatile").split(",") if m.strip()]

# 🔀 9router — roteador local/próprio de múltiplas IAs, API compatível com
# OpenAI (endpoint tipo /v1/chat/completions numa porta específica).
# Aparece no seletor como router/... Preencha ROUTER_URL com a porta certa,
# ex: http://localhost:9000/v1/chat/completions
ROUTER_URL = os.environ.get("ROUTER_URL", "")
ROUTER_API_KEY = os.environ.get("ROUTER_API_KEY", "")
ROUTER_MODELOS = [m.strip() for m in os.environ.get(
    "ROUTER_MODELOS", "").split(",") if m.strip()]

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
LMSTUDIO_URL = os.environ.get("LMSTUDIO_URL", "http://localhost:1234")
KOBOLDCPP_URL = os.environ.get("KOBOLDCPP_URL", "http://localhost:5001")

# 🧊 KoboldCpp REAL (o .exe, não o llama-cpp-python). O Jarvis sobe o
# processo do KoboldCpp com o GGUF escolhido e conversa por HTTP — é o
# mesmo motor que o usuário já usa na mão, com aceleração Vulkan na GPU
# (Radeon RX 5500 XT) e todas as flags de KV/Flash/offload de verdade.
# NÃO usar "koboldcpp.exe" (build CUDA) — nesta máquina (AMD) o certo é
# o build Vulkan: "koboldcpp-nocuda.exe".
KOBOLDCPP_EXE = os.environ.get(
    "KOBOLDCPP_EXE", r"C:\Users\Casa\koboldcpp-nocuda.exe")
# porta do servidor do KoboldCpp (o default dele é 5001; bate com KOBOLDCPP_URL)
KOBOLDCPP_PORT = int(os.environ.get("KOBOLDCPP_PORT", "5001"))
# quantização do KV cache aceita pelo KoboldCpp (--quantkv). Lista real do
# 1.123 (confirmada no --help): f16, bf16, q8_0, q5_1, q4_0. Outros tipos
# (q2_0, iq*, tq*, mxfp4) são de PESO, não de KV — não existem aqui.
KOBOLDCPP_KV_VALIDOS = ("f16", "bf16", "q8_0", "q5_1", "q4_0")

# Backend local ativo: "ollama", "lmstudio" ou "koboldcpp" — troca ao vivo pela interface,
# sem precisar reiniciar. Afeta listar modelos, chat, e status.
# koboldcpp sobe o koboldcpp.exe real (Vulkan) e conversa por HTTP.
SERVIDOR_LOCAL = os.environ.get("SERVIDOR_LOCAL", "ollama")

# IMPORTANTE: o modelo precisa suportar function calling (tools).
# llama3 NÃO suporta. Use: llama3.1, llama3.2, qwen2.5, mistral-nemo...
MODELO = os.environ.get("JARVIS_MODELO", "llama3.1")

# ChromaDB (memória vetorial) — mesma porta do seu Jarvis original
CHROMA_HOST = "localhost"
CHROMA_PORT = 8100

# SearxNG local (se estiver rodando); senão usa DuckDuckGo como fallback
SEARX_URL = "http://localhost:8080/search"

# serviço de geração de imagens (sd-api, Stable Diffusion 3.5)
SD_API_URL = "http://localhost:8200"

# 🎬 ffmpeg — usado para extrair 1 frame de vídeos anexados no chat (pro
# modelo com visão conseguir "ver" o vídeo). Se o ffmpeg já estiver no
# PATH do Windows, pode deixar em branco (detecção automática). Senão,
# aponte aqui pro ffmpeg.exe direto — ex, se você baixou o
# "ffmpeg-8.1.1-full_build" e só descompactou sem adicionar ao PATH:
# FFMPEG_PATH=C:\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "")

# liga/desliga o processamento de vídeo anexado (extração de frame via
# ffmpeg) sem precisar desinstalar nada — controlável pelo switch da
# interface (aba Sistema do menu Configurações).
VIDEO_ATIVO = os.environ.get("VIDEO_ATIVO", "1") != "0"

# ⏱️ tempo máximo (segundos) esperando a resposta de um modelo antes de
# desistir com erro de timeout. Reduzido de 900s (15 min) pra 120s em
# 19/08/2026 — 15 min deixava o usuário esperando demais sem feedback
# quando um provedor (ex: Gemini/9router) travava. Se uma tarefa
# legítima e demorada (apostila longa, missão multiagente) começar a
# estourar esse limite, ajuste via .env em vez de mudar aqui direto.
TIMEOUT_RESPOSTA = int(os.environ.get("TIMEOUT_RESPOSTA", "120"))

# ⏱️ mesma ideia, só que pra chamada LOCAL (Ollama) — separado do
# TIMEOUT_RESPOSTA de cima porque os dois cenários são bem diferentes:
# um cérebro de NUVEM travar 120s já é sinal de problema real (provedor
# fora do ar); já um modelo LOCAL numa GPU sem suporte oficial (ex: AMD
# RDNA1/gfx1012, RX 5500 XT — não está na lista de GPUs suportadas do
# Ollama no Windows) pode legitimamente demorar mais que isso na carga
# inicial, sem que nada esteja quebrado. Maior por padrão (10 min) só
# pro caminho local; cloud continua com o limite curto de sempre.
TIMEOUT_RESPOSTA_LOCAL = int(os.environ.get("TIMEOUT_RESPOSTA_LOCAL", "600"))

# 🎨 imagens automáticas: liga sozinho ao abrir o Jarvis, mas continua
# pausando sozinho quando um modelo LOCAL entra em uso (a GPU de 8GB não
# aguenta Ollama + Stable Diffusion ao mesmo tempo) — e volta a ligar
# sozinho ao trocar pra um cérebro de nuvem. Defina IMAGENS_AUTO=0 no
# .env para desligar esse comportamento e voltar ao clique manual.
IMAGENS_AUTO = os.environ.get("IMAGENS_AUTO", "1") != "0"

# 🔀 modelo automático: o Jarvis troca sozinho de modelo LOCAL conforme o
# tipo de pedido (imagem anexada → modelo com visão, pedido de código →
# modelo especialista em código, etc.), sem precisar mexer no seletor na
# mão. Só atua quando o modelo ativo é local (Ollama) — nunca troca um
# cérebro de nuvem escolhido manualmente, já que essa escolha costuma ser
# deliberada (custo/qualidade). Defina AUTO_MODELO_ATIVO=0 no .env pra
# desligar e voltar à troca 100% manual.
AUTO_MODELO_ATIVO = os.environ.get("AUTO_MODELO_ATIVO", "1") != "0"

# 🏭 Linha de Produção: classifica a tarefa automaticamente
# (SIMPLES/MEDIA/COMPLEXA) e roteia pro fluxo certo:
#   SIMPLES  → loop normal (executar → responder)
#   MEDIA    → loop + verificação pós-execução
#   COMPLEXA → executar_missao automático (planejar→executar→revisar)
# Inclui anti-preguiça: detecta quando o modelo para no meio e empurra
# de volta. Defina LINHA_PRODUCAO_ATIVO=0 no .env pra desligar.
LINHA_PRODUCAO_ATIVO = os.environ.get("LINHA_PRODUCAO_ATIVO", "1") != "0"

# pasta onde as ferramentas criam/leem arquivos
WORKSPACE = os.path.join(_PASTA, "workspace")

# 📂 pastas EXTRAS que o Jarvis pode ler/listar (além do workspace).
# Adicione aqui as que quiser liberar — uma por linha.
PASTAS_PERMITIDAS = [
    r"C:\IA\Jarvis",
    r"C:\Users\Casa\Downloads\Claude_Auto",
]

# 🧊 Pastas de modelos GGUF para o backend KoboldCpp.
# O Jarvis busca arquivos .gguf recursivamente em todas essas pastas e
# sobe o koboldcpp.exe apontando pro arquivo escolhido.
# ⚠️ O Ollama NÃO guarda .gguf solto (usa blobs + manifest), por isso não
# adianta apontar pra pasta .ollama\models — ela nunca vai render GGUF.
# Formato: lista de caminhos absolutos.
PASTAS_MODELOS_GGUF = [
    r"C:\Users\Casa\.lmstudio\models",  # LM Studio (GGUF de verdade)
    # Adicione aqui pastas extras com .gguf:
    # r"D:\Modelos\GGUF",
    # r"C:\IA\Jarvis\Modelos",
]

# limite de rodadas do loop agêntico (evita loop infinito)
# Aumentado de 8 para 12 em 22/09/2026 — tarefas complexas precisam
# de mais iterações para completar sem cortar no meio.
MAX_ITERACOES = 12

# 👤 perfil fixo do usuário (entra em todo prompt, sem depender de memória)
PERFIL_USUARIO = """Fatos confirmados sobre o usuário:
- Nome: Bento
- Diretório principal de trabalho: C:\\IA\\Jarvis
- Sistema: Windows
- Projeto principal: Claude_Auto"""

# 🧹 política de faxina da memória de longo prazo
FAXINA_DIAS = 0        # 0 = desligado; >0 apaga memórias mais velhas que N dias
MAX_MEMORIAS = 50000   # teto de memórias; passou disso, apaga as mais antigas

# 💾 backup automático da memória (ChromaDB) — cópia periódica pro HD
# deste PC, dentro da própria pasta do Jarvis (sobrevive a reinstalação
# do app, já que fica FORA do que o PyInstaller empacota). O Jarvis não
# sabe o caminho físico onde o servidor Chroma persiste os dados (ele só
# fala com o Chroma pela API HTTP em CHROMA_HOST:CHROMA_PORT) — por isso
# o backup é feito exportando o conteúdo das coleções via API, não
# copiando uma pasta específica do disco.
BACKUP_MEMORIA_DIR = os.path.join(_PASTA, "backups_memoria")
BACKUP_MEMORIA_INTERVALO_HORAS = 6     # de quanto em quanto tempo faz um backup novo
BACKUP_MEMORIA_MAX_ARQUIVOS = 30       # mantém só os N backups mais recentes (rotação)
