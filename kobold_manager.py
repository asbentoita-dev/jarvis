"""🧊 Gerenciador do KoboldCpp REAL (koboldcpp.exe).

O que muda em relação ao que existia: antes o backend "koboldcpp" do Jarvis
usava o `llama-cpp-python` embutido — que nesta máquina está compilado SÓ
para CPU (sem CUDA/Vulkan), então rodava lento e sem usar a GPU. Agora o
Jarvis SOBE O PRÓPRIO KoboldCpp (o .exe que o usuário já usa: build Vulkan,
`koboldcpp-nocuda.exe`) como processo filho, apontando pro GGUF escolhido
com as flags de verdade (--usevulkan, --gpulayers, --contextsize,
--quantkv, --noflashattention, --threads, --batchsize), e conversa por HTTP
(porta 5001). Ou seja: a performance do KoboldCpp na GPU, dentro do Jarvis.

Responsabilidades: montar o comando, subir/derrubar o processo, detectar
prontidão, e reportar status (modelo carregado, VRAM). O chat em si fica em
llm.py (via `_chat_openai_compat` na API compatível com OpenAI do KoboldCpp).
"""

import os
import subprocess
import sys
import threading
import time

import requests

import config
import gguf_info

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_proc = None
_carregado = None          # {"gguf":..., "params":{...}} do que está no ar
_lock = threading.RLock()
_log_path = os.path.join(config.BASE_DIR, "koboldcpp.log")


# ─────────────────────────────────────────────────────────────
# endpoints / porta
# ─────────────────────────────────────────────────────────────
def _url(path=""):
    base = (config.KOBOLDCPP_URL or "http://localhost:5001").rstrip("/")
    return base + path


def porta():
    try:
        return config.KOBOLDCPP_PORT
    except Exception:
        return 5001


def _matar_arvore(pid):
    """Mata o processo E todos os filhos. O KoboldCpp tem um launcher que
    sobe o engine em OUTRO processo: matar só o pai deixa o engine vivo
    segurando a VRAM (isso já aconteceu e derrubou o Ollama pra CPU)."""
    try:
        import psutil
    except Exception:
        return False
    try:
        raiz = psutil.Process(pid)
    except Exception:
        return False
    alvos = []
    try:
        alvos = raiz.children(recursive=True)
    except Exception:
        pass
    alvos.append(raiz)
    matou = False
    for p in alvos:
        try:
            p.terminate()
            matou = True
        except Exception:
            pass
    try:
        _, sobraram = psutil.wait_procs(alvos, timeout=4)
    except Exception:
        sobraram = alvos
    for p in sobraram:
        try:
            p.kill()
            matou = True
        except Exception:
            pass
    return matou


def _matar_porta():
    """Mata quem estiver escutando na porta do KoboldCpp (usado ao trocar
    de modelo/backend, pra liberar a porta e a VRAM)."""
    try:
        import psutil
    except Exception:
        return False
    achou = False
    try:
        for conn in psutil.net_connections(kind="inet"):
            if (conn.status == psutil.CONN_LISTEN and conn.laddr
                    and conn.laddr.port == porta() and conn.pid):
                if _matar_arvore(conn.pid):
                    achou = True
    except Exception:
        pass
    return achou


# ─────────────────────────────────────────────────────────────
# status
# ─────────────────────────────────────────────────────────────
def modelo_servidor():
    """Nome do modelo que o KoboldCpp tem carregado agora (ou None)."""
    try:
        r = requests.get(_url("/api/v1/model"), timeout=2)
        if r.ok:
            d = r.json() or {}
            # o KoboldCpp devolve o nome em "result" (ex: "koboldcpp/LFM2.5-...")
            return d.get("result") or d.get("model")
    except Exception:
        pass
    return None


def esta_rodando():
    proc_vivo = _proc is not None and _proc.poll() is None
    if proc_vivo:
        return True
    # pode haver um KoboldCpp aberto por fora (o usuário subiu na mão)
    return modelo_servidor() is not None


def vram_livre_bytes():
    try:
        import desempenho
        st = desempenho.vram_status() or {}
        return st.get("livre_bytes")
    except Exception:
        return None


