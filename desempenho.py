# ⚙️ Modo desempenho: quando um modelo LOCAL (Ollama) está em uso, nada
# do próprio Jarvis deve competir por CPU/GPU com ele. Quando o cérebro
# ativo é de nuvem (Groq, 9router, MCP não usa GPU local mesmo), não há
# esse problema — os recursos da máquina ficam livres.
#
# Ligado automaticamente toda vez que o modelo muda (via
# llm.registrar_callback_troca), sem precisar de nenhuma ação manual.

import atexit
import json
import os
import subprocess
import sys
import time

try:
    import psutil
except ImportError:
    psutil = None

try:
    import requests
except ImportError:
    requests = None

import config

# funções chamadas quando um modelo LOCAL entra em uso (ex: pausar SD-API)
_pausadores_gpu = []
# funções chamadas quando volta para um cérebro de nuvem (opcional)
_retomadores_gpu = []

_prioridade_baixa_atual = None  # evita mexer na prioridade sem necessidade

# 🧹 mesmo raciocínio do ChromaDB em web.py: antes o SD-API subia numa
# janela preta separada ("start", shell=True) sem vínculo nenhum com o
# processo principal — fechar o Jarvis não fechava ela, e cada janela
# nova roubava o foco bem na hora de clicar em algo. Agora sobe escondido
# (sem janela), log vai pra arquivo, e fica registrado pra ser encerrado
# junto com o Jarvis.
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_sd_api_proc = None


def _encerrar_sd_api_no_saida():
    global _sd_api_proc
    if _sd_api_proc and _sd_api_proc.poll() is None:
        try:
            _sd_api_proc.terminate()
        except Exception:
            pass


atexit.register(_encerrar_sd_api_no_saida)


def registrar_pausador_gpu(func):
    _pausadores_gpu.append(func)


def registrar_retomador_gpu(func):
    _retomadores_gpu.append(func)


def _definir_prioridade(baixa):
    global _prioridade_baixa_atual
    if _prioridade_baixa_atual == baixa:
        return  # já está no estado certo
    if not psutil:
        return
    try:
        p = psutil.Process()
        if sys.platform == "win32":
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if baixa
                   else psutil.NORMAL_PRIORITY_CLASS)
        else:
            p.nice(10 if baixa else 0)
        _prioridade_baixa_atual = baixa
    except Exception:
        pass


def notificar_troca_modelo(nome_modelo, eh_local):
    """Chamado sempre que o cérebro ativo muda (ver llm.definir_modelo)."""
    if eh_local:
        _definir_prioridade(baixa=True)
        for f in _pausadores_gpu:
            try:
                f()
            except Exception:
                pass
        print(f"[DESEMPENHO] modelo local '{nome_modelo}' em uso — "
              f"prioridade do Jarvis reduzida e GPU liberada para o Ollama.")
    else:
        _definir_prioridade(baixa=False)
        for f in _retomadores_gpu:
            try:
                f()
            except Exception:
                pass


def sd_api_online():
    """Health-check rápido e não-destrutivo (não inicia nada)."""
    if not requests:
        return False
    try:
        return requests.get(f"{config.SD_API_URL}/health", timeout=1.5).ok
    except Exception:
        return False


def iniciar_sd_api_se_preciso():
    """Sobe o serviço SD-API (geração de imagens) automaticamente se ainda
    não estiver rodando. Usado tanto no boot do Jarvis (web.py) quanto sob
    demanda quando alguém pede pra gerar imagem e o serviço está pausado
    (ver tools.gerar_imagem) — mesma função, um lugar só, sem duplicar a
    lógica de quoting do subprocess em dois arquivos.

    ⚠️ GPU: o SD-API usa a mesma GPU que o Ollama local (SDXL-Turbo via
    DirectML). Numa placa de 8GB, rodar os dois ao mesmo tempo com um
    modelo local grande pode disputar VRAM e deixar tudo lento (ou dar
    OOM). Com cérebro de nuvem (Groq, 9router) não tem esse problema,
    já que não usa GPU local nenhuma."""
    global _sd_api_proc

    if sd_api_online():
        print("[SD-API] já estava online.")
        return True

    sd_dir = os.path.join(os.path.dirname(config.BASE_DIR), "sd-api")
    python_venv = os.path.join(sd_dir, "venv", "Scripts", "python.exe")
    script = os.path.join(sd_dir, "sd_api.py")
    if not os.path.isfile(python_venv) or not os.path.isfile(script):
        print(f"[SD-API] não achei o venv/script em {sd_dir} — pulei o "
              f"auto-start. Rode SD-API.bat na mão se quiser geração de "
              f"imagens (ele avisa se faltar instalar o venv).")
        return False

    try:
        log_path = os.path.join(sd_dir, "sd-api.log")
        log_file = open(log_path, "a", encoding="utf-8")
        _sd_api_proc = subprocess.Popen(
            [python_venv, script],
            cwd=sd_dir,
            stdout=log_file, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
        )
        print(f"[SD-API] iniciando em segundo plano, sem janela (log em "
              f"{log_path}) — pode levar ~1 min pra carregar o modelo na "
              f"1ª vez.")
        return True
    except Exception as e:
        print(f"[SD-API] falha ao iniciar automaticamente: {e} "
              f"— rode SD-API.bat na mão se precisar.")
        return False


def esperar_sd_api_pronto(timeout_s=90, intervalo_s=3):
    """Bloqueia até o SD-API responder /health ou o timeout estourar.
    Usado por tools.gerar_imagem depois de disparar o auto-start, pra não
    devolver erro pro usuário só porque o serviço ainda estava subindo."""
    limite = time.monotonic() + timeout_s
    while time.monotonic() < limite:
        if sd_api_online():
            return True
        time.sleep(intervalo_s)
    return False


# 📊 VRAM LIVRE AGORA + SUGESTÃO DE CAMADAS ("num_gpu") — pra tela de
# Informações do Modelo / aba Model mostrarem quanto espaço tem de
# verdade AGORA (a VRAM disponível muda a cada instante conforme outros
# programas abrem/fecham, não é fixa por modelo), em vez do usuário
# ter que adivinhar um valor de camadas na mão.
#
# ⚠️ Vendor-agnóstico de propósito: usa os contadores de desempenho do
# PRÓPRIO WINDOWS (a mesma fonte que o Gerenciador de Tarefas usa pra
# desenhar o gráfico "Memória da GPU dedicada"), não uma SDK de
# fabricante — funciona pra AMD, Intel ou NVIDIA igual, importante aqui
# porque a GPU é uma AMD RX 5500 XT sem NVML/nvidia-smi disponível.
#
# "Melhor esforço": nomes de contador de desempenho no Windows podem
# variar por versão/driver, e Get-Counter às vezes falha silenciosamente
# num sistema específico. Se falhar, devolve None — quem chama mostra
# "indisponível" na tela em vez de quebrar. Se der None na sua máquina,
# me mande o que aparece rodando isso no PowerShell:
#   Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage'
# que eu ajusto o nome do contador pro seu Windows específico.
_PS_VRAM = r'''
$ErrorActionPreference = "Stop"
$usadoBytes = $null
try {
    $amostras = (Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage' -ErrorAction Stop).CounterSamples
    $usadoBytes = ($amostras | Measure-Object -Property CookedValue -Maximum).Maximum
} catch {}

$totalBytes = $null
try {
    $classeGpu = '{4d36e968-e325-11ce-bfc1-08002be10318}'
    $chave = Get-ChildItem "HKLM:\SYSTEM\ControlSet001\Control\Class\$classeGpu" -ErrorAction Stop |
        Where-Object {
            $v = (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).'HardwareInformation.qwMemorySize'
            $v -gt 0
        } | Select-Object -First 1
    if ($chave) {
        $totalBytes = (Get-ItemProperty $chave.PSPath).'HardwareInformation.qwMemorySize'
    }
} catch {}

if (-not $totalBytes) {
    try {
        $totalBytes = (Get-CimInstance Win32_VideoController -ErrorAction Stop |
            Sort-Object AdapterRAM -Descending | Select-Object -First 1).AdapterRAM
    } catch {}
}

[PSCustomObject]@{ usado_bytes = $usadoBytes; total_bytes = $totalBytes } | ConvertTo-Json -Compress
'''


def vram_status():
    """{"usado_bytes", "total_bytes", "livre_bytes"} ou None (indisponível
    — driver/Windows não expôs os contadores esperados, ver comentário
    acima). Consulta ao vivo, sem cache — o objetivo é saber o que está
    livre NESTE instante, não um valor de minutos atrás."""
    if sys.platform != "win32":
        return None
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_VRAM],
            capture_output=True, text=True, timeout=8,
            creationflags=CREATE_NO_WINDOW,
        )
        dados = json.loads((r.stdout or "").strip())
        total = dados.get("total_bytes")
        usado = dados.get("usado_bytes")
        if not total:
            return None
        livre = max(0, total - (usado or 0))
        return {"usado_bytes": usado, "total_bytes": total, "livre_bytes": livre}
    except Exception as e:
        print(f"[DESEMPENHO] não consegui ler VRAM (contador indisponível nessa "
              f"máquina/driver): {e}")
        return None


def sugerir_num_gpu(vram_livre_bytes, n_camadas_total, tamanho_modelo_bytes, margem=0.85):
    """Sugere quantas camadas cabem na GPU com folga, dado o espaço livre
    AGORA (não um valor fixo por modelo — muda conforme outros programas
    usam a GPU). Reserva margem de segurança (15% por padrão): parte da
    VRAM some pro cache KV conforme o contexto cresce durante a conversa,
    e sobrar zero de folga foi exatamente o que causou os travamentos que
    já vimos nessa máquina. Estimativa por regra de três (tamanho do
    arquivo ÷ nº de camadas) — as camadas não são todas do mesmo
    tamanho na vida real, então isso é uma SUGESTÃO, não um valor exato."""
    if not (vram_livre_bytes and n_camadas_total and tamanho_modelo_bytes):
        return None
    bytes_por_camada = tamanho_modelo_bytes / n_camadas_total
    camadas = int((vram_livre_bytes * margem) // bytes_por_camada)
    return max(0, min(camadas, n_camadas_total))