def status():
    rodando = esta_rodando()
    return {
        "rodando": rodando,
        "modelo": modelo_servidor() if rodando else None,
        "gguf": (_carregado or {}).get("gguf"),
        "params": (_carregado or {}).get("params"),
        "url": config.KOBOLDCPP_URL,
        "porta": porta(),
        "exe": config.KOBOLDCPP_EXE,
        "exe_existe": os.path.exists(config.KOBOLDCPP_EXE),
        "vram_livre_bytes": vram_livre_bytes() if rodando else None,
        "log": _log_path,
    }


# ─────────────────────────────────────────────────────────────
# camadas de GPU (--gpulayers)
# ─────────────────────────────────────────────────────────────
def _bytes_por_elemento_kv(kv_dtype):
    return {
        "f16": 2.0, "bf16": 2.0, "q8_0": 1.05,
        "q5_1": 0.75, "q4_0": 0.6, "q4_1": 0.6,
    }.get((kv_dtype or "f16"), 2.0)


def calcular_gpulayers(caminho_gguf, n_ctx, kv_dtype, livre_bytes, info=None):
    """Quantas camadas dá pra jogar na GPU com folga. Estimativa
    conservadora (margem de 15%) pra não estourar a VRAM de 8GB e derrubar
    o carregamento. Se não der pra estimar, devolve None (aí usamos todas)."""
    if not livre_bytes or livre_bytes <= 0:
        return None
    info = info or gguf_info.ler_metadados(caminho_gguf)
    n_camadas = info.get("n_camadas") or 32
    modelo_bytes = info.get("tamanho_bytes") or 0
    if not modelo_bytes:
        return None

    emb = info.get("embedding") or 4096
    # KV ≈ ctx * camadas * emb * 2 (K e V) * bytes_por_elemento
    kv_bytes = n_ctx * n_camadas * emb * 2 * _bytes_por_elemento_kv(kv_dtype)
    # buffers de compute/reserva do llama.cpp
    reserva = 512 * 1024 * 1024
    usavel = livre_bytes * 0.85

    disponivel_pro_modelo = usavel - kv_bytes - reserva
    if disponivel_pro_modelo <= 0:
        # nem o KV + reserva cabem: manda só o essencial (deixa o
        # KoboldCpp/llama.cpp decidir e não force OOM)
        return 0

    # custo por camada: divide o modelo pelo nº de camadas + a "camada"
    # extra (embeddings/output, ~1 camada de tamanho médio)
    por_camada = modelo_bytes / max(1, n_camadas + 1)
    n = int(disponivel_pro_modelo // por_camada)
    return max(0, min(n, n_camadas))


def _resolver_gpulayers(caminho_gguf, params):
    if params.get("low_vram"):
        return 0
    num_gpu = params.get("num_gpu")
    try:
        num_gpu = int(num_gpu) if num_gpu is not None else None
    except Exception:
        num_gpu = None
    # info do GGUF: serve pra NÃO pedir mais camadas do que o modelo tem
    # (o model_params.json global veio do Ollama e traz num_gpu=40, mas o
    # LFM2.5 tem 24 camadas; pedir 40 faz o KoboldCpp reservar sem caber)
    info = {}
    try:
        info = gguf_info.ler_metadados(caminho_gguf) or {}
    except Exception:
        info = {}
    camadas = info.get("n_camadas")
    if num_gpu is not None and num_gpu >= 0:
        limite = num_gpu
        if camadas:
            limite = min(limite, int(camadas))
        # se nem a VRAM livre cabe o suficiente, cede pro ajuste automático
        # (rodar com camadas a mais que a VRAM suporta dá mais lentidão do
        # que rodar com menos: testado, 40 camadas pedidas = 5,5 tok/s)
        n_ctx = params.get("num_ctx") or 8192
        kv = params.get("quant_kv_cache") or "f16"
        cabe = calcular_gpulayers(caminho_gguf, n_ctx, kv, vram_livre_bytes())
        if cabe is not None:
            return min(limite, int(cabe))
        return limite
    # autom�tico
    n_ctx = params.get("num_ctx") or 8192
    kv = params.get("quant_kv_cache") or "f16"
    n = calcular_gpulayers(caminho_gguf, n_ctx, kv, vram_livre_bytes())
    if n is None:
        return camadas or 999
    return n


def liberar_vram_ollama(timeout=6):
    """Descarrega os modelos que o Ollama deixou residentes na VRAM.
    O KoboldCpp e o Ollama dividem a mesma GPU (8 GB aqui): se o Ollama
    segura um modelo parado, o KoboldCpp perde camadas e cai pra CPU.
    Melhor esforço: falhou, segue o jogo (o ajuste de camadas compensa)."""
    try:
        import requests
        base = (getattr(config, "OLLAMA_URL", "") or "http://localhost:11434").rstrip("/")
        ps = requests.get(f"{base}/api/ps", timeout=timeout).json() or {}
        for m in (ps.get("models") or []):
            nome = m.get("name") or m.get("model")
            if not nome:
                continue
            try:
                requests.post(f"{base}/api/generate",
                              json={"model": nome, "keep_alive": 0},
                              timeout=timeout)
            except Exception:
                pass
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# montar comando / subir / derrubar
# ─────────────────────────────────────────────────────────────
def _montar_argv(caminho_gguf, params):
    exe = config.KOBOLDCPP_EXE
    n_ctx = params.get("num_ctx") or 8192
    # gpu_backend_autofit (ligado no model_params.json, default True) =>
    # o PRÓPRIO KoboldCpp calcula as camadas com a VRAM que ele enxerga.
    # Bem mais confiável que a nossa estimativa, que lia um contador de
    # VRAM via PowerShell e oscilava entre 3 e 15 camadas na mesma
    # máquina, reiniciando o motor atoa entre uma chamada e outra.
    autofit = bool(params.get("gpu_backend_autofit", True))
    ngl = None if autofit else _resolver_gpulayers(caminho_gguf, params)

    argv = [exe, "--model", caminho_gguf, "--usevulkan",
            "--contextsize", str(int(n_ctx))]
    if autofit:
        argv.append("--autofit")
    else:
        argv += ["--gpulayers", str(int(ngl))]
    argv += ["--host", "127.0.0.1", "--port", str(porta()),
             "--skiplauncher"]

    kv = (params.get("quant_kv_cache") or "").strip().lower()
    if kv and kv in config.KOBOLDCPP_KV_VALIDOS and kv != "f16":
        argv += ["--quantkv", kv]

    if not params.get("flash_attention", True):
        argv.append("--noflashattention")

    # --jinja: usa o chat_template EMBUTIDO no GGUF em vez do
    # "universal formatting" do KoboldCpp, e habilita os tool calls do
    # template (o agente manda as ferramentas como texto na fase de
    # execução, mas o Jinja fica pronto pro que vier). SEM esse flag o
    # LFM2.5 respondia como se estivesse continuando um documento
    # (testado: "The user asks: ..." e resposta nenhuma).
    if params.get("jinja", True):
        argv.append("--jinja")

    # pensamento: o model_params.json tem pensar_modo ("nunca"/"sempre"),
    # mapeado direto pro --jinjathink do KoboldCpp. Com "nunca" o modelo
    # vai direto pra resposta em vez de gastar 200+ tokens raciocinando.
    modo = str(params.get("pensar_modo") or "").strip().lower()
    if modo == "nunca":
        argv += ["--jinjathink", "false"]
    elif modo == "sempre":
        argv += ["--jinjathink", "true"]

    threads = params.get("num_thread")
    if threads:
        argv += ["--threads", str(int(threads))]

    batch = params.get("batch_size")
    if batch:
        argv += ["--batchsize", str(int(batch))]

    mcpfile = os.path.join(config.BASE_DIR, "kobold_mcp.json")
    tem_mcpfile = os.path.exists(mcpfile)
    if tem_mcpfile:
        argv += ["--mcpfile", mcpfile]

    return argv, {"n_ctx": int(n_ctx),
                  "n_gpu_layers": (None if autofit else int(ngl)),
                  "autofit": bool(autofit),
                  "quantkv": kv or "f16",
                  "flash_attention": bool(params.get("flash_attention", True)),
                  "jinja": bool(params.get("jinja", True)),
                  "jinjathink": ("false" if modo == "nunca"
                                 else "true" if modo == "sempre" else "default"),
                  "threads": threads, "batch_size": batch,
                  "mcpfile": (mcpfile if tem_mcpfile else None)}


def _chave_requisicao(params):
    """O que o chamador PEDEU (não o que a VRAM livre da hora permitiu).
    Usada pra decidir se dá pra reaproveitar o motor que já está no ar:
    comparar os parâmetros calculados fazia a contagem de camadas oscilar
    (VRAM livre muda a cada consulta) e reiniciar o motor a CHAMADA,
    custando ~25s de recarga sem necessidade nenhuma."""
    chaves = ("num_ctx", "num_gpu", "quant_kv_cache", "flash_attention",
              "num_thread", "batch_size", "low_vram", "pensar_modo",
              "jinja", "gpu_backend_autofit")
    return {k: params.get(k) for k in chaves}


def _mesma_config(caminho_gguf, params_resolvidos):
    if not _carregado:
        return False
    return (_carregado.get("gguf") == caminho_gguf
            and _carregado.get("params") == params_resolvidos)


def aguardar_pronto(timeout=180):
    inicio = time.time()
    while time.time() - inicio < timeout:
        if _proc is not None and _proc.poll() is not None:
            return False  # o processo morreu
        try:
            r = requests.get(_url("/api/v1/model"), timeout=3)
            if r.ok:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def iniciar(caminho_gguf, params=None, esperar=True, timeout=180):
    """Sobe o KoboldCpp com o GGUF dado. Se já estiver rodando com a MESMA
    config, reaproveita. Devolve (ok, mensagem)."""
    global _proc, _carregado
    params = dict(params or {})

    if not os.path.exists(config.KOBOLDCPP_EXE):
        return False, (f"koboldcpp.exe não encontrado em '{config.KOBOLDCPP_EXE}'. "
                       f"Ajuste KOBOLDCPP_EXE no .env.")
    if not os.path.exists(caminho_gguf):
        return False, f"arquivo GGUF não encontrado: {caminho_gguf}"

    with _lock:
        # já está no ar com o MESMO pedido do chamador? reaproveita sem
        # recalcular camadas (a VRAM livre oscilava e reiniciava o motor)
        if (esta_rodando() and _carregado
                and _carregado.get("gguf") == caminho_gguf
                and _carregado.get("req") == _chave_requisicao(params)):
            return True, "já estava carregado com a mesma configuração"

        # derruba o que estiver no ar (nosso ou de fora) pra liberar porta/VRAM
        parar()

        # libera a VRAM que o Ollama deixou parada e dá um tempo pro driver
        # devolver a do processo que morreu, ANTES de medir e escolher camadas
        liberar_vram_ollama()
        time.sleep(1.5)

        argv, resolvidos = _montar_argv(caminho_gguf, params)

        print(f"[KOBOLDCPP] subindo: {' '.join(argv)}")
        log = open(_log_path, "a", encoding="utf-8", errors="replace")
        log.write(f"\n\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n{argv}\n")
        log.flush()
        try:
            _proc = subprocess.Popen(
                argv, cwd=config.BASE_DIR,
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:
            return False, f"falha ao iniciar o KoboldCpp: {e}"

        _carregado = {"gguf": caminho_gguf, "params": resolvidos,
                      "req": _chave_requisicao(params)}

        if not esperar:
            return True, "iniciando em segundo plano"

        pronto = aguardar_pronto(timeout=timeout)
        if not pronto:
            codigo = _proc.poll() if _proc else "?"
            parar()
            return False, (f"o KoboldCpp não ficou pronto em {timeout}s "
                           f"(código de saída: {codigo}). Veja o log: {_log_path}")
        if resolvidos.get("autofit"):
            return True, "pronto (camadas na GPU decididas pelo autofit)"
        return True, f"pronto ({resolvidos['n_gpu_layers']} camadas na GPU)"


def parar():
    """Derruba o KoboldCpp GERENCIADO por nós (e, se sobrar, quem estiver
    na porta). Não mexe em nada se não houver nada rodando."""
    global _proc, _carregado
    with _lock:
        matou = False
        if _proc is not None and _proc.poll() is None:
            try:
                # árvore inteira: launcher + engine (filho segura a VRAM)
                if _matar_arvore(_proc.pid):
                    matou = True
            except Exception:
                pass
        _proc = None
        _carregado = None
        # limpa qualquer resto na porta (ex: um KoboldCpp aberto por fora)
        if _matar_porta():
            matou = True
            time.sleep(0.5)
        return matou


def garantir(caminho_gguf, params=None, esperar=True):
    """Sobe só se necessário (modelo/config diferentes do que está no ar)."""
    return iniciar(caminho_gguf, params, esperar=esperar)


if __name__ == "__main__":
    # teste rápido pela linha de comando
    import sys as _sys
    if len(_sys.argv) >= 2:
        alvo = _sys.argv[1]
        if alvo == "parar":
            print("parado:", parar())
        else:
            print(iniciar(alvo))
    else:
        print(status())
