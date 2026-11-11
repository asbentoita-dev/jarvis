import atexit
import io
import json
import queue
import signal
import subprocess
import sys
import threading
import time

import re
import requests
from flask import Flask, request, jsonify, send_from_directory, send_file, Response, stream_with_context
from agent import processar_mensagem, pedir_parada, _encontrar_ffmpeg
import llm
import config
from config import WORKSPACE, SD_API_URL
import memory
import historico
import psutil
import os
import cerebros
import mcp_config
import pastas_config
import model_params
import desempenho
import exportar
from tools import listar_plugins, definir_plugin, ler_arquivo, executar_ferramenta
from urllib.parse import urlparse

# ══════════════════════════════════════════════════════════════════
# 🚀 ABRIDOR DE APPS — interceptação direta (antes do LLM)
# ══════════════════════════════════════════════════════════════════
try:
    from abridor_apps import interceptar_abrir
    ABRIDOR_DISPONIVEL = True
except Exception as _e:
    ABRIDOR_DISPONIVEL = False
    interceptar_abrir = None
    print(f"[ABRIDOR] Não carregado: {_e}")

# 🧠 JARVIS NEXT GEN — Interface Premium
# Design inspirado em: ChatGPT Desktop, Claude, Cursor, Linear, Vercel, Arc Browser

app = Flask(__name__)

historico_curto = []
sessao_atual = historico.criar_sessao()

# 🧭 MODO PLANEJAMENTO por sessão: {sessao_id: True/False}. Enquanto True
# pra sessão ativa, o Jarvis só conversa em texto (sem tools/thinking) até
# o usuário confirmar com uma palavra-gatilho — ver agent.PADRAO_CONFIRMACAO.
# Fica só em memória de propósito (não precisa sobreviver a um restart).
MODO_PLANEJAMENTO = {}

# 📏 teto do contexto curto passado ao modelo a cada /chat — evita que uma
# conversa longa acumule tokens sem limite (o histórico completo continua
# salvo em disco via historico.py, isso só limita o que vai no prompt).
MAX_HISTORICO_CURTO = 30

# ─────────────────────────────────────────
# 🧹 CICLO DE VIDA DOS PROCESSOS FILHOS — antes, ChromaDB/SD-API abriam
# numa janelinha preta separada (via "start", shell=True) que não tinha
# NENHUM vínculo com o processo principal do Jarvis: fechar o Jarvis não
# fechava elas, e cada nova sessão criava outra janela do zero — daí a
# fila de python.exe acumulando no Gerenciador de Tarefas, e cada janela
# nova roubando o foco do mouse bem na hora de clicar em algo. Agora eles
# sobem ESCONDIDOS (sem janela nenhuma pra roubar foco), com os logs
# indo pra um arquivo .log em vez de sumir, e ficam registrados aqui pra
# serem encerrados de verdade junto com o Jarvis (fechar normal, Ctrl+C
# ou até "Finalizar tarefa" no processo principal).
# ─────────────────────────────────────────

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_PROCESSOS_FILHOS = []

def _registrar_processo_filho(proc):
    if proc:
        _PROCESSOS_FILHOS.append(proc)

def _encerrar_processos_filhos():
    for proc in _PROCESSOS_FILHOS:
        if proc.poll() is not None:
            continue  # já morreu sozinho
        try:
            proc.terminate()
        except Exception:
            pass
    # dá um segundo pra sair na boa antes de forçar
    time.sleep(1)
    for proc in _PROCESSOS_FILHOS:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

atexit.register(_encerrar_processos_filhos)

def _handler_sinal(signum, frame):
    # SIGTERM não roda atexit por padrão no Python — força um sys.exit()
    # limpo pra garantir que _encerrar_processos_filhos() sempre execute.
    sys.exit(0)

try:
    signal.signal(signal.SIGTERM, _handler_sinal)
    signal.signal(signal.SIGINT, _handler_sinal)
except Exception:
    pass  # alguns ambientes (thread secundária, etc.) não deixam registrar

def _iniciar_chromadb_se_preciso():
    """Sobe o ChromaDB (memória de longo prazo) automaticamente se ainda
    não estiver rodando. Os dados ficam em BASE_DIR/chroma_data
    (persistem entre execuções).

    BUG GRAVE corrigido em 14/08/2026: isso rodava
    subprocess.Popen([sys.executable, "-m", "chromadb.cli.cli", "run", ...]).
    Em modo dev sys.executable é o python.exe de verdade, então "-m"
    funciona. Mas no .exe empacotado, sys.executable É O PRÓPRIO
    Jarvis.exe inteiro — "-m" não significa nada pra ele, então esse
    Popen na prática abria OUTRA CÓPIA COMPLETA do Jarvis (bandeja,
    Flask, preparo do Ollama, e essa cópia tentando subir o ChromaDB
    de novo do mesmo jeito quebrado — uma cascata). Isso explica CPU/
    RAM/VRAM disparando ao abrir o Jarvis sem motivo aparente.
    Correção: no .exe empacotado, chama o servidor do ChromaDB direto
    dentro do processo atual (numa thread), sem subprocess nenhum."""
    try:
        import chromadb
        chromadb.HttpClient(host=config.CHROMA_HOST, port=config.CHROMA_PORT).heartbeat()
        print("[CHROMADB] já estava online.")
        return
    except Exception:
        pass

    dados_dir = os.path.join(config.BASE_DIR, "chroma_data")
    os.makedirs(dados_dir, exist_ok=True)
    log_path = os.path.join(dados_dir, "chromadb.log")

    if getattr(sys, "frozen", False):
        def _rodar_dentro_do_processo():
            try:
                import chromadb_rust_bindings
                with open(log_path, "a", encoding="utf-8") as log_file:
                    velho_out, velho_err = sys.stdout, sys.stderr
                    sys.stdout = sys.stderr = log_file
                    try:
                        chromadb_rust_bindings.cli([
                            "chroma", "run",
                            "--path", dados_dir,
                            "--port", str(config.CHROMA_PORT),
                        ])
                    finally:
                        sys.stdout, sys.stderr = velho_out, velho_err
            except Exception as e:
                print(f"[CHROMADB] falha ao iniciar embutido: {e} — memória "
                      f"de longo prazo ficará indisponível nesta sessão.")

        threading.Thread(target=_rodar_dentro_do_processo, daemon=True).start()
        print(f"[CHROMADB] iniciando embutido no processo atual (porta "
              f"{config.CHROMA_PORT}, log em {log_path}).")
        return

    try:
        log_file = open(log_path, "a", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "chromadb.cli.cli", "run",
             "--path", dados_dir, "--port", str(config.CHROMA_PORT)],
            cwd=config.BASE_DIR,
            stdout=log_file, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
        )
        _registrar_processo_filho(proc)
        print(f"[CHROMADB] iniciando em segundo plano, sem janela (porta "
              f"{config.CHROMA_PORT}, log em {log_path}) — pode levar "
              f"alguns segundos pra ficar pronto.")
    except Exception as e:
        print(f"[CHROMADB] falha ao iniciar automaticamente: {e} — rode "
              f"'chroma run --path chroma_data --port {config.CHROMA_PORT}' "
              f"na mão se precisar de memória.")

# ─────────────────────────────────────────
# ⚙️ MODO DESEMPENHO — pausa/retoma o SD-API sozinho ao trocar de modelo,
# pra não disputar VRAM com um modelo local grande (confirmado pelo
# usuário: GLM-4.6V-Flash rodando a 3.3 tok/s com a GPU de 8GB estourando
# pra memória compartilhada, porque o SD-API tava ligado junto).
# ─────────────────────────────────────────

def _matar_processo_na_porta(porta):
    """Mata o processo escutando na porta dada. Devolve True se achou
    e matou alguma coisa."""
    try:
        for conn in psutil.net_connections(kind="inet"):
            if (conn.status == psutil.CONN_LISTEN and conn.laddr
                    and conn.laddr.port == porta and conn.pid):
                try:
                    psutil.Process(conn.pid).terminate()
                except Exception:
                    pass
                return True
    except Exception as e:
        print(f"[DESEMPENHO] não consegui listar processos da porta "
              f"{porta} ({e}) — pausa automática do SD-API pode não "
              f"funcionar nesta máquina.")
    return False

def _pausar_sd_api():
    """Chamado quando um modelo LOCAL entra em uso — libera a VRAM que
    o SD-API estava segurando, matando o processo (ele sobe de novo
    sozinho quando precisar, via _retomar_sd_api)."""
    porta = urlparse(SD_API_URL).port or 8200
    if _matar_processo_na_porta(porta):
        print("[DESEMPENHO] SD-API pausado (VRAM liberada pro modelo local).")

def _retomar_sd_api():
    """Chamado quando volta pra um cérebro de nuvem — religa o SD-API
    (ele mesmo decide não fazer nada se já estiver rodando)."""
    desempenho.iniciar_sd_api_se_preciso()

llm.registrar_callback_troca(desempenho.notificar_troca_modelo)
desempenho.registrar_pausador_gpu(_pausar_sd_api)
desempenho.registrar_retomador_gpu(_retomar_sd_api)

# estado inicial: só sobe o SD-API no boot se o modelo padrão NÃO for
# local — senão ele nasceria e morreria em seguida (ou pior, ficaria
# ligado disputando VRAM com o modelo local que já é o padrão)
if llm._eh_nuvem(llm.modelo_atual()):
    desempenho.iniciar_sd_api_se_preciso()
else:
    print("[DESEMPENHO] modelo padrão é local — SD-API fica pausado até "
          "trocar pra um cérebro de nuvem (ou gerar a 1ª imagem manualmente).")
_iniciar_chromadb_se_preciso()

print(f"Faxina da memória: {memory.faxina()}")
print(f"Histórico: {len(historico.listar_sessoes())} sessões salvas")

def get_system_stats():
    """Retorna estatísticas do sistema em tempo real."""
    return {
        "cpu": psutil.cpu_percent(interval=0.5),
        "ram": psutil.virtual_memory().percent,
        "disk": psutil.disk_usage(WORKSPACE).percent
    }

HTML = r"""<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<title>Jarvis AI</title>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=5, user-scalable=yes">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/16.3.0/lib/marked.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.4.11/purify.min.js"></script>
<style>
  :root {
    color-scheme: dark;
    /* Background — tema Dark */
    --bg-primary:    #0D0F14;
    --bg-secondary:  #111318;
    --bg-tertiary:   #13151C;
    --bg-quaternary: #16181F;

    /* Accent primário: índigo/roxo */
    --primary:       #7C6EF8;
    --primary-light: #9D91FA;
    --primary-dark:  #5A4ECC;
    --accent-blue:   #60A5FA;
    --accent-cyan:   #22D3EE;
    --accent-green:  #10B981;
    --accent-orange: #FB923C;
    --accent-pink:   #F472B6;

    /* Text */
    --text-primary:   #E8EAF0;
    --text-secondary: #9BA3B2;
    --text-tertiary:  #555D6E;

    /* Glass — escuro com borda sutil iluminada */
    --glass-bg:     rgba(255, 255, 255, 0.05);
    --glass-border: rgba(255, 255, 255, 0.09);
    --glass-blur:   20px;

    /* Glow */
    --glow-primary: 0 0 20px rgba(124, 110, 248, 0.25);
    --glow-blue:    0 0 16px rgba(96, 165, 250, 0.2);
    --glow-green:   0 0 12px rgba(16, 185, 129, 0.2);

    /* Border radius */
    --radius-sm: 10px;
    --radius-md: 16px;
    --radius-lg: 20px;
    --radius-xl: 28px;

    /* Shadows */
    --shadow-sm: 0 2px 8px rgba(0, 0, 0, 0.4);
    --shadow-md: 0 4px 20px rgba(0, 0, 0, 0.5);
    --shadow-lg: 0 8px 40px rgba(0, 0, 0, 0.6);

    /* Transitions */
    --transition-fast: 150ms cubic-bezier(0.4, 0, 0.2, 1);
    --transition-base: 250ms cubic-bezier(0.4, 0, 0.2, 1);
    --transition-slow: 350ms cubic-bezier(0.4, 0, 0.2, 1);
  }
  
  * {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
  }
  
  body {
    background: linear-gradient(135deg, var(--bg-primary) 0%, var(--bg-secondary) 25%, var(--bg-tertiary) 50%, var(--bg-quaternary) 100%);
    background-attachment: fixed;
    color: var(--text-primary);
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    height: 100vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }
  
  /* ── Layout fix — garante que o chat ocupe toda a altura e o input fique embaixo ── */
  .app-layout {
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
  }
  
  .chat-area {
    flex: 1;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  
  .messages {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    scroll-behavior: smooth;
    scrollbar-width: thin;
    scrollbar-color: var(--glass-border) transparent;
  }

  .messages::-webkit-scrollbar {
    width: 8px;
  }

  .messages::-webkit-scrollbar-track {
    background: transparent;
  }

  .messages::-webkit-scrollbar-thumb {
    background: var(--glass-border);
    border-radius: 4px;
  }

  .messages::-webkit-scrollbar-thumb:hover {
    background: rgba(255, 255, 255, 0.18);
  }
  
  /* Previne zoom indesejado em mobile/desktop */
  @media screen and (max-width: 768px) {
    html, body {
      height: 100%;
      overflow: hidden;
    }
    .messages {
      -webkit-overflow-scrolling: touch;
    }
  }

  /* Halo de fundo removido (era o "glow" colorido e animado atrás de
     tudo — principal causa de fadiga visual em sessões longas). Fica
     só o fundo neutro quase plano definido em --bg-*. */
  
  @keyframes haloFloat {
    0%, 100% { transform: translate(0, 0) rotate(0deg); }
    50% { transform: translate(2%, 2%) rotate(5deg); }
  }
  
  /* ─────────────────────────────────────────
     TOP RAIL
  ───────────────────────────────────────── */
  .top-rail {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 0 12px;
    height: 44px;
    background: rgba(17, 19, 24, 0.97);
    border-bottom: 1px solid rgba(255,255,255,0.07);
    box-shadow: 0 2px 12px rgba(0,0,0,0.4);
    flex-shrink: 0;
    position: sticky;
    top: 0;
    z-index: 10;
  }

  .rail-logo {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-right: 4px;
    text-decoration: none;
  }

  .rail-logo-img {
    width: 26px;
    height: 26px;
    object-fit: contain;
  }

  .rail-logo-text {
    font-size: 13px;
    font-weight: 700;
    color: var(--text-primary);
    white-space: nowrap;
  }

  .rail-sep {
    width: 1px;
    height: 20px;
    background: var(--glass-border);
    flex-shrink: 0;
    margin: 0 4px;
  }

  .rail-btn {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    height: 22px;
    background: rgba(255,255,255,0.06);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    font-family: inherit;
    font-size: 11px;
    font-weight: 500;
    cursor: pointer;
    white-space: nowrap;
    transition: all var(--transition-fast);
    flex-shrink: 0;
  }

  .rail-btn:hover {
    background: rgba(255,255,255,0.12);
    border-color: var(--primary);
    color: var(--text-primary);
  }

  .rail-btn.active {
    background: rgba(255,255,255,0.1);
    border-color: var(--primary);
    color: var(--text-primary);
  }

  .rail-spacer { flex: 1; }

  .rail-model-selector {
    height: 30px;
    padding: 0 10px;
    background: rgba(255,255,255,0.06);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    color: #93C5FD;
    font-family: inherit;
    font-size: 12px;
    cursor: pointer;
    max-width: 190px;
    transition: all var(--transition-base);
    color-scheme: dark;
  }

  .rail-model-selector:focus {
    outline: none;
    border-color: var(--primary);
  }

  .rail-eject {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    background: rgba(239,68,68,0.1);
    border: 1px solid rgba(239,68,68,0.3);
    border-radius: var(--radius-md);
    color: #DC2626;
    font-size: 14px;
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .rail-eject:hover { background: rgba(239,68,68,0.2); }
  .rail-eject:disabled { opacity: 0.5; cursor: default; }

  /* ── Custom model picker ── */
  .model-picker { position: relative; flex-shrink: 0; }

  .model-picker-btn {
    display: flex; align-items: center; gap: 4px;
    height: 22px; padding: 0 8px;
    background: rgba(255,255,255,0.06);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-family: inherit; font-size: 11px;
    cursor: pointer; max-width: 200px;
    transition: all var(--transition-fast);
    white-space: nowrap; overflow: hidden;
  }
  .model-picker-btn:hover { border-color: var(--primary); background: rgba(255,255,255,0.1); }
  .model-picker-btn.open { border-color: var(--primary); background: rgba(124,110,248,0.1); }

  .model-picker-dot {
    width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0;
    background: var(--text-tertiary);
  }
  .model-picker-dot.tools { background: var(--primary); box-shadow: 0 0 5px rgba(124,110,248,0.6); }

  .model-picker-name {
    flex: 1; overflow: hidden; text-overflow: ellipsis;
    color: #93C5FD; font-weight: 500;
  }
  .model-picker-arrow { color: var(--text-tertiary); font-size: 10px; flex-shrink: 0; }

  .model-picker-panel {
    display: none; position: fixed;
    top: 44px; /* altura do rail */
    background: #16181F;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-lg);
    box-shadow: 0 12px 40px rgba(0,0,0,0.7);
    z-index: 200; width: 320px;
    max-height: 480px; overflow-y: auto;
    padding: 8px 0;
  }
  .model-picker-panel.open { display: block; }
  .model-picker-panel::-webkit-scrollbar { width: 4px; }
  .model-picker-panel::-webkit-scrollbar-thumb { background: var(--glass-border); border-radius: 4px; }

  .model-picker-category {
    padding: 10px 14px 4px;
    font-size: 10px; font-weight: 700; letter-spacing: 0.08em;
    color: var(--text-tertiary); text-transform: uppercase;
  }
  .model-picker-category:not(:first-child) { border-top: 1px solid rgba(255,255,255,0.05); margin-top: 4px; }

  .model-picker-item {
    display: flex; align-items: center; gap: 10px;
    padding: 7px 14px; cursor: pointer;
    transition: background var(--transition-fast);
  }
  .model-picker-item:hover { background: rgba(255,255,255,0.06); }
  .model-picker-item.selected { background: rgba(124,110,248,0.12); border-left: 2px solid var(--primary); padding-left: 12px; }

  .model-picker-item-dot {
    width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0;
    background: var(--text-tertiary);
  }
  .model-picker-item-dot.tools { background: var(--primary); box-shadow: 0 0 4px rgba(124,110,248,0.5); }

  .model-picker-item-info { flex: 1; min-width: 0; }
  .model-picker-item-name {
    font-size: 13px; font-weight: 600; color: var(--text-primary);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .model-picker-item-desc {
    font-size: 11px; color: var(--text-tertiary); margin-top: 1px;
  }
  .model-picker-drag {
    cursor: grab; color: var(--text-tertiary); font-size: 14px;
    opacity: 0; flex-shrink: 0; user-select: none; padding: 0 2px;
    transition: opacity 0.15s; letter-spacing: -1px;
  }
  .model-picker-item:hover .model-picker-drag { opacity: 0.55; }
  .model-picker-drag:active { cursor: grabbing; }
  .model-picker-item.dragging { opacity: 0.35; background: rgba(124,110,248,0.08) !important; }
  .model-picker-item.drag-over-top { box-shadow: inset 0 2px 0 var(--primary); }
  .model-picker-item.drag-over-bottom { box-shadow: inset 0 -2px 0 var(--primary); }
  .model-picker-panel { user-select: none; }

  .rail-status-group {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 10px;
    background: rgba(255,255,255,0.05);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    height: 30px;
    flex-shrink: 0;
  }

  .rail-status-item {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    color: var(--text-tertiary);
    white-space: nowrap;
  }

  .rail-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
    background: var(--accent-green);
    box-shadow: 0 0 5px var(--accent-green);
  }

  .rail-dot.offline { background: var(--text-tertiary); box-shadow: none; }
  .rail-dot.error   { background: #EF4444; box-shadow: none; }

  .rail-gear {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    background: transparent;
    border: none;
    border-radius: var(--radius-md);
    cursor: pointer;
    transition: all var(--transition-fast);
    flex-shrink: 0;
  }

  .rail-gear:hover { background: rgba(255,255,255,0.08); }

  .rail-zoom { display: flex; align-items: center; gap: 2px; flex-shrink: 0; }
  .rail-zoom-btn {
    width: 26px; height: 26px; border: none; border-radius: var(--radius-sm);
    background: rgba(255,255,255,0.06); color: var(--text-secondary);
    cursor: pointer; font-size: 13px; font-weight: 700;
    display: flex; align-items: center; justify-content: center;
    transition: all var(--transition-fast); font-family: inherit;
  }
  .rail-zoom-btn:hover { background: rgba(255,255,255,0.13); color: var(--text-primary); }

  /* Botão de alternância Ollama/LM Studio */
  .rail-backend {
    width: 52px; height: 26px; border-radius: 13px;
    background: rgba(255,255,255,0.08); border: 1px solid var(--glass-border);
    cursor: pointer; position: relative; flex-shrink: 0;
    transition: all var(--transition-fast);
  }
  .rail-backend:hover { background: rgba(255,255,255,0.13); }
  .rail-backend-knob {
    width: 20px; height: 20px; border-radius: 50%;
    background: var(--primary); position: absolute; top: 2px; left: 2px;
    transition: left var(--transition-fast);
    box-shadow: 0 1px 3px rgba(0,0,0,0.3);
  }
  .rail-backend[data-backend="koboldcpp"] .rail-backend-knob { left: 16px; }
  .rail-backend[data-backend="lmstudio"] .rail-backend-knob { left: 30px; }

  .zoom-indicator {
    position: fixed; top: 52px; right: 14px; z-index: 9999;
    background: var(--bg-secondary); border: 1px solid var(--primary);
    border-radius: 8px; padding: 5px 14px;
    font-size: 13px; color: var(--primary); font-weight: 700;
    opacity: 0; transition: opacity 0.2s ease;
    pointer-events: none; letter-spacing: 0.5px;
  }
  .zoom-indicator.visible { opacity: 1; }

  /* ── Dropdown de histórico ── */
  .hist-dropdown {
    display: none;
    position: fixed;
    top: 44px;
    left: 0;
    width: 290px;
    max-height: 440px;
    background: var(--glass-bg);
    backdrop-filter: blur(var(--glass-blur));
    -webkit-backdrop-filter: blur(var(--glass-blur));
    border: 1px solid var(--glass-border);
    border-top: none;
    border-radius: 0 0 var(--radius-lg) var(--radius-lg);
    box-shadow: var(--shadow-lg);
    z-index: 200;
    overflow: hidden;
    flex-direction: column;
  }

  .hist-dropdown.open { display: flex; }

  .hist-dropdown-search {
    padding: 10px 12px;
    border-bottom: 1px solid var(--glass-border);
    flex-shrink: 0;
  }

  .hist-dropdown-search input {
    width: 100%;
    padding: 6px 10px;
    background: var(--bg-tertiary);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    font-family: inherit;
    font-size: 12px;
    color: var(--text-primary);
    outline: none;
  }

  .hist-dropdown-search input:focus { border-color: var(--primary); }

  .hist-dropdown-list {
    flex: 1;
    overflow-y: auto;
    padding: 6px;
  }

  .hist-dropdown-list::-webkit-scrollbar { width: 4px; }
  .hist-dropdown-list::-webkit-scrollbar-thumb { background: var(--glass-border); border-radius: 4px; }

  .history-item {
    padding: 7px 8px;
    background: var(--bg-quaternary, rgba(22, 24, 31, 0.8));
    border: 1px solid transparent;
    border-radius: var(--radius-md);
    cursor: pointer;
    transition: all var(--transition-base);
    position: relative;
    overflow: hidden;
    margin-bottom: 3px;
  }

  .history-item:hover {
    background: var(--bg-tertiary, rgba(19, 21, 28, 0.9));
    border-color: var(--glass-border);
  }

  .history-item.active {
    background: rgba(124,110,248,0.12);
    border-color: var(--primary);
  }

  .history-item-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 5px;
  }

  .history-title {
    font-size: 12px;
    font-weight: 500;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-bottom: 3px;
  }

  .history-meta {
    font-size: 11px;
    color: var(--text-tertiary);
    display: flex;
    gap: 4px;
  }

  .history-kebab {
    flex-shrink: 0;
    width: 22px;
    height: 22px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: none;
    border-radius: 4px;
    color: var(--text-tertiary);
    font-size: 15px;
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .history-kebab:hover {
    background: rgba(255,255,255,0.07);
    color: var(--text-primary);
  }

  .section-title {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text-tertiary);
    padding: 4px 8px 6px;
  }

  /* history-dropdown: menu ⋮ global das sessões */
  .history-dropdown {
    position: fixed;
    z-index: 2000;
    min-width: 130px;
    background: var(--bg-secondary, #1a120d);
    border: 1px solid var(--glass-border);
    border-radius: 6px;
    box-shadow: 0 6px 20px rgba(0,0,0,0.4);
    overflow: hidden;
    display: none;
  }

  .history-dropdown.open { display: block; }

  .history-dropdown-item {
    display: block;
    width: 100%;
    padding: 8px 10px;
    background: none;
    border: none;
    text-align: left;
    color: var(--text-secondary);
    font-size: 12px;
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .history-dropdown-item:hover {
    background: rgba(255,255,255,0.07);
    color: var(--text-primary);
  }

  .history-dropdown-item.perigo { color: #EF4444; }
  .history-dropdown-item.perigo:hover { background: rgba(239,68,68,0.12); }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }
  
  /* ─────────────────────────────────────────
     MODAIS
  ───────────────────────────────────────── */
  .modal-overlay {
    display: none;
    position: fixed;
    inset: 0;
    z-index: 1000;
    background: rgba(0,0,0,0.45);
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
    align-items: center;
    justify-content: center;
  }
  .modal-overlay.open { display: flex; }

  .modal-content {
    background: var(--bg-secondary);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-xl);
    box-shadow: var(--shadow-lg);
    width: 480px;
    max-width: 95vw;
    max-height: 88vh;
    overflow-y: auto;
    padding: 24px;
  }
  .modal-content.wide { width: 660px; }
  .modal-content::-webkit-scrollbar { width: 8px; }
  .modal-content::-webkit-scrollbar-track { background: transparent; }
  .modal-content::-webkit-scrollbar-thumb { background: var(--glass-border); border-radius: 6px; }
  .modal-content::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.18); }

  .modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;
  }
  .modal-header h2 { font-size: 18px; font-weight: 700; color: var(--text-primary); }

  .modal-close {
    width: 32px; height: 32px;
    display: flex; align-items: center; justify-content: center;
    background: rgba(255,255,255,0.06);
    border: none; border-radius: var(--radius-sm);
    font-size: 16px; color: var(--text-secondary);
    cursor: pointer; transition: all var(--transition-fast);
  }
  .modal-close:hover { background: rgba(239,68,68,0.2); color: #FC8181; }

  /* lightbox */
  .lightbox-content {
    position: relative; max-width: 90vw; max-height: 90vh;
    display: flex; flex-direction: column; align-items: center;
  }
  .lightbox-close { position: absolute; top: -40px; right: 0; }
  #lightbox-img, #lightbox-video {
    max-width: 85vw; max-height: 85vh;
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-lg);
  }

  /* Tabs de configuração */
  .config-tabs {
    display: flex; gap: 4px;
    border-bottom: 1px solid var(--glass-border);
    margin-bottom: 16px; padding-bottom: 0;
    flex-wrap: wrap;
  }
  .config-tab {
    padding: 7px 14px; background: transparent;
    border: none; border-radius: var(--radius-sm) var(--radius-sm) 0 0;
    font-family: inherit; font-size: 13px; font-weight: 500;
    color: var(--text-secondary); cursor: pointer;
    transition: all var(--transition-fast);
  }
  .config-tab:hover { color: var(--text-primary); background: rgba(255,255,255,0.06); }
  .config-tab.active { color: var(--primary-light); background: rgba(124,110,248,0.12); font-weight: 600; }

  .config-panel { display: none; }
  .config-panel.active { display: block; }

  .config-section { margin-bottom: 20px; }
  .config-section h3 { font-size: 14px; font-weight: 600; color: var(--text-primary); margin-bottom: 12px; }

  .config-item {
    display: flex; align-items: center; justify-content: space-between;
    gap: 12px; padding: 8px 0;
    border-bottom: 1px solid rgba(255,255,255,0.05);
  }
  .config-label { font-size: 13px; color: var(--text-secondary); flex: 1; }

  .config-input {
    height: 32px; padding: 0 10px;
    background: var(--bg-tertiary);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    font-family: inherit; font-size: 12px;
    color: var(--text-primary); outline: none;
    width: 200px;
    color-scheme: dark;
  }
  .config-input.small { width: 120px; }
  .config-input:focus { border-color: var(--primary); }

  .config-form { display: flex; flex-direction: column; gap: 12px; }

  .btn-save, .btn-mini {
    padding: 7px 14px;
    background: #B8860B;
    border: none; border-radius: var(--radius-md);
    font-family: inherit; font-size: 13px; font-weight: 600;
    color: #fff; cursor: pointer;
    transition: all var(--transition-fast);
  }
  .btn-save:hover, .btn-mini:hover { background: #DAA520; }
  .btn-mini.danger { background: rgba(239,68,68,0.15); color: #FC8181; border: 1px solid rgba(239,68,68,0.3); }
  .btn-mini.danger:hover { background: rgba(239,68,68,0.3); }
  .btn-mini { padding: 5px 10px; font-size: 12px; }

  /* Toggle switch: <label class="switch-mini"><input type="checkbox"><span class="slider"></span></label> */
  .switch-mini {
    display: inline-block; position: relative;
    width: 36px; height: 20px; flex-shrink: 0; cursor: pointer;
  }
  .switch-mini input { display: none; }
  .switch-mini .slider {
    position: absolute; inset: 0;
    background: rgba(255,255,255,0.12); border-radius: 10px;
    transition: background var(--transition-fast);
  }
  .switch-mini input:checked + .slider { background: var(--accent-green); }
  .switch-mini .slider::after {
    content: ''; position: absolute; top: 3px; left: 3px;
    width: 14px; height: 14px; border-radius: 50%;
    background: #fff; transition: left var(--transition-fast);
    box-shadow: 0 1px 3px rgba(0,0,0,0.3);
  }
  .switch-mini input:checked + .slider::after { left: 19px; }

  /* Range slider nativo */
  input[type="range"] {
    accent-color: var(--primary);
    cursor: pointer;
  }

  /* ─────────────────────────────────────────
     MAIN CONTENT
  ───────────────────────────────────────── */
  .main-content {
    flex: 1;
    min-width: 0;
    min-height: 0;  /* necessário para o flex child encolher e o overflow-y do chat-container funcionar */
    display: flex;
    flex-direction: column;
    position: relative;
    z-index: 1;
    /* 2026-10-24: evitar colapso do flex quando o browser redimensiona durante input */
    height: 100%;
  }
  
  /* Header */
  .header {
    padding: 12px 16px;
    display: flex;
    align-items: center;
    gap: 8px;
    border-bottom: 1px solid var(--glass-border);
    background: var(--glass-bg);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
  }
  
  .model-selector {
    /* flex+min-width:0 deixa ele encolher (e truncar o nome comprido do
       modelo) em vez de esticar pro tamanho do texto e empurrar o
       ejetor/status/engrenagem pra fora da janela quando ela fica
       estreita */
    flex: 0 1 150px;   /* nao cresce mais (era flex:1, esticava sozinho) */
    min-width: 0;
    max-width: 150px;   /* menor ainda, sem sobrar espaço vazio antes da seta */
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    padding: 8px 14px;
    background: var(--bg-tertiary);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-family: inherit;
    font-size: 13px;
    cursor: pointer;
    transition: all var(--transition-base);
  }

  .model-selector:hover {
    border-color: var(--primary);
    box-shadow: var(--glow-primary);
  }
  
  /* Botão ejetor de modelo — ao lado do seletor, mesma ideia do LM Studio:
     descarrega o modelo local da VRAM (e pausa o SD-API) sem precisar abrir
     Configurações. Reaproveita a mesma limparVram() do painel avançado. */
  .btn-ejetar-modelo {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 34px;
    height: 34px;
    margin-left: 6px;
    background: rgba(239, 68, 68, 0.12);
    border: 1px solid rgba(239, 68, 68, 0.35);
    border-radius: var(--radius-md);
    color: #DC2626;
    font-size: 15px;
    line-height: 1;
    cursor: pointer;
    transition: all var(--transition-base);
  }
  .btn-ejetar-modelo:hover {
    background: rgba(239, 68, 68, 0.22);
    border-color: #EF4444;
  }
  .btn-ejetar-modelo:disabled {
    opacity: 0.6;
    cursor: default;
  }

  .model-selector:focus {
    outline: none;
    border-color: var(--primary);
    box-shadow: 0 0 0 2px rgba(124, 110, 248, 0.2);
  }
  
  .header-stats {
    flex-shrink: 0;
    /* sem margin-left:auto: fica logo depois do ejetor, junto da caixa
       de modelo, em vez de ser empurrado pra ponta direita da janela */
    display: flex;
    gap: 8px;
    align-items: center;
  }

  .stat-badge {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 14px;
    background: rgba(255, 255, 255, 0.6);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    font-size: 12px;
    color: var(--text-secondary);
  }
  
  .stat-badge .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent-green);
    box-shadow: 0 0 8px var(--accent-green);
  }
  
  .stat-badge .dot.offline {
    background: var(--text-tertiary);
    box-shadow: none;
  }

  /* Botão de engrenagem no header — só ícone, abre o mesmo modal de
     Configurações do botão da sidebar */
  .btn-config-icon {
    width: 36px;
    height: 36px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(255, 255, 255, 0.6);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    font-size: 16px;
    cursor: pointer;
    transition: all var(--transition-base);
    line-height: 1;
  }

  .btn-config-icon:hover {
    background: rgba(26, 26, 26, 0.25);
    border-color: var(--primary);
    box-shadow: var(--glow-primary);
    transform: rotate(45deg);
  }

  /* Painel de Capacidades do Modelo — agora vive DENTRO de .input-actions,
     encostado nos botões de anexar/microfone/enviar (mesma linha, mesmo
     alinhamento vertical), separado por um traço fino. Antes ficava
     position:fixed solto no canto da tela e desalinhava com o resto. */
  .model-caps-panel {
    display: flex;
    align-items: center;
    gap: 6px;
    align-self: center;
    padding-left: 10px;
    margin-left: 2px;
    border-left: 1px solid var(--glass-border);
  }

  .model-caps-panel .cap-icon {
    width: 30px;
    height: 30px;
    object-fit: contain;
    border-radius: 5px;
    font-size: 27px;
    line-height: 1;
    opacity: 0.3;
    filter: grayscale(65%);
    transition: all var(--transition-base);
    cursor: default;
  }

  .model-caps-panel .cap-icon.ativo {
    opacity: 1;
    filter: grayscale(0%);
    text-shadow: none;
  }

  @media (max-width: 768px) {
    .model-caps-panel {
      gap: 5px;
      padding-left: 6px;
    }
    .model-caps-panel .cap-icon {
      width: 24px;
      height: 24px;
      font-size: 21px;
    }
  }

  /* Chat Area */
  .chat-container {
    flex: 1;
    min-width: 0;
    min-height: 0;  /* idem — sem isso overflow-y: auto não ativa */
    overflow: hidden;
    overflow-x: hidden;
    position: relative;
    display: flex;
    flex-direction: column;
    /* 2026-10-24: prevenir que qualquer estilo inline ou animação de navegador
       "engula" o container — mantém altura visível mesmo sob pressão de flex-shrink */
    flex-shrink: 0;
    will-change: height;
  }

  /* Zoom container: applies transform only to chat content, not input bar */
  .chat-zoom-wrapper {
    flex: 1 1 0%;
    min-height: 0;  /* sem isto o flex child não encolhe e o overflow-y não ativa (scroll do mouse morre) */
    overflow-y: auto !important;
    overflow-x: hidden !important;
    padding: 32px;
    display: flex;
    flex-direction: column;
    gap: 24px;
    scroll-behavior: smooth;
    -webkit-overflow-scrolling: touch;
    overscroll-behavior: contain;
    /* 2026-10-24: blindar contra zoom/transform pai que possa empurrar o wrapper */
    transform: none !important;
    height: auto !important;
  }

  .chat-zoom-wrapper::-webkit-scrollbar {
    width: 8px;
  }

  .chat-zoom-wrapper::-webkit-scrollbar-track {
    background: transparent;
  }

  .chat-zoom-wrapper::-webkit-scrollbar-thumb {
    background: var(--glass-border);
    border-radius: 4px;
  }

  .chat-zoom-wrapper::-webkit-scrollbar-thumb:hover {
    background: rgba(255, 255, 255, 0.18);
  }

  /* Firefox: barra fina em todos os containers com scroll */
  .messages,
  .chat-zoom-wrapper,
  .hist-dropdown-list,
  .model-picker-panel,
  .modal-content,
  .drawer-content {
    scrollbar-width: thin;
    scrollbar-color: var(--glass-border) transparent;
  }
  
  /* Welcome Screen */
  .welcome-screen {
    flex: 1 0 auto;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 32px;
    animation: fadeIn var(--transition-slow);
    transition: opacity 0.25s ease, transform 0.25s ease;
  }
  .welcome-screen.typing {
    display: none;
    opacity: 0;
    transform: translateY(-16px) scale(0.98);
    pointer-events: none;
  }
  
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(20px); }
    to { opacity: 1; transform: translateY(0); }
  }
  
  .welcome-logo {
    width: 120px;
    height: 120px;
    background: transparent;
    border-radius: var(--radius-xl);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 56px;
    box-shadow: none;
    position: relative;
    overflow: hidden;
  }
  
  .welcome-logo::after {
    content: '';
    position: absolute;
    top: -50%;
    left: -50%;
    width: 200%;
    height: 200%;
    background: linear-gradient(45deg, transparent, rgba(255,255,255,0.2), transparent);
    animation: shimmer 3s infinite;
  }
  
  .welcome-title {
    font-size: 42px;
    font-weight: 700;
    background: linear-gradient(135deg, var(--text-primary) 0%, var(--primary) 50%, var(--accent-blue) 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    text-align: center;
  }
  
  .welcome-subtitle {
    font-size: 16px;
    color: var(--text-secondary);
    text-align: center;
    max-width: 500px;
    line-height: 1.6;
  }
  
  .quick-actions {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 16px;
    max-width: 600px;
    width: 100%;
  }
  
  .quick-action {
    padding: 20px 24px;
    background: var(--bg-secondary);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-lg);
    cursor: pointer;
    transition: all var(--transition-base);
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  
  .quick-action:hover {
    background: rgba(124,110,248,0.08);
    border-color: rgba(124,110,248,0.35);
    transform: translateY(-2px);
  }

  .quick-action-icon {
    width: 48px;
    height: 48px;
    background: rgba(255,255,255,0.05);
    border-radius: var(--radius-md);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 24px;
  }
  
  .quick-action-title {
    font-size: 14px;
    font-weight: 600;
    color: #DAA520;
  }
  
  .quick-action-desc {
    font-size: 12px;
    color: var(--text-tertiary);
  }
  
  /* Messages */
  .message {
    max-width: 800px;
    margin: 0 auto;
    width: 100%;
    flex-shrink: 0;
    animation: slideIn var(--transition-base);
  }
  
  @keyframes slideIn {
    from { opacity: 0; transform: translateY(20px); }
    to { opacity: 1; transform: translateY(0); }
  }
  
  .message-user {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
  }

  .message-content {
    max-width: 70%;
    padding: 5px 20px;
    background: rgba(124, 110, 248, 0.15);
    border: 1px solid rgba(124, 110, 248, 0.25);
    color: var(--text-primary);
    border-radius: 10px;
    border-top-right-radius: 4px;
    box-shadow: var(--shadow-md);
    font-size: 14px;
    line-height: 1.6;
    white-space: pre-wrap;
    overflow-wrap: break-word;
    word-break: break-word;
  }

  .message-content[contenteditable="true"] {
    outline: 2px solid var(--primary-light);
    cursor: text;
  }

  /* Miniaturas de imagem/vídeo anexados ou gerados, exibidas direto
     no balão pro operador conferir sem precisar abrir nada */
  .msg-media-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 10px;
  }

  .msg-media {
    max-width: 220px;
    max-height: 220px;
    border-radius: var(--radius-md);
    border: 1px solid var(--glass-border);
    cursor: zoom-in;
    display: block;
    object-fit: cover;
    transition: transform var(--transition-base);
  }

  .msg-media:hover {
    transform: scale(1.02);
  }

  .msg-media-legenda {
    font-size: 11px;
    color: var(--text-tertiary);
    margin-top: 3px;
    text-align: center;
  }

  /* Linha discreta com tempo/tokens embaixo da resposta */
  .msg-metricas {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-top: 10px;
    padding-top: 8px;
    border-top: 1px solid var(--glass-border);
    font-size: 11px;
    color: var(--text-tertiary);
  }

  .msg-metricas span {
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 3px;
  }

  .metric-icon {
    width: 12px;
    height: 12px;
    object-fit: contain;
    vertical-align: middle;
  }

  .msg-live-text {
    white-space: pre-wrap;
    word-break: break-word;
  }

  .message-assistant {
    padding: 24px;
    background: var(--bg-tertiary);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-lg);
    font-size: 14px;
    line-height: 1.7;
    color: var(--text-primary);
    /* sem pre-wrap: agora o HTML vem estruturado (p/ul/li/etc) via marked.js,
       cada tag já cuida do próprio espaçamento — pre-wrap duplicaria quebras */
  }

  /* Ações discretas (copiar/editar) embaixo de cada balão */
  .msg-actions {
    display: flex;
    gap: 10px;
    margin-top: 6px;
    opacity: 0.4;
    transition: opacity var(--transition-base);
  }

  .message:hover .msg-actions {
    opacity: 1;
  }

  .msg-act {
    cursor: pointer;
    font-size: 12px;
    color: var(--text-tertiary);
    padding: 2px 5px;
    border-radius: 4px;
    transition: all var(--transition-base);
    user-select: none;
  }

  .msg-act:hover {
    color: var(--text-primary);
    background: rgba(255, 255, 255, 0.08);
  }

  /* Botão "Ouvir" enquanto a leitura em voz alta está rolando — vira um
     botão de parar (⏹, vermelho) até a fala terminar ou ser cancelada. */
  .msg-act.falando {
    opacity: 1;
    color: #EF4444;
    background: rgba(239, 68, 68, 0.15);
  }

  .msg-export-select {
    cursor: pointer;
    font-size: 11px;
    color: var(--text-tertiary);
    background: transparent;
    border: none;
    padding: 2px 4px;
    border-radius: 4px;
    transition: all var(--transition-base);
    font-family: inherit;
  }

  .msg-export-select:hover, .msg-export-select:focus {
    color: var(--text-primary);
    background: rgba(255, 255, 255, 0.08);
    outline: none;
  }

  .msg-export-select option {
    background: var(--bg-secondary, #0f172a);
    color: var(--text-primary);
  }

  .message-assistant code {
    background: rgba(0, 0, 0, 0.4);
    color: #FFFFFF;
    padding: 2px 8px;
    border-radius: 6px;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 13px;
  }

  .message-assistant pre {
    background: rgba(0, 0, 0, 0.6);
    color: #FFFFFF;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    padding: 16px;
    overflow-x: auto;
    margin: 16px 0;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 13px;
  }

  .message-assistant pre code {
    background: none;
    padding: 0;
  }

  /* Markdown renderizado (marked.js): títulos, listas, links, tabelas... */
  .message-assistant h1, .message-assistant h2, .message-assistant h3,
  .message-assistant h4, .message-assistant h5, .message-assistant h6 {
    color: var(--text-primary);
    font-weight: 700;
    margin: 18px 0 8px;
    line-height: 1.3;
  }
  .message-assistant h1 { font-size: 20px; }
  .message-assistant h2 { font-size: 18px; }
  .message-assistant h3 { font-size: 16px; }
  .message-assistant h4, .message-assistant h5, .message-assistant h6 { font-size: 14px; }

  .message-assistant p {
    margin: 8px 0;
  }

  .message-assistant strong { color: var(--text-primary); font-weight: 700; }
  .message-assistant em { font-style: italic; }

  .message-assistant ul, .message-assistant ol {
    margin: 8px 0;
    padding-left: 24px;
  }
  .message-assistant li {
    margin: 4px 0;
  }
  .message-assistant li > ul, .message-assistant li > ol {
    margin: 4px 0;
  }

  .message-assistant a {
    color: var(--primary-light);
    text-decoration: underline;
  }

  .message-assistant blockquote {
    border-left: 3px solid var(--primary);
    margin: 12px 0;
    padding: 4px 16px;
    color: var(--text-secondary);
    font-style: italic;
    background: rgba(124, 110, 248, 0.07);
    border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  }

  .message-assistant hr {
    border: none;
    border-top: 1px solid var(--glass-border);
    margin: 16px 0;
  }

  .message-assistant table {
    border-collapse: collapse;
    margin: 12px 0;
    font-size: 13px;
    width: 100%;
  }
  .message-assistant th, .message-assistant td {
    border: 1px solid var(--glass-border);
    padding: 6px 10px;
    text-align: left;
  }
  .message-assistant th {
    background: rgba(124, 110, 248, 0.12);
    font-weight: 600;
    color: var(--text-primary);
  }

  .tool-chips {
    /* Escondido do cliente de propósito: a pílula "busca_web"/
       "salvar_memoria" com Args/Resultado é debug interno, não algo
       que um cliente numa demo deveria ver ou precisar clicar. Pra
       reativar (uso pessoal/debug), troque "none" por "flex". */
    display: none;
    flex-wrap: wrap;
    gap: 8px;
    margin: 16px 0;
  }
  
  .tool-chip {
    padding: 8px 16px;
    background: var(--bg-tertiary);
    border: 1px solid var(--glass-border);
    border-radius: 20px;
    font-size: 12px;
    color: var(--text-secondary);
    cursor: pointer;
    transition: all var(--transition-base);
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .tool-chip:hover {
    background: var(--bg-quaternary);
    border-color: var(--primary);
    box-shadow: none;
    transform: translateY(-2px);
  }

  .tool-chip-detail {
    display: none;
    margin-top: 12px;
    padding: 12px 16px;
    background: var(--bg-tertiary, #1a1d26);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    font-size: 12px;
    color: var(--text-secondary);
    white-space: pre-wrap;
  }
  
  .tool-chip.active + .tool-chip-detail {
    display: block;
    animation: fadeIn var(--transition-base);
  }
  
  /* Input Area */
  .input-container {
    padding: 24px 32px;
    background: var(--glass-bg);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border-top: 1px solid var(--glass-border);
    /* 2026-10-24: impedir que o container de input encolha com o flex-shrink */
    flex-shrink: 0;
  }
  
  .input-wrapper {
    max-width: 800px;
    margin: 0 auto;
    position: relative;
  }
  
  .input-box {
    display: flex;
    gap: 12px;
    padding: 16px;
    background: var(--bg-secondary);
    border: 2px solid var(--glass-border);
    border-radius: var(--radius-xl);
    transition: all var(--transition-base);
    box-shadow: var(--shadow-md);
  }
  
  .input-box:focus-within {
    border-color: var(--primary);
    box-shadow: 0 0 0 2px rgba(124, 110, 248, 0.15);
  }

  .input-box textarea {
    flex: 1;
    background: transparent;
    border: none;
    outline: none;
    color: var(--text-primary);
    font-family: inherit;
    font-size: 14px;
    line-height: 1.5;
    resize: none;
    max-height: 200px;
    min-height: 24px;
  }
  
  .input-box textarea::placeholder {
    color: var(--text-tertiary);
  }
  
  .input-actions {
    display: flex;
    gap: 8px;
    align-items: flex-end;
  }
  
  .btn-icon {
    width: 21px;
    height: 21px;
    background: linear-gradient(135deg, var(--primary) 0%, var(--primary-light) 100%);
    border: none;
    border-radius: var(--radius-md);
    color: white;
    font-size: 9px;
    cursor: pointer;
    transition: all var(--transition-base);
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 2px 8px rgba(26, 26, 26, 0.18);
  }
  
  .btn-icon:hover {
    transform: translateY(-2px) scale(1.05);
    box-shadow: 0 4px 12px rgba(26, 26, 26, 0.22);
  }

  .btn-icon-img {
    width: 65%;
    height: 65%;
    object-fit: contain;
  }
  
  .btn-icon:active {
    transform: translateY(0) scale(1);
  }
  
  .btn-icon.secondary {
    background: rgba(26, 26, 26, 0.2);
    border: 1px solid var(--glass-border);
    box-shadow: none;
  }
  
  .btn-icon.secondary:hover {
    background: rgba(26, 26, 26, 0.3);
    border-color: var(--primary);
  }

  /* Microfone gravando */
  .btn-icon.gravando {
    background: rgba(239, 68, 68, 0.25);
    border-color: #EF4444;
    animation: pulseGravando 1.2s ease-in-out infinite;
  }

  @keyframes pulseGravando {
    0%, 100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.4); }
    50% { box-shadow: 0 0 0 10px rgba(239, 68, 68, 0); }
  }

  /* Cores próprias pra cada botão secundário do input — antes todos
     usavam o mesmo laranja apagado e ficavam quase invisíveis juntos */
  .btn-icon.cor-planejamento {
    background: rgba(59, 130, 246, 0.18);
    border-color: rgba(59, 130, 246, 0.5);
    color: #93C5FD;
  }
  .btn-icon.cor-planejamento:hover {
    background: rgba(59, 130, 246, 0.3);
    border-color: #3B82F6;
  }

  .btn-icon.cor-anexar {
    background: rgba(34, 197, 94, 0.18);
    border-color: rgba(34, 197, 94, 0.5);
    color: #86EFAC;
  }
  .btn-icon.cor-anexar:hover {
    background: rgba(34, 197, 94, 0.3);
    border-color: #22C55E;
  }

  .btn-icon.cor-microfone {
    background: rgba(168, 85, 247, 0.18);
    border-color: rgba(168, 85, 247, 0.5);
    color: #D8B4FE;
  }
  .btn-icon.cor-microfone:hover {
    background: rgba(168, 85, 247, 0.3);
    border-color: #A855F7;
  }

  .btn-icon.cor-print {
    background: rgba(234, 179, 8, 0.18);
    border-color: rgba(234, 179, 8, 0.5);
    color: #FDE68A;
    font-size: 16px;
  }
  .btn-icon.cor-print:hover {
    background: rgba(234, 179, 8, 0.3);
    border-color: #EAB308;
  }

  /* Botão cérebro — ver tela inteira, tamanho destacado */
  .btn-cerebro {
    width: 52px !important;
    height: 52px !important;
    border-radius: 50% !important;
    padding: 3px !important;
    background: rgba(124, 110, 248, 0.15) !important;
    border: 2px solid rgba(124, 110, 248, 0.55) !important;
    box-shadow: 0 0 12px rgba(124, 110, 248, 0.25);
    transition: all 0.2s ease !important;
    flex-shrink: 0;
  }
  .btn-cerebro:hover {
    background: rgba(124, 110, 248, 0.3) !important;
    border-color: var(--primary) !important;
    box-shadow: 0 0 22px rgba(124, 110, 248, 0.5);
    transform: scale(1.08);
  }
  .btn-cerebro img {
    width: 100%; height: 100%;
    object-fit: cover; border-radius: 50%;
  }
  .btn-cerebro.piscando {
    animation: pulseCerebro 0.6s ease-in-out 3;
  }
  @keyframes pulseCerebro {
    0%,100% { box-shadow: 0 0 12px rgba(124,110,248,0.25); }
    50% { box-shadow: 0 0 32px rgba(124,110,248,0.8); }
  }
  .btn-icon.cor-print.aguardando {
    opacity: 0.6;
    pointer-events: none;
  }

  .btn-icon.cor-gravar {
    background: rgba(234, 179, 8, 0.18);
    border-color: rgba(234, 179, 8, 0.5);
    color: #FDE68A;
    font-size: 16px;
  }
  .btn-icon.cor-gravar:hover {
    background: rgba(234, 179, 8, 0.3);
    border-color: #EAB308;
  }
  .btn-icon.cor-gravar.aguardando {
    opacity: 0.6;
    pointer-events: none;
  }
  /* Gravação em andamento: vermelho pulsante, pra ficar óbvio que tem
     uma gravação rolando em background enquanto o usuário mexe no
     resto do chat. */
  .btn-icon.cor-gravar.gravando {
    background: rgba(239, 68, 68, 0.25);
    border-color: #EF4444;
    color: #FCA5A5;
    animation: pulso-gravando 1.4s ease-in-out infinite;
  }
  @keyframes pulso-gravando {
    0%, 100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.45); }
    50% { box-shadow: 0 0 0 6px rgba(239, 68, 68, 0); }
  }

  /* Enviar — antes usava o fundo escuro padrão do .btn-icon (virava um
     quadrado preto atrás da seta); agora segue o mesmo estilo claro dos
     outros botões da barra, sem fundo sólido. */
  .btn-icon.cor-enviar {
    background: rgba(26, 26, 26, 0.12);
    border: 1px solid rgba(26, 26, 26, 0.25);
    box-shadow: none;
  }
  .btn-icon.cor-enviar:hover {
    background: rgba(26, 26, 26, 0.22);
    border-color: var(--primary);
  }

  /* Modo planejamento ativo (só conversa até confirmar) */
  .btn-icon.planejamento-ativo {
    background: rgba(59, 130, 246, 0.35);
    border-color: #3B82F6;
    box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.35);
  }

  /* Chips de anexo (imagem/vídeo) pendentes, acima da caixa de texto */
  .anexos-preview {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 10px;
  }
  .aviso-visao {
    width: 100%; font-size: 12px; padding: 6px 10px;
    background: rgba(234, 179, 8, 0.12); border: 1px solid rgba(234, 179, 8, 0.35);
    border-radius: 8px; color: #ca8a04; line-height: 1.4;
  }

  .chip-anexo {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 10px;
    background: rgba(255, 255, 255, 0.08);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    font-size: 12px;
    color: var(--text-secondary);
    max-width: 220px;
  }

  .chip-anexo.enviando { opacity: .55; }

  .chip-anexo .nome {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chip-anexo .remover {
    cursor: pointer;
    color: var(--text-tertiary);
    font-weight: 700;
  }

  .chip-anexo .remover:hover { color: #FCA5A5; }

  /* Botões de ação abaixo do input */
  .input-action-buttons {
    display: flex;
    gap: 10px;
    margin-top: 12px;
    justify-content: space-between;
    align-items: center;
  }
  
  .btn-action {
    padding: 8px 16px;
    background: rgba(239, 68, 68, 0.15);
    border: 1px solid rgba(239, 68, 68, 0.3);
    border-radius: var(--radius-md);
    color: #EF4444;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: all var(--transition-base);
    display: flex;
    align-items: center;
    gap: 6px;
  }
  
  .btn-action:hover {
    background: rgba(239, 68, 68, 0.25);
    box-shadow: 0 0 20px rgba(239, 68, 68, 0.3);
    transform: translateY(-2px);
  }
  
  .btn-action:disabled {
    opacity: 0.4;
    cursor: not-allowed;
    transform: none;
  }

  /* Cronometro da tarefa em andamento */
  .cronometro-tarefa {
    display: none;
    align-items: center;
    gap: 6px;
    padding: 8px 14px;
    background: rgba(239, 68, 68, 0.08);
    border: 1px solid rgba(239, 68, 68, 0.2);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    font-size: 12px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  .cronometro-tarefa.ativo {
    display: flex;
  }

  .cronometro-tarefa .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #EF4444;
    animation: pulseDot 1s ease-in-out infinite;
  }

  @keyframes pulseDot {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }

  @media (max-width: 1024px) {
    
    .quick-actions {
      grid-template-columns: 1fr;
    }
    
    .message-content, .message-assistant {
      max-width: 85%;
    }
  }
  
  /* Loading animation */
  .typing-indicator {
    display: flex;
    gap: 6px;
    padding: 16px 20px;
  }
  
  .typing-indicator span {
    width: 8px;
    height: 8px;
    background: var(--primary);
    border-radius: 50%;
    animation: typing 1.4s infinite;
  }
  
  .typing-indicator span:nth-child(2) {
    animation-delay: 0.2s;
  }
  
  .typing-indicator span:nth-child(3) {
    animation-delay: 0.4s;
  }
  
  @keyframes typing {
    0%, 100% { opacity: 0.3; transform: translateY(0); }
    50% { opacity: 1; transform: translateY(-8px); }
  }
  
  /* Stop button */
  .btn-stop {
    padding: 10px 20px;
    background: rgba(239, 68, 68, 0.2);
    border: 1px solid rgba(239, 68, 68, 0.3);
    border-radius: var(--radius-md);
    color: #EF4444;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: all var(--transition-base);
    display: flex;
    align-items: center;
    gap: 8px;
  }
  
  .btn-stop:hover {
    background: rgba(239, 68, 68, 0.3);
    box-shadow: 0 0 20px rgba(239, 68, 68, 0.3);
  }
  /* Temas da Janela (Branco / Cinza / Preto) */
  :root[data-window-theme="white"] {
    --bg-primary: #ffffff;
    --bg-secondary: #f7f7f7;
    --bg-tertiary: #f0f0f0;
    --bg-quaternary: #e9e9e9;
    --bg-glass: rgba(255, 255, 255, 0.92);
    --glass-bg: rgba(255, 255, 255, 0.92);
    --glass-border: rgba(0, 0, 0, 0.08);
    --text-primary: #151515;
    --text-secondary: #3f3f3f;
    --text-tertiary: #6f6f6f;
    --shadow-md: 0 1px 2px rgba(0, 0, 0, 0.02), 0 8px 30px rgba(0, 0, 0, 0.04);
    --shadow-lg: 0 12px 40px rgba(0, 0, 0, 0.06);
  }

  :root[data-window-theme="gray"] {
    --bg-primary: #1f1f1f;
    --bg-secondary: #242424;
    --bg-tertiary: #2b2b2b;
    --bg-quaternary: #333333;
    --bg-glass: rgba(31, 31, 31, 0.92);
    --glass-bg: rgba(31, 31, 31, 0.92);
    --glass-border: rgba(255, 255, 255, 0.08);
    --text-primary: #f5f5f5;
    --text-secondary: #d7d7d7;
    --text-tertiary: #a9a9a9;
    --shadow-md: 0 1px 2px rgba(0, 0, 0, 0.25), 0 8px 30px rgba(0, 0, 0, 0.28);
    --shadow-lg: 0 12px 40px rgba(0, 0, 0, 0.32);
  }

  :root[data-window-theme="black"] {
    --bg-primary: #0f0f0f;
    --bg-secondary: #151515;
    --bg-tertiary: #1b1b1b;
    --bg-quaternary: #232323;
    --bg-glass: rgba(15, 15, 15, 0.94);
    --glass-bg: rgba(15, 15, 15, 0.94);
    --glass-border: rgba(255, 255, 255, 0.06);
    --text-primary: #f5f5f5;
    --text-secondary: #d7d7d7;
    --text-tertiary: #9b9b9b;
    --shadow-md: 0 1px 2px rgba(0, 0, 0, 0.35), 0 8px 30px rgba(0, 0, 0, 0.38);
    --shadow-lg: 0 12px 40px rgba(0, 0, 0, 0.42);
  }

  /* Toolbar discreta abaixo do input */
  .input-toolbar-subtle {
    display: flex;
    align-items: center;
    gap: 4px;
    margin-top: 8px;
    max-width: 800px;
    margin-left: auto;
    margin-right: auto;
    padding: 0 4px;
    opacity: 1;
    transition: opacity 0.15s ease;
  }

  .input-toolbar-subtle:hover {
    opacity: 1;
  }

  .input-toolbar-subtle:focus-within {
    opacity: 1;
  }

  /* Cores por botão na toolbar discreta (fundo suave + ícone colorido) */
  .btn-discreto.cor-cerebro { background: rgba(239, 68, 68, 0.14); border-color: rgba(239, 68, 68, 0.4); }
  .btn-discreto.cor-planejamento { background: rgba(59, 130, 246, 0.14); border-color: rgba(59, 130, 246, 0.4); }
  .btn-discreto.cor-anexar { background: rgba(34, 197, 94, 0.14); border-color: rgba(34, 197, 94, 0.4); }
  .btn-discreto.cor-print { background: rgba(234, 179, 8, 0.14); border-color: rgba(234, 179, 8, 0.4); }
  .btn-discreto.cor-gravar { background: rgba(234, 179, 8, 0.14); border-color: rgba(234, 179, 8, 0.4); }
  .btn-discreto.cor-microfone { background: rgba(168, 85, 247, 0.14); border-color: rgba(168, 85, 247, 0.4); }
  .btn-discreto.cor-cerebro:hover { background: rgba(239, 68, 68, 0.28); border-color: #EF4444; }
  .btn-discreto.cor-planejamento:hover { background: rgba(59, 130, 246, 0.28); border-color: #3B82F6; }
  .btn-discreto.cor-anexar:hover { background: rgba(34, 197, 94, 0.28); border-color: #22C55E; }
  .btn-discreto.cor-print:hover { background: rgba(234, 179, 8, 0.28); border-color: #EAB308; }
  .btn-discreto.cor-gravar:hover { background: rgba(234, 179, 8, 0.28); border-color: #EAB308; }
  .btn-discreto.cor-microfone:hover { background: rgba(168, 85, 247, 0.28); border-color: #A855F7; }

  .btn-discreto {
    width: 22px;
    height: 22px;
    border-radius: 6px;
    border: 1px solid transparent;
    background: transparent;
    color: var(--text-tertiary);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    transition: all 0.12s ease;
    flex-shrink: 0;
    font-size: 14px;
    line-height: 1;
  }

  .btn-discreto img {
    width: 16px;
    height: 16px;
    object-fit: contain;
    filter: none;
    opacity: 1;
  }

  .btn-discreto:hover {
    background: var(--bg-tertiary);
    border-color: var(--glass-border);
    color: var(--text-secondary);
  }

  .btn-discreto:hover img {
    filter: none;
    opacity: 1;
  }

  .btn-discreto:active {
    transform: scale(0.96);
  }

  .btn-discreto.ativo {
    background: var(--bg-tertiary);
    color: var(--text-primary);
  }

  .btn-discreto.ativo img {
    filter: grayscale(0);
    opacity: 1;
  }

  .btn-discreto.gravando {
    color: #ef4444;
    animation: pulseDiscreto 1.2s ease-in-out infinite;
  }

  .btn-discreto.aguardando {
    opacity: 0.4;
    pointer-events: none;
  }

  @keyframes pulseDiscreto {
    0%, 100% { opacity: 0.8; }
    50% { opacity: 1; }
  }

  .btn-discreto.cerebro-mini {
    width: 26px;
    height: 26px;
    border-radius: 50%;
    overflow: hidden;
    border: 1px solid var(--glass-border);
  }

  .btn-discreto.cerebro-mini img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    filter: none;
    opacity: 1;
  }

  .toolbar-spacer {
    flex: 1;
  }

  .model-caps-mini {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 2px 6px;
    border-radius: 999px;
    border: 1px solid var(--glass-border);
    background: transparent;
  }

  .model-caps-mini .cap-icon {
    width: 14px;
    height: 14px;
    opacity: 0.25;
    filter: grayscale(0.6);
  }

  .model-caps-mini .cap-icon.ativo {
    opacity: 0.9;
    filter: grayscale(0);
  }

  /* Drawer lateral de ferramentas */
  .drawer-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.4);
    backdrop-filter: blur(2px);
    -webkit-backdrop-filter: blur(2px);
    opacity: 0;
    visibility: hidden;
    transition: all 0.15s ease;
    z-index: 1000;
  }

  .drawer-overlay.active {
    opacity: 1;
    visibility: visible;
  }

  .tools-drawer {
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    width: min(320px, 90vw);
    background: var(--bg-primary);
    border-left: 1px solid var(--glass-border);
    transform: translateX(100%);
    transition: transform 0.2s ease;
    z-index: 1001;
    display: flex;
    flex-direction: column;
    box-shadow: -8px 0 40px rgba(0, 0, 0, 0.15);
  }

  .tools-drawer.active {
    transform: translateX(0);
  }

  .drawer-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 20px;
    border-bottom: 1px solid var(--glass-border);
    background: var(--bg-secondary);
  }

  .drawer-title {
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
    letter-spacing: -0.01em;
  }

  .drawer-close {
    width: 32px;
    height: 32px;
    border-radius: var(--radius-md);
    border: 1px solid transparent;
    background: transparent;
    color: var(--text-secondary);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 20px;
    line-height: 1;
    transition: all 0.15s ease;
  }

  .drawer-close:hover {
    background: var(--bg-tertiary);
    color: var(--text-primary);
  }

  .drawer-content {
    flex: 1;
    overflow-y: auto;
    padding: 16px 20px 20px;
    display: flex;
    flex-direction: column;
    gap: 20px;
    scrollbar-width: thin;
    scroll-behavior: smooth;
    -webkit-overflow-scrolling: touch;
  }

  .drawer-content::-webkit-scrollbar {
    width: 4px;
  }

  .drawer-content::-webkit-scrollbar-thumb {
    background: var(--glass-border);
    border-radius: 2px;
  }

  .drawer-section {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .drawer-section-title {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 600;
    color: var(--text-tertiary);
    padding: 0 6px 4px;
  }

  .drawer-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 10px;
    border-radius: var(--radius-md);
    border: 1px solid transparent;
    background: transparent;
    color: var(--text-primary);
    cursor: pointer;
    transition: all 0.15s ease;
    text-align: left;
    width: 100%;
    font-size: 13px;
    line-height: 1.4;
    font-family: inherit;
  }

  .drawer-item:hover {
    background: var(--bg-tertiary);
    border-color: var(--glass-border);
  }

  .drawer-item:active {
    transform: scale(0.995);
  }

  .drawer-item.active {
    background: var(--bg-tertiary);
    border-color: var(--glass-border);
  }

  .drawer-item-icon {
    width: 20px;
    height: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    color: var(--text-secondary);
    font-size: 16px;
  }

  .drawer-item-icon img {
    width: 18px;
    height: 18px;
    object-fit: contain;
  }

  .drawer-item-label {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .drawer-item-title {
    font-size: 13px;
    font-weight: 500;
    color: var(--text-primary);
    letter-spacing: -0.01em;
  }

  .drawer-item-subtitle {
    font-size: 11px;
    color: var(--text-tertiary);
    line-height: 1.3;
  }

  .drawer-divider {
    height: 1px;
    background: var(--glass-border);
    margin: 4px 6px;
  }

  .drawer-caps {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 10px 8px;
    border-radius: var(--radius-md);
    border: 1px solid var(--glass-border);
    background: var(--bg-secondary);
  }

  .drawer-caps-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 2px;
  }

  .drawer-caps-title {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 600;
    color: var(--text-tertiary);
  }

  .drawer-caps-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
    justify-content: flex-start;
  }

  .drawer-caps-grid .cap-icon {
    width: 20px;
    height: 20px;
    opacity: 0.25;
    transition: all 0.15s ease;
    filter: grayscale(0.4);
  }

  .drawer-caps-grid .cap-icon.ativo {
    opacity: 1;
    filter: none;
  }

  .btn-icon.ghost {
    background: transparent;
    border: 1px solid transparent;
    box-shadow: none;
    color: var(--text-secondary);
  }

  .btn-icon.ghost:hover {
    background: var(--bg-tertiary);
    border-color: var(--glass-border);
    color: var(--text-primary);
    transform: none;
    box-shadow: none;
  }

  .btn-icon.ghost:active {
    transform: scale(0.98);
  }

  .btn-icon.minimal-plus {
    font-size: 18px;
    font-weight: 400;
    line-height: 1;
  }

</style>
</head>
<body>

<!-- Top Rail -->
<nav class="top-rail" id="top-rail">
  <!-- Logo -->
  <a class="rail-logo" href="#" onclick="return false">
    <img class="rail-logo-img" src="/imagem/_icones/jarvis_mascote.png" alt="Jarvis">
    <span class="rail-logo-text">Jarvis AI</span>
  </a>

  <div class="rail-sep"></div>

  <!-- Nova Conversa -->
  <button class="rail-btn" onclick="novaSessao()">➕ Nova Conversa</button>

  <!-- Histórico (dropdown) -->
  <button class="rail-btn" id="btn-historico" onclick="toggleHistoricoDropdown()">📋 Histórico</button>

  <div class="rail-spacer"></div>

  <!-- Status dots (só pontos — texto oculto, ainda atualizado pelo JS) -->
  <div class="rail-status-group" title="Ollama · Memória · Imagens · Vídeo">
    <span class="rail-dot offline" id="status-ollama" title="Ollama: -"></span>
    <span class="rail-dot offline" id="status-memory" title="Memória: -"></span>
    <span class="rail-dot offline" id="status-images" title="Imagens: -"></span>
    <span class="rail-dot offline" id="status-ffmpeg" title="Vídeo: -"></span>
  </div>
  <!-- textos ocultos preservados para o JS -->
  <span id="text-ollama" style="display:none">-</span>
  <span id="text-memory" style="display:none">-</span>
  <span id="text-images" style="display:none">-</span>
  <span id="text-ffmpeg" style="display:none">-</span>

  <div class="rail-sep"></div>

  <!-- Model selector (custom) -->
  <select id="model-selector" style="display:none" onchange="trocarModelo()">
    <option value="">Carregando...</option>
  </select>
  <div class="model-picker" id="model-picker">
    <button class="model-picker-btn" id="model-picker-btn" onclick="toggleModelPicker(event)">
      <span class="model-picker-dot" id="model-picker-dot"></span>
      <span class="model-picker-name" id="model-picker-name">Carregando...</span>
      <span class="model-picker-arrow">▾</span>
    </button>
    <div class="model-picker-panel" id="model-picker-panel"></div>
  </div>

  <!-- Eject -->
  <button class="rail-eject" id="btn-ejetar-modelo" onclick="limparVram()" title="Ejetar modelo da VRAM">⏏</button>

  <!-- Backend switch (Ollama / LM Studio) -->
  <div class="rail-backend" id="btn-backend-switch" title="Trocar backend local: Ollama → LM Studio → KoboldCpp" onclick="trocarBackend()">
    <div class="rail-backend-knob"></div>
  </div>

  <!-- Zoom -->
  <div class="rail-zoom">
    <button class="rail-zoom-btn" onclick="ajustarZoom(-10)" title="Diminuir zoom (Ctrl+scroll ↓)">A−</button>
    <button class="rail-zoom-btn" onclick="ajustarZoom(10)"  title="Aumentar zoom (Ctrl+scroll ↑)">A+</button>
  </div>

  <!-- Gear -->
  <button class="rail-gear" onclick="abrirConfig()" title="Configurações">
    <img src="/imagem/_icones/config.png" alt="Config" style="width:16px;height:16px;object-fit:contain">
  </button>

  <!-- hidden: preserved for status checks -->
  <span id="text-model" style="display:none"></span>
</nav>

<div class="zoom-indicator" id="zoom-indicator">100%</div>

<!-- Histórico dropdown -->
<div class="hist-dropdown" id="hist-dropdown">
  <div class="hist-dropdown-search">
    <input type="text" id="search-input" placeholder="Buscar no histórico..." onkeydown="handleSearch(event)" autocomplete="off" data-lpignore="true" data-1p-ignore="true">
  </div>
  <div class="hist-dropdown-list" id="history-list">
    <div class="section-title">Recentes</div>
  </div>
</div>

<!-- Menu "⋮" único e global das conversas recentes (fixed, fora da sidebar
     pra não ser cortado por overflow dos containers) -->
<div class="history-dropdown" id="history-global-menu">
  <button class="history-dropdown-item" onclick="event.stopPropagation(); historyMenuExportar()">💾 Salvar em .txt</button>
  <button class="history-dropdown-item perigo" onclick="event.stopPropagation(); historyMenuExcluir()">🗑️ Apagar</button>
</div>

<!-- Lightbox: imagem/vídeo anexado ou gerado, em tamanho grande -->
<div class="modal-overlay" id="modal-lightbox" onclick="fecharLightbox(event)">
  <div class="lightbox-content">
    <button class="modal-close lightbox-close" onclick="fecharLightbox(event)">✕</button>
    <img id="lightbox-img" src="" alt="" style="display:none">
    <video id="lightbox-video" controls autoplay style="display:none"></video>
  </div>
</div>

<!-- Modal de Configurações -->
<div class="modal-overlay" id="modal-config">
  <div class="modal-content wide">
    <div class="modal-header">
      <h2>⚙️ Configurações</h2>
      <button class="modal-close" onclick="fecharConfig()">✕</button>
    </div>

    <div class="config-tabs">
      <button class="config-tab active" data-tab="cerebros" onclick="mudarTabConfig('cerebros', this)">🧠 Cérebros</button>
      <button class="config-tab" data-tab="mcp" onclick="mudarTabConfig('mcp', this)">🔌 MCP</button>
      <button class="config-tab" data-tab="plugins" onclick="mudarTabConfig('plugins', this)">🧩 Plugins</button>
      <button class="config-tab" data-tab="pastas" onclick="mudarTabConfig('pastas', this)">📁 Pastas</button>
      <button class="config-tab" data-tab="model" onclick="mudarTabConfig('model', this)">⚙️ Model</button>
      <button class="config-tab" data-tab="sistema" onclick="mudarTabConfig('sistema', this)">🖥️ Sistema</button>
    </div>

    <!-- CÉREBROS DE NUVEM -->
    <div class="config-panel active" id="panel-cerebros">
      <div class="config-section">
        <h3>Cérebros de Nuvem (Groq, 9router, ou qualquer outro compatível com OpenAI)</h3>
        <div id="lista-cerebros"><p style="color:var(--text-tertiary);font-size:13px">Carregando...</p></div>
        <button class="btn-mini" style="margin-top:8px" onclick="toggleFormCerebro()">+ Adicionar cérebro</button>
        <div class="config-form" id="form-cerebro" style="display:none">
          <label>Nome</label>
          <input id="cb-nome" placeholder="ex: openrouter">
          <label>Tipo</label>
          <select id="cb-tipo"><option value="api">api</option><option value="https">https</option></select>
          <label>URL (chat completions)</label>
          <input id="cb-url" placeholder="https://.../v1/chat/completions">
          <label>Chave (API key)</label>
          <input id="cb-chave" type="password" placeholder="sk-..." autocomplete="new-password" data-lpignore="true" data-1p-ignore="true">
          <label>Modelos (separados por vírgula)</label>
          <textarea id="cb-modelos" placeholder="modelo-1, modelo-2"></textarea>
          <div style="display:flex; gap:8px;">
            <button class="btn-mini" onclick="descobrirModelosCerebro()">🔍 Descobrir modelos</button>
            <button class="btn-mini" onclick="salvarCerebro()">💾 Salvar</button>
          </div>
        </div>
      </div>
    </div>

    <!-- MCP -->
    <div class="config-panel" id="panel-mcp">
      <div class="config-section">
        <h3>Servidores MCP</h3>
        <div id="lista-mcp"><p style="color:var(--text-tertiary);font-size:13px">Carregando...</p></div>
        <button class="btn-mini" style="margin-top:8px" onclick="toggleFormMcp()">+ Adicionar servidor</button>
        <div class="config-form" id="form-mcp" style="display:none">
          <label>Nome</label>
          <input id="mcp-nome" placeholder="ex: filesystem">
          <label>Comando</label>
          <input id="mcp-command" placeholder="npx">
          <label>Argumentos (1 por linha)</label>
          <textarea id="mcp-args" placeholder="-y
@modelcontextprotocol/server-filesystem
C:\\pasta"></textarea>
          <label>Variáveis de ambiente (CHAVE=valor, 1 por linha)</label>
          <textarea id="mcp-env" placeholder="API_KEY=..."></textarea>
          <button class="btn-mini" onclick="salvarMcp()">💾 Salvar</button>
        </div>
      </div>
    </div>

    <!-- PLUGINS -->
    <div class="config-panel" id="panel-plugins">
      <div class="config-section">
        <h3>Plugins (ferramentas)</h3>
        <div id="lista-plugins"><p style="color:var(--text-tertiary);font-size:13px">Carregando...</p></div>
        <p style="font-size:11px;color:var(--text-tertiary);margin-top:10px">Precisa reiniciar o Jarvis pra aplicar.</p>
      </div>
    </div>

    <!-- PASTAS -->
    <div class="config-panel" id="panel-pastas">
      <div class="config-section">
        <h3>Pastas de trabalho extras</h3>
        <div id="lista-pastas"><p style="color:var(--text-tertiary);font-size:13px">Carregando...</p></div>
        <div style="display:flex; gap:8px; margin-top:10px;">
          <input class="config-input" id="nova-pasta" placeholder="C:\caminho\da\pasta" style="flex:1">
          <button class="btn-mini" onclick="adicionarPasta()">+ Adicionar</button>
        </div>
      </div>
    </div>

    <!-- MODEL -->
    <div class="config-panel" id="panel-model">
      <div class="config-section">
        <h3>⚙️ Parâmetros do Model</h3>
        <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:14px">
          Os 4 controles essenciais de geração. Os parâmetros avançados de
          carga do modelo local (Ollama) ficam no botão logo abaixo. Aplica na
          próxima mensagem, sem precisar reiniciar o Jarvis.
        </p>

        <label style="display:block;font-size:12px;color:var(--text-secondary);margin-bottom:6px">Prompt do Sistema (extra)</label>
        <textarea class="config-input" id="model-prompt-sistema" rows="4"
          style="width:100%;resize:vertical;margin-bottom:4px"
          placeholder="Instrução adicional pro Jarvis, ex: responda sempre em tom mais formal. (Some ao final do prompt fixo do agente, não substitui.)"></textarea>
        <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:16px">Vazio = só o comportamento padrão do Jarvis, sem instrução extra.</p>

        <div class="config-item">
          <span class="config-label">Temperature (<span id="model-temp-valor">0.7</span>)</span>
        </div>
        <input type="range" id="model-temperature" min="0" max="2" step="0.1" value="0.7"
          style="width:100%;margin-bottom:4px" oninput="document.getElementById('model-temp-valor').textContent = this.value">
        <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:16px">Baixo (0.2-0.4) = mais previsível, bom pra código/function calling. Alto (0.7-1.0) = mais criativo.</p>

        <div class="config-item">
          <span class="config-label">🔧 Chamada de função (function calling)</span>
          <label class="switch-mini">
            <input type="checkbox" id="model-function-calling" checked>
            <span class="slider"></span>
          </label>
        </div>
        <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:16px">Desligado = Jarvis responde só em texto, sem usar ferramentas (útil pra debug ou conversa sem custo extra de contexto).</p>

        <div class="config-item">
          <span class="config-label">max_tokens</span>
          <input type="number" class="config-input small" id="model-max-tokens" min="-2" step="1" value="0" placeholder="0 = sem limite">
        </div>
        <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:16px">0 = usa o padrão do modelo/provedor. -1 = geração infinita, -2 = preenche todo contexto.</p>

        <!-- Botão mostrar/ocultar avançados -->
        <button class="btn-mini" style="margin-top:4px;margin-bottom:12px" onclick="toggleAvancados()">⚙️ Parâmetros Avançados (Ollama)</button>
        <div id="model-avancados" style="display:none;margin-bottom:16px;padding-top:12px;border-top:1px solid var(--glass-border)">

          <div class="config-item">
            <span class="config-label">num_gpu (camadas na GPU / ngl) <span id="model-camadas-total" style="font-weight:400;color:var(--text-tertiary)"></span></span>
            <input type="number" class="config-input small" id="model-num-gpu" min="0" step="1" value="" placeholder="auto" oninput="sincronizarNumGpuCampo(this.value)">
          </div>
          <input type="range" id="model-num-gpu-slider" min="0" max="0" step="1" value="0"
            style="width:100%;margin:6px 0 2px" oninput="sincronizarNumGpuSlider(this.value)">
          <div style="display:flex;gap:8px;margin-bottom:10px">
            <button class="btn-mini" onclick="setNumGpuAuto()">♻ Auto (VRAM)</button>
            <button class="btn-mini" onclick="carregarDadosModelo(false)">📊 Dados do modelo</button>
          </div>
          <div id="model-dados-panel" style="display:none;margin-bottom:12px;padding:12px;border:1px solid var(--glass-border);border-radius:8px;font-size:12px;line-height:1.6;color:var(--text-secondary)"></div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:10px">Arraste pra subir as camadas à GPU aos poucos e testar o modelo (0 = tudo na CPU, máximo = todas na GPU). Vazio/Auto = Ollama decide pela VRAM.</p>

          <div class="config-item">
            <span class="config-label">num_ctx (janela de contexto)</span>
            <input type="number" class="config-input small" id="model-num-ctx" min="1" step="1" value="" placeholder="auto">
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:10px">Tamanho da janela em tokens. Vazio = padrão do modelo. Contexto maior consome mais VRAM.</p>

          <div class="config-item">
            <span class="config-label">num_thread (threads CPU)</span>
            <input type="number" class="config-input small" id="model-num-thread" min="1" step="1" value="" placeholder="auto">
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:10px">Nº de threads de CPU. Vazio = Ollama decide (geralmente núcleos físicos).</p>

          <div class="config-item">
            <span class="config-label">use_mmap (mapeamento em memória)</span>
            <label class="switch-mini">
              <input type="checkbox" id="model-use-mmap" checked>
              <span class="slider"></span>
            </label>
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:10px">True = mapeia o arquivo do modelo (mais rápido pra recarregar). False = carrega tudo em RAM (evita I/O durante geração).</p>

          <div class="config-item">
            <span class="config-label">use_mlock (travar em RAM física)</span>
            <label class="switch-mini">
              <input type="checkbox" id="model-use-mlock" checked>
              <span class="slider"></span>
            </label>
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:10px">True = trava o modelo em RAM (não deixa swap) — reduz stutter, exige RAM sobrando.</p>

          <div class="config-item">
            <span class="config-label">pensar_modo (thinking nativo)</span>
            <select class="config-input small" id="model-pensar-modo">
              <option value="auto">auto (padrão)</option>
              <option value="sempre">sempre ligado</option>
              <option value="nunca">nunca</option>
            </select>
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:10px">Só afeta modelos locais com thinking (qwen3, deepseek-r1, etc). Auto decide pela categoria da mensagem.</p>

          <div class="config-item">
            <span class="config-label">mtp_draft_num_predict</span>
            <input type="number" class="config-input small" id="model-mtp" min="0" max="8" step="1" value="" placeholder="auto">
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:10px">Tokens de rascunho pro MTP (Multi-Token Prediction). Só funciona em GGUFs com draft head. 0 = desligado, 1-8 = ligado. Vazio = auto.</p>

          <div class="config-item">
            <span class="config-label">🧪 SSQP (preset MoE testado)</span>
            <label class="switch-mini">
              <input type="checkbox" id="model-ssqp-ativo">
              <span class="slider"></span>
            </label>
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:10px">Quando ligado, usa o preset de camadas GPU/CPU testado e salvo pro modelo atual (moe_tuner), com prioridade sobre num_gpu/num_ctx globais. Desligado = usa só os valores manuais/automáticos.</p>

          <div class="config-item">
            <span class="config-label">motor_busca</span>
            <select class="config-input small" id="model-motor-busca">
              <option value="auto">auto (padrão)</option>
              <option value="tavily">tavily</option>
              <option value="yandex">yandex</option>
              <option value="brave">brave</option>
            </select>
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:10px">Qual motor de busca priorizar. Auto mantém a cascata automática.</p>

          <hr style="border:none;border-top:1px solid var(--glass-border);margin:16px 0 12px">
          <h4 style="font-size:12px;color:var(--text-secondary);margin-bottom:10px">🎛️ Amostragem & Inferência Avançada</h4>

          <div class="config-item">
            <span class="config-label">num_batch (lote de avaliação)</span>
            <input type="number" class="config-input small" id="model-num-batch" min="1" step="1" value="" placeholder="auto">
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:8px">Tamanho do lote interno. Default do Ollama costuma ser bom. Ajustar só se souber o que está fazendo.</p>

          <div class="config-item">
            <span class="config-label">num_ubatch (lote físico)</span>
            <input type="number" class="config-input small" id="model-num-ubatch" min="1" step="1" value="" placeholder="auto">
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:8px">Batch size físico na GPU. Geralmente igual a num_batch ou menor. Default é suficiente.</p>

          <div class="config-item">
            <span class="config-label">seed (seed aleatória)</span>
            <input type="number" class="config-input small" id="model-seed" min="0" step="1" value="" placeholder="aleatório">
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:8px">Seed fixa para respostas determinísticas. Vazio = aleatório a cada chamada.</p>

          <div class="config-item">
            <span class="config-label">top_k</span>
            <input type="number" class="config-input small" id="model-top-k" min="1" step="1" value="" placeholder="auto">
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:8px">Amostragem top-k. 40 é padrão do Ollama. Menor = mais focado, maior = mais variado.</p>

          <div class="config-item">
            <span class="config-label">top_p</span>
            <input type="number" class="config-input small" id="model-top-p" min="0" max="1" step="0.05" value="" placeholder="auto">
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:8px">Amostragem top-p. 0.9 é padrão. Menor = mais concentrado nas palavras mais prováveis.</p>

          <div class="config-item">
            <span class="config-label">repeat_penalty</span>
            <input type="number" class="config-input small" id="model-repeat-penalty" min="0.1" max="2" step="0.05" value="" placeholder="auto">
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:8px">Penalidade de repetição. 1.1 é padrão. Maior = menos repetições, menor = mais criativo/repetitivo.</p>

          <div class="config-item">
            <span class="config-label">num_keep (tokens a manter)</span>
            <input type="number" class="config-input small" id="model-num-keep" min="0" step="1" value="" placeholder="auto">
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:8px">Tokens a manter no contexto quando truncado. 0 = padrão do modelo.</p>

          <div class="config-item">
            <span class="config-label">mirostat (controle de perplexidade)</span>
            <select class="config-input small" id="model-mirostat">
              <option value="">auto (desligado)</option>
              <option value="0">0 (desligado)</option>
              <option value="1">1 (v1)</option>
              <option value="2">2 (v2)</option>
            </select>
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:8px">Ajuste automático de temperatura durante geração. 0 = desligado (recomendado para a maioria dos casos).</p>

          <div class="config-item">
            <span class="config-label">mirostat_tau</span>
            <input type="number" class="config-input small" id="model-mirostat-tau" min="0.1" max="10" step="0.1" value="" placeholder="auto">
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:8px">Target perplexity do Mirostat. 5.0 é padrão.</p>

          <div class="config-item">
            <span class="config-label">mirostat_eta</span>
            <input type="number" class="config-input small" id="model-mirostat-eta" min="0.01" max="1" step="0.01" value="" placeholder="auto">
          </div>
          <p style="font-size:11px;color:var(--text-tertiary);margin-bottom:8px">Learning rate do Mirostat. 0.1 é padrão.</p>

        </div>

        <button class="btn-save" id="btn-salvar-model" onclick="salvarConfigModel()">💾 Salvar Model</button>
      </div>
    </div>

    <!-- SISTEMA -->
    <div class="config-panel" id="panel-sistema">
      <div class="config-section">
        <h3>Modelo de IA</h3>
        <div class="config-item">
          <span class="config-label">Modelo Atual</span>
          <select class="config-input small" id="config-modelo" onchange="trocarModeloConfig()">
            <option value="">Carregando...</option>
          </select>
        </div>
      </div>

      <div class="config-section">
        <h3>APIs Externas</h3>
        <div class="config-item">
          <span class="config-label">Ollama URL</span>
          <input type="text" class="config-input small" id="config-ollama-url" value="http://localhost:11434">
        </div>
        <div class="config-item">
          <span class="config-label">SD-API URL</span>
          <input type="text" class="config-input small" id="config-sd-url" value="http://localhost:8200">
        </div>
      </div>

      <div class="config-section">
        <h3>Sistema</h3>
        <div class="config-item">
          <span class="config-label">Status ChromaDB</span>
          <span class="config-input small" id="config-chroma-status">-</span>
        </div>
        <div class="config-item">
          <span class="config-label">Histórico</span>
          <span class="config-input small" id="config-historico-count">-</span>
        </div>
        <div class="config-item">
          <span class="config-label">🗑️ Apagar todo o histórico</span>
          <button class="btn-mini" id="btn-apagar-historico" onclick="apagarHistorico()" title="Apaga todas as sessões salvas em disco">Apagar</button>
        </div>
        <div class="config-item">
          <span class="config-label">Vídeo anexado (ffmpeg)</span>
          <label class="switch-mini">
            <input type="checkbox" id="switch-video" onchange="toggleVideo(this.checked)">
            <span class="slider"></span>
          </label>
        </div>
        <div class="config-item">
          <span class="config-label">🔊 Falar respostas automaticamente</span>
          <label class="switch-mini">
            <input type="checkbox" id="switch-auto-falar" onchange="toggleAutoFalar(this.checked)">
            <span class="slider"></span>
          </label>
        </div>
        <div class="config-item">
          <span class="config-label">🎨 Tema da janela</span>
          <select id="config-window-theme" onchange="aplicarTemaJanela(this.value)" class="config-input small">
            <option value="white">Branco</option>
            <option value="gray">Cinza</option>
            <option value="black">Preto</option>
          </select>
        </div>
        <div class="config-item">
          <span class="config-label">🔀 Trocar de modelo automaticamente</span>
          <label class="switch-mini">
            <input type="checkbox" id="switch-auto-modelo" onchange="toggleAutoModelo(this.checked)">
            <span class="slider"></span>
          </label>
        </div>
        <div class="config-item">
          <span class="config-label">🧹 Liberar VRAM agora (descarrega o modelo local)</span>
          <button class="btn-mini" id="btn-limpar-vram" onclick="limparVram()">Limpar</button>
        </div>
      </div>

      <button class="btn-save" id="btn-salvar-sistema" onclick="salvarConfig()">💾 Salvar Configurações</button>
    </div>
  </div>
</div>

<!-- Main Content -->
<main class="main-content">
  <!-- stat-badge (hidden, preserved for JS status updates) -->
  <span id="stat-dot" style="display:none"></span>
  <span id="stat-text" style="display:none">Pronto</span>
  
  <!-- Chat Container -->
  <div class="chat-container" id="chat">
    <div class="chat-zoom-wrapper" id="chat-zoom-wrapper">
    <!-- Welcome Screen (when empty) -->
    <div class="welcome-screen" id="welcome">
      <div class="welcome-logo"><img src="/imagem/_icones/jarvis_mascote.png" alt="Jarvis" style="width:100%;height:100%;object-fit:contain"></div>
      <h1 class="welcome-title">Bem-vindo ao Jarvis AI</h1>
      <p class="welcome-subtitle">
        Seu assistente pessoal de inteligência artificial com capacidades agênticas, 
        memória persistente e integração com ferramentas.
      </p>
      
      <div class="quick-actions">
        <div class="quick-action" onclick="quickAction('criar_arquivo')">
          <div class="quick-action-icon"><img src="/imagem/_icones/criar_arquivo.png" alt="Criar Arquivo" style="width:100%;height:100%;object-fit:contain"></div>
          <div class="quick-action-title">Criar Arquivo</div>
          <div class="quick-action-desc">Gere arquivos de texto, código ou documentos</div>
        </div>
        <div class="quick-action" onclick="quickAction('buscar_web')">
          <div class="quick-action-icon"><img src="/imagem/_icones/buscar.png" alt="Buscar na Web" style="width:100%;height:100%;object-fit:contain"></div>
          <div class="quick-action-title">Buscar na Web</div>
          <div class="quick-action-desc">Pesquise informações atualizadas na internet</div>
        </div>
        <div class="quick-action" onclick="analisarPasta()">
          <div class="quick-action-icon"><img src="/imagem/_icones/pasta_projeto.png" alt="Pasta" style="width:100%;height:100%;object-fit:contain"></div>
          <div class="quick-action-title">Analisar Projeto</div>
          <div class="quick-action-desc">Entenda a estrutura de uma pasta</div>
        </div>
        <div class="quick-action" onclick="quickAction('gerar_imagem')">
          <div class="quick-action-icon"><img src="/imagem/_icones/camera.png" alt="Gerar Imagem" style="width:100%;height:100%;object-fit:contain"></div>
          <div class="quick-action-title">Gerar Imagem</div>
          <div class="quick-action-desc">Crie imagens com IA a partir de descrições</div>
        </div>
        <div class="quick-action" onclick="verTela()" style="border-color:rgba(124,110,248,0.5);background:rgba(124,110,248,0.07)">
          <div class="quick-action-icon"><img src="/imagem/_icones/cerebro.jpg" alt="Ver Tela" style="width:100%;height:100%;object-fit:cover;border-radius:50%"></div>
          <div class="quick-action-title" style="color:var(--primary)">Jarvis vê a tela</div>
          <div class="quick-action-desc">Captura instantânea da tela e analisa</div>
        </div>
      </div>
    </div>
  </div>

    <!-- Input Area -->
    <div class="input-container">
      <div class="input-wrapper">
        <div class="anexos-preview" id="anexos-preview"></div>
        <input type="file" id="input-arquivo" accept="image/*,video/*,.pdf,.docx,.doc,.xlsx,.xlsm,.csv,.txt,.md" multiple style="display:none" onchange="handleArquivoSelecionado(event)">
        <div class="input-box" id="input-box" ondragover="event.preventDefault()" ondrop="handleDropArquivo(event)">
          <textarea
            id="input"
            placeholder="Digite sua mensagem... (ou arraste arquivos aqui)"
            rows="1"
            onkeydown="handleInput(event)"
            oninput="autoResize(this)"
          ></textarea>
          <div class="input-actions">
            <button class="btn-icon ghost minimal-plus" id="btn-tools-menu" title="Mais ações" onclick="toggleToolsDrawer()"><span>+</span></button>
            <button class="btn-icon secondary cor-enviar" id="send-btn" onclick="enviar()"><img class="btn-icon-img" src="/imagem/_icones/enviar.png" alt="Enviar"></button>
          </div>
        </div>
        <div class="input-toolbar-subtle">
          <button class="btn-discreto cerebro-mini cor-cerebro" id="btn-ver-tela" title="Jarvis vê sua tela" onclick="verTela()"><img src="/imagem/_icones/cerebro.jpg" alt="Ver Tela"></button>
          <button class="btn-discreto cor-planejamento" id="btn-planejamento" title="Chat Livre" onclick="togglePlanejamento()"><img src="/imagem/_icones/planejamento.png" alt="Chat Livre"></button>
          <button class="btn-discreto cor-anexar" id="btn-anexar" title="Anexar" onclick="document.getElementById('input-arquivo').click()"><img src="/imagem/_icones/anexar.png" alt="Anexar"></button>
          <button class="btn-discreto cor-print" id="btn-print-tela" title="Captura guiada" onclick="capturarPrintTela()">📸</button>
          <button class="btn-discreto cor-gravar" id="btn-gravar-tela" title="Gravação guiada" onclick="alternarGravacaoTela()">🎥</button>
          <button class="btn-discreto cor-microfone" id="btn-microfone" title="Falar" onclick="toggleMicrofone()"><img id="img-microfone" src="/imagem/_icones/microfone.png" alt="Microfone"></button>
          <div class="toolbar-spacer"></div>
          <div class="model-caps-mini" id="model-caps-panel" title="Capacidades do modelo ativo">
            <img class="cap-icon" id="cap-visao" src="/imagem/_icones/visao.png" alt="Visão">
            <img class="cap-icon" id="cap-web" src="/imagem/_icones/web.png" alt="Web">
            <img class="cap-icon" id="cap-pensamento" src="/imagem/_icones/pensamento.png" alt="Pensamento">
            <img class="cap-icon" id="cap-ferramentas" src="/imagem/_icones/ferramentas.png" alt="Ferramentas">
            <img class="cap-icon" id="cap-imagem" src="/imagem/_icones/visao.png" alt="Gerar Imagem">
          </div>
        </div>

        <!-- Botão de ação: PARAR + cronometro da tarefa -->
        <div class="input-action-buttons">
          <button class="btn-action" id="btn-parar" onclick="pararProcessamento()" style="display: none;">
            ⏹ PARAR
          </button>
          <div class="cronometro-tarefa" id="cronometro-tarefa">
            <span class="dot"></span>
            <span id="cronometro-texto">0:00</span>
          </div>
        </div>

      
    </div>
  </div>

  <!-- Drawer lateral de ferramentas -->
  <div class="drawer-overlay" id="tools-drawer-overlay" onclick="closeToolsDrawer()"></div>
  <aside class="tools-drawer" id="tools-drawer">
    <div class="drawer-header">
      <div class="drawer-title">Ferramentas</div>
      <button class="drawer-close" onclick="closeToolsDrawer()">×</button>
    </div>
    <div class="drawer-content">
      <div class="drawer-section">
        <div class="drawer-section-title">Captura & Visão</div>
        <button class="drawer-item" onclick="verTela(); closeToolsDrawer()">
          <div class="drawer-item-icon">
            <img src="/imagem/_icones/cerebro.jpg" alt="">
          </div>
          <div class="drawer-item-label">
            <div class="drawer-item-title">Ver tela inteira</div>
            <div class="drawer-item-subtitle">Captura imediata e anexa</div>
          </div>
        </button>
        <button class="drawer-item" id="drawer-btn-print-tela" onclick="capturarPrintTela(); closeToolsDrawer()">
          <div class="drawer-item-icon">📸</div>
          <div class="drawer-item-label">
            <div class="drawer-item-title">Captura guiada</div>
            <div class="drawer-item-subtitle">Selecionar área com o mouse</div>
          </div>
        </button>
        <button class="drawer-item" id="drawer-btn-gravar-tela" onclick="alternarGravacaoTela(); closeToolsDrawer()">
          <div class="drawer-item-icon">🎥</div>
          <div class="drawer-item-label">
            <div class="drawer-item-title">Gravação guiada</div>
            <div class="drawer-item-subtitle">Gravar área selecionada</div>
          </div>
        </button>
      </div>

      <div class="drawer-divider"></div>

      <div class="drawer-section">
        <div class="drawer-section-title">Entrada</div>
        <button class="drawer-item" onclick="document.getElementById('input-arquivo').click(); closeToolsDrawer()">
          <div class="drawer-item-icon">
            <img src="/imagem/_icones/anexar.png" alt="">
          </div>
          <div class="drawer-item-label">
            <div class="drawer-item-title">Anexar arquivo</div>
            <div class="drawer-item-subtitle">Imagem, vídeo, PDF, DOCX, TXT...</div>
          </div>
        </button>
        <button class="drawer-item" id="drawer-btn-microfone" onclick="toggleMicrofone(); closeToolsDrawer()">
          <div class="drawer-item-icon">
            <img src="/imagem/_icones/microfone.png" alt="">
          </div>
          <div class="drawer-item-label">
            <div class="drawer-item-title">Falar com microfone</div>
            <div class="drawer-item-subtitle">Transcrever e enviar</div>
          </div>
        </button>
      </div>

      <div class="drawer-divider"></div>

      <div class="drawer-section">
        <div class="drawer-section-title">Modo</div>
        <button class="drawer-item" id="drawer-btn-planejamento" onclick="togglePlanejamento(); closeToolsDrawer()">
          <div class="drawer-item-icon">
            <img src="/imagem/_icones/planejamento.png" alt="">
          </div>
          <div class="drawer-item-label">
            <div class="drawer-item-title">Chat Livre</div>
            <div class="drawer-item-subtitle">Modelo responde cru até confirmar</div>
          </div>
        </button>
      </div>

      <div class="drawer-divider"></div>

      <div class="drawer-caps">
        <div class="drawer-caps-header">
          <div class="drawer-caps-title">Capacidades do modelo</div>
        </div>
        <div class="drawer-caps-grid">
          <img class="cap-icon" id="cap-visao-drawer" src="/imagem/_icones/visao.png" alt="Visão" title="Visão">
          <img class="cap-icon" id="cap-web-drawer" src="/imagem/_icones/web.png" alt="Web" title="Busca Web">
          <img class="cap-icon" id="cap-pensamento-drawer" src="/imagem/_icones/pensamento.png" alt="Pensamento" title="Pensamento">
          <img class="cap-icon" id="cap-ferramentas-drawer" src="/imagem/_icones/ferramentas.png" alt="Ferramentas" title="Ferramentas">
          <img class="cap-icon" id="cap-imagem-drawer" src="/imagem/_icones/visao.png" alt="Gerar Imagem" title="Gerar Imagem">
        </div>
      </div>
    </div>
  </aside>
</main>

<script>
const chat = document.getElementById("chat");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
// 'welcome' é REATRIBUÍDA em renderWelcome() (Nova Conversa / excluir
// sessão recria o nó) — por isso 'let': com 'const', addMessage passava
// a esconder um nó órfão e a tela de boas-vindas ficava atrás das mensagens.
let welcome = document.getElementById("welcome");
const modelSelector = document.getElementById("model-selector");
// Elemento rolável do chat — usado por addMessage, addTypingIndicator,
// enviar() e renderWelcome(). Sem esta declaração global, quem chamasse
// "wrapper" fora de addMessage levava ReferenceError e o envio abortava
// antes do fetch("/chat") (o app "não chamava nenhum modelo").
const wrapper = document.getElementById("chat-zoom-wrapper") || chat;

// ── DIAGNÓSTICO: MutationObserver para capturar mudanças invisíveis ──
// Quando o usuário relata que elementos "desaparecem" ao digitar, mas
// nenhum JS modifica o DOM, o culpado costuma ser:
//   1. O navegador/WebView redimensionando o viewport (autofocus, zoom)
//   2. CSS externo injetando estilos inline
//   3. Transições não-intencionais em classes pai
// Este observador grava tudo no console pra gente investigar.
(function _iniciarDiagnosticoLayout() {
  const ALVO = document.getElementById("chat-zoom-wrapper");
  if (!ALVO) return;
  let _tick = 0;
  const obs = new MutationObserver(mutations => {
    for (const m of mutations) {
      if (m.type === "attributes") {
        const el = m.target;
        const tag = el.tagName + (el.id ? "#" + el.id : "") + (el.className ? "." + el.className.split(" ")[0] : "");
        console.debug("[diag-layout]", ++_tick, "attr mudou em", tag, ":", m.attributeName, "->", el.getAttribute(m.attributeName));
        // Se alguém alterar display/visibility/opacity no wrapper, reage
        if (m.attributeName === "style" && el === ALVO) {
          const s = el.style;
          console.warn("[diag-layout] ⚠ style do wrapper alterado!",
            "display=", s.display, "visibility=", s.visibility, "opacity=", s.opacity,
            "height=", s.height, "max-height=", s.maxHeight, "overflowY=", s.overflowY, "overflow=", s.overflow);
          // Corrige imediatamente a barra de rolagem
          if (s.overflowY === 'hidden' || s.overflow === 'hidden') {
            s.overflowY = 'auto';
            s.overflowX = 'hidden';
            console.warn('[diag-layout] overflow corrigido para auto');
          }
          if (getComputedStyle(el).overflowY === 'hidden') {
            s.overflowY = 'auto';
            s.overflowX = 'hidden';
            console.warn('[diag-layout] overflow corrigido via computedStyle');
          }
        }
      } else if (m.type === "childList") {
        for (const n of m.addedNodes) {
          if (n.nodeType === 1) console.debug("[diag-layout]", ++_tick, "nó adicionado:", n.tagName + (n.id ? "#" + n.id : ""));
        }
        for (const n of m.removedNodes) {
          if (n.nodeType === 1) console.debug("[diag-layout]", ++_tick, "nó removido:", n.tagName + (n.id ? "#" + n.id : ""));
        }
      }
    }
  });
  // Observa o wrapper E seus pais imediatos (chat-container e main-content)
  const pais = [ALVO, ALVO.parentElement, ALVO.parentElement?.parentElement].filter(Boolean);
  pais.forEach(p => p && obs.observe(p, {attributes: true, childList: true, subtree: true}));
  console.debug("[diag-layout] Observador ativado nos nós:", pais.map(e => e.tagName + (e.id ? "#" + e.id : "")).join(", "));

  // Observer dedicado APENAS pro wrapper pra garantir correção instantânea.
  // NOTA: o #chat-container DEVE continuar overflow:hidden (é o design
  // original — o scroll vive só no wrapper); aqui só se garante o wrapper.
  const obsWrap = new MutationObserver(() => {
    _forcarOverflowWrapper();
  });
  obsWrap.observe(ALVO, {attributes: true, attributeFilter: ['style', 'class']});

  // ── FIX: mantém a barra de rolagem do wrapper sempre ativa ──
  function _forcarOverflowWrapper() {
    const wrap = document.getElementById('chat-zoom-wrapper');
    if (!wrap) return;
    const precisa = wrap.style.overflowY === 'hidden' ||
                    wrap.style.overflow === 'hidden' ||
                    getComputedStyle(wrap).overflowY === 'hidden';
    if (precisa) {
      wrap.style.setProperty('overflow-y', 'auto', 'important');
      wrap.style.setProperty('overflow-x', 'hidden', 'important');
      console.warn('[scroll-fix] overflowY do wrapper forçado para auto !important');
      return true;
    }
    // Sempre garante overflow-y:auto declarado inline (mesmo valor do CSS,
    // mas blindado contra qualquer reescrita futura)
    if (wrap.style.overflowY !== 'auto') {
      wrap.style.setProperty('overflow-y', 'auto', 'important');
      wrap.style.setProperty('overflow-x', 'hidden', 'important');
      return true;
    }
    return false;
  }

  function _garantirScrollbarChat() {
    _forcarOverflowWrapper();
  }

  // Força a cada 200ms (barato: checagem de 1 elemento) e nos eventos do input
  setInterval(_garantirScrollbarChat, 200);

  const _inputEl = document.getElementById('input');
  if (_inputEl) {
    ['focus', 'focusin', 'focusout', 'input', 'keydown', 'keyup', 'paste', 'compositionstart', 'compositionend', 'click'].forEach(ev => {
      _inputEl.addEventListener(ev, _garantirScrollbarChat, { passive: true });
    });
  }

  // Força ao carregar e após pequeno delay
  _garantirScrollbarChat();
  setTimeout(_garantirScrollbarChat, 250);
  setTimeout(_garantirScrollbarChat, 1000);
})();

// Elemento que realmente tem a barra de rolagem do chat (o .chat-container
// ficou overflow:hidden; o scroll vive no .chat-zoom-wrapper interno, desde
// que o recurso de zoom foi adicionado). Sem isto, tanto o auto-scroll do
// JS quanto a rolagem do mouse não funcionam.
function _scrollChatParaFim(forcar = false) {
  const wrapper = document.getElementById("chat-zoom-wrapper") || chat;
  if (!wrapper) return;
  // Auto-scroll sempre para mensagens novas do assistente (forcar=true).
  // Para interação do usuário (scroll manual), só sobe se já estiver no
  // topo ou perto do final — evita "atropelar" leitura de histórico.
  const nearBottom = wrapper.scrollTop + wrapper.clientHeight >= wrapper.scrollHeight - 10;
  const atTop = wrapper.scrollTop <= 0;
  if (forcar || nearBottom || atTop) {
    wrapper.scrollTop = wrapper.scrollHeight;
  }
}

// Tema da janela (white/gray/black)
const _WINDOW_THEME_KEY = "jarvis_window_theme";
function aplicarTemaJanela(t) {
  const tema = (t === "white" || t === "gray" || t === "black") ? t : "gray";
  document.documentElement.setAttribute("data-window-theme", tema);
  try { localStorage.setItem(_WINDOW_THEME_KEY, tema); } catch(e) {}
}

(function initTemaJanela(){
  try {
    const salvo = localStorage.getItem(_WINDOW_THEME_KEY);
    aplicarTemaJanela(salvo || "gray");
  } catch(e) {
    aplicarTemaJanela("gray");
  }
})();

function toggleToolsDrawer() {
  const overlay = document.getElementById("tools-drawer-overlay");
  const drawer = document.getElementById("tools-drawer");
  if (!overlay || !drawer) return;
  const ativo = drawer.classList.contains("active");
  if (ativo) {
    closeToolsDrawer();
  } else {
    overlay.classList.add("active");
    drawer.classList.add("active");
  }
}

function closeToolsDrawer() {
  const overlay = document.getElementById("tools-drawer-overlay");
  const drawer = document.getElementById("tools-drawer");
  if (overlay) overlay.classList.remove("active");
  if (drawer) drawer.classList.remove("active");
}

// Sincroniza estados visuais do drawer com os botões originais
function syncDrawerStates() {
  const btnPlanejamento = document.getElementById("btn-planejamento");
  const drawerPlanejamento = document.getElementById("drawer-btn-planejamento");
  if (btnPlanejamento && drawerPlanejamento) {
    drawerPlanejamento.classList.toggle("active", btnPlanejamento.classList.contains("planejamento-ativo"));
    btnPlanejamento.classList.toggle("ativo", btnPlanejamento.classList.contains("planejamento-ativo"));
  } else if (btnPlanejamento) {
    btnPlanejamento.classList.toggle("ativo", btnPlanejamento.classList.contains("planejamento-ativo"));
  }

  const btnGravar = document.getElementById("btn-gravar-tela");
  const drawerGravar = document.getElementById("drawer-btn-gravar-tela");
  if (btnGravar && drawerGravar) {
    drawerGravar.classList.toggle("active", btnGravar.classList.contains("gravando"));
    btnGravar.classList.toggle("ativo", btnGravar.classList.contains("gravando") || btnGravar.classList.contains("aguardando"));
    btnGravar.classList.toggle("gravando", btnGravar.classList.contains("gravando"));
    btnGravar.classList.toggle("aguardando", btnGravar.classList.contains("aguardando"));
  } else if (btnGravar) {
    btnGravar.classList.toggle("ativo", btnGravar.classList.contains("gravando") || btnGravar.classList.contains("aguardando"));
    btnGravar.classList.toggle("gravando", btnGravar.classList.contains("gravando"));
    btnGravar.classList.toggle("aguardando", btnGravar.classList.contains("aguardando"));
  }

  const btnPrint = document.getElementById("btn-print-tela");
  const drawerPrint = document.getElementById("drawer-btn-print-tela");
  if (btnPrint && drawerPrint) {
    drawerPrint.classList.toggle("active", btnPrint.classList.contains("aguardando"));
    btnPrint.classList.toggle("ativo", btnPrint.classList.contains("aguardando"));
    btnPrint.classList.toggle("aguardando", btnPrint.classList.contains("aguardando"));
  } else if (btnPrint) {
    btnPrint.classList.toggle("ativo", btnPrint.classList.contains("aguardando"));
    btnPrint.classList.toggle("aguardando", btnPrint.classList.contains("aguardando"));
  }

  const btnMicrofone = document.getElementById("btn-microfone");
  const drawerMicrofone = document.getElementById("drawer-btn-microfone");
  if (btnMicrofone && drawerMicrofone) {
    drawerMicrofone.classList.toggle("active", btnMicrofone.classList.contains("gravando"));
    btnMicrofone.classList.toggle("ativo", btnMicrofone.classList.contains("gravando"));
    btnMicrofone.classList.toggle("gravando", btnMicrofone.classList.contains("gravando"));
  } else if (btnMicrofone) {
    btnMicrofone.classList.toggle("ativo", btnMicrofone.classList.contains("gravando"));
    btnMicrofone.classList.toggle("gravando", btnMicrofone.classList.contains("gravando"));
  }
}

// Sincroniza capacidades do modelo no drawer
function syncDrawerCapacidades() {
  const map = [
    ["cap-visao", "cap-visao-drawer"],
    ["cap-web", "cap-web-drawer"],
    ["cap-pensamento", "cap-pensamento-drawer"],
    ["cap-ferramentas", "cap-ferramentas-drawer"],
    ["cap-imagem", "cap-imagem-drawer"],
  ];
  for (const [orig, dest] of map) {
    const o = document.getElementById(orig);
    const d = document.getElementById(dest);
    if (o && d) {
      d.classList.toggle("ativo", o.classList.contains("ativo"));
    }
  }
}
let currentSessionId = null;
let liveMetrics = {tokens_saida: 0, tokens_entrada: 0, tempo_s: 0, tokens_por_segundo: 0, contexto_tokens: 0};

// 🔧 Auto-abrir aba Model se URL tiver ?tab=model (usado pelo jarvis-configurar.bat)
(function() {
  const params = new URLSearchParams(window.location.search);
  if (params.get("tab") === "model") {
    // espera o DOM estar pronto e abre o modal na aba Model
    document.addEventListener("DOMContentLoaded", function() {
      setTimeout(function() {
        const modal = document.getElementById("modal-config");
        const tabBtn = document.querySelector('.config-tab[data-tab="model"]');
        if (modal && tabBtn) {
          modal.classList.add("open");
          mudarTabConfig("model", tabBtn);
        }
      }, 300);
    });
  }
})();

// ─────────────────────────────────────────
// ANEXOS (imagem / vídeo) — pendentes de envio na próxima mensagem
// ─────────────────────────────────────────
let anexosAtuais = []; // {nome, caminho, tipo, enviando}
let videoAtivoAtual = true; // espelha config.VIDEO_ATIVO, sincronizado via /status

const _EXT_DOC_ICONES = {
  pdf: "📕", docx: "📘", doc: "📘", xlsx: "📗", xlsm: "📗",
  csv: "📊", txt: "📄", md: "📄"
};

function _iconeAnexo(a) {
  if (a.tipo === "video") return "🎬";
  if (a.tipo === "imagem") return "🖼️";
  const ext = (a.nome || "").split(".").pop().toLowerCase();
  return _EXT_DOC_ICONES[ext] || "📄";
}

function renderAnexosPreview() {
  const el = document.getElementById("anexos-preview");
  el.innerHTML = anexosAtuais.map((a, i) => `
    <div class="chip-anexo ${a.enviando ? "enviando" : ""}" ${a.auto ? 'title="Anexado automaticamente (print/gravação recente)"' : ""}>
      <span>${a.auto ? "📸" : _iconeAnexo(a)}</span>
      <span class="nome">${escapeHtml(a.nome)}</span>
      <span class="remover" onclick="removerAnexo(${i})">✕</span>
    </div>
  `).join("");
  const temImagem = anexosAtuais.some(a => a.tipo !== "documento");
  if (temImagem && _ultimoStatus && _ultimoStatus.visao === false) {
    el.innerHTML += `<div class="aviso-visao">&#x26A0;&#xFE0F; Este modelo não analisa imagens. Troque para um modelo com visão — local: <b>qwen2.5vl</b> ou <b>minicpm-v</b>; nuvem: Gemini, Claude.</div>`;
  }
}

function removerAnexo(i) {
  anexosAtuais.splice(i, 1);
  renderAnexosPreview();
}

function _tipoChuteArquivo(file) {
  if (file.type.indexOf("video") !== -1) return "video";
  if (file.type.indexOf("image") !== -1) return "imagem";
  const ext = (file.name || "").split(".").pop().toLowerCase();
  if (["pdf", "docx", "doc", "xlsx", "xlsm", "csv", "txt", "md"].includes(ext)) return "documento";
  return "imagem";
}

async function subirAnexo(file) {
  if (!file) return;
  const tipoChute = _tipoChuteArquivo(file);
  const idx = anexosAtuais.push({nome: file.name || "anexo", caminho: "", tipo: tipoChute, enviando: true}) - 1;
  renderAnexosPreview();

  try {
    const fd = new FormData();
    fd.append("arquivo", file, file.name || `anexo_${Date.now()}.png`);
    const r = await fetch("/api/anexo", {method: "POST", body: fd});
    const d = await r.json();
    if (d.ok) {
      anexosAtuais[idx] = {nome: d.nome, caminho: d.caminho, tipo: d.tipo, tamanho_kb: d.tamanho_kb, enviando: false};
    } else {
      anexosAtuais.splice(idx, 1);
      alert(d.erro || "Falha ao enviar anexo.");
    }
  } catch (e) {
    anexosAtuais.splice(idx, 1);
    alert("Erro de conexão ao enviar anexo: " + e);
  }
  renderAnexosPreview();
}

function handleArquivoSelecionado(event) {
  Array.from(event.target.files || []).forEach(subirAnexo);
  event.target.value = ""; // permite escolher o mesmo arquivo de novo depois
}

function handleDropArquivo(event) {
  event.preventDefault();
  Array.from((event.dataTransfer && event.dataTransfer.files) || []).forEach(subirAnexo);
}

// ─────────────────────────────────────────
// UTILS
// ─────────────────────────────────────────

function escapeHtml(t) {
  return String(t)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

// Nome curto pra exibir na interface: pega só o arquivo final de um
// caminho — funciona tanto pra modelos do Ollama/LM Studio ("qwen3:8b")
// quanto pra caminhos .gguf do KoboldCpp ("C:\...\modelo.gguf").
function _nomeCurtoModelo(nome) {
  if (!nome) return nome;
  return String(nome).split(/[\\/]/).pop();
}

// Renderização real de Markdown (negrito, itálico, títulos, listas,
// links, tabelas, blocos de código...) via marked.js, sanitizado com
// DOMPurify antes de entrar no DOM (a resposta do modelo é conteúdo
// não-confiável — sem isso um HTML malicioso na resposta rodaria).
if (typeof marked !== "undefined") {
  marked.setOptions({ breaks: true, gfm: true });
}

function formatMessage(t) {
  if (typeof marked === "undefined" || typeof DOMPurify === "undefined") {
    // CDN falhou em carregar (ex: sem internet) — cai pro texto simples
    // escapado, pelo menos não quebra a tela
    return escapeHtml(t).replace(/\n/g, "<br>");
  }
  const html = marked.parse(t || "");
  return DOMPurify.sanitize(html);
}

  function autoResize(textarea) {
  // Mantém a altura fixa por enquanto para evitar que altere o layout do chat e suma a barra
  // textarea.style.height = "auto";
  // textarea.style.height = Math.min(textarea.scrollHeight, 200) + "px";
  // Esconde os cards de boas-vindas enquanto o usuário digita
  const hasText = textarea.value.trim().length > 0;
  // Ocultação da tela de boas-vindas desativada para teste
  // document.querySelectorAll('.welcome-screen').forEach(el =>
  //   el.classList.toggle('typing', hasText));

  // ── Layout Guard (2026-10-24) ──────────────────────────────────────
  // Previne que qualquer transformação de CSS/WebView (IME composition,
  // autofill zoom, etc.) oculte o contêiner principal ao digitar.
  // Também garante que a barra de rolagem do chat nunca seja escondida.
  const guard = () => {
    ['main-content', 'chat-container', 'input-container'].forEach(id => {
      const el = document.getElementById(id) || document.querySelector('.' + id);
      if (!el) return;
      const s = el.style;
      // Se o elemento estiver "desaparecendo", reestabelece
      if (s.display === 'none' || s.visibility === 'hidden') {
        s.display = '';
        s.visibility = '';
        console.warn('[layout-guard]', id, 'foi reestabelecido de', id + '.display/visibility');
      }
      // Garante que opacity nunca fique zero por acidente
      if (s.opacity !== '' && parseFloat(s.opacity) <= 0.01) {
        s.opacity = '';
        console.warn('[layout-guard]', id, 'foi reestabelecido de opacity');
      }
    });

    // Força a barra de rolagem do chat a permanecer visível
    const wrap = document.getElementById('chat-zoom-wrapper');
    if (wrap) {
      const ws = wrap.style;
      if (ws.overflowY === 'hidden' || ws.overflow === 'hidden' || ws.overflowX === 'hidden' && ws.overflowY === '') {
        ws.overflowY = 'auto';
        ws.overflowX = 'hidden';
      }
      if (getComputedStyle(wrap).overflowY === 'hidden') {
        ws.overflowY = 'auto';
        ws.overflowX = 'hidden';
      }
      // Garante scroll suave e funcional
      if (ws.scrollBehavior !== 'smooth') {
        ws.scrollBehavior = 'smooth';
      }
    }
  };
  guard();
}

  function handleInput(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      enviar();
    }
  }
  
  // Suporte a colar imagem (Ctrl+V) — vira anexo de verdade (mesmo
  // pipeline do 📎/arrastar), não mais um texto tipo "[Imagem colada: ...]"
  // que o modelo nunca conseguia realmente enxergar.
  input.addEventListener('paste', function(e) {
    const items = e.clipboardData.items;
    for (let i = 0; i < items.length; i++) {
      if (items[i].type.indexOf('image') !== -1) {
        e.preventDefault();
        subirAnexo(items[i].getAsFile());
        break;
      }
    }
  });

function handleSearch(e) {
  if (e.key === "Enter") {
    const query = e.target.value.trim();
    if (query) buscarHistorico(query);
  }
}

// toggleSidebar() removido (layout top-rail)

function toggleHistoricoDropdown() {
  const dd = document.getElementById("hist-dropdown");
  const btn = document.getElementById("btn-historico");
  const isOpen = dd.classList.toggle("open");
  btn.classList.toggle("active", isOpen);
  if (isOpen) {
    // close on outside click
    setTimeout(() => {
      document.addEventListener("click", function closeDD(e) {
        if (!dd.contains(e.target) && e.target !== btn) {
          dd.classList.remove("open");
          btn.classList.remove("active");
          document.removeEventListener("click", closeDD);
        }
      });
    }, 0);
  }
}

// ─────────────────────────────────────────
// CONFIGURAÇÕES
// ─────────────────────────────────────────

function abrirConfig() {
  carregarDadosConfig();
  carregarCerebros();
  carregarConfigModel();
  _preencherDropdownVoz();
  try {
    const selTema = document.getElementById("config-window-theme");
    if (selTema) {
      const atual = document.documentElement.getAttribute("data-window-theme") || localStorage.getItem("jarvis_window_theme") || "gray";
      selTema.value = (atual === "white" || atual === "gray" || atual === "black") ? atual : "gray";
    }
  } catch(e) {}
  document.getElementById("modal-config").classList.add("open");
}

// ─────────────────────────────────────────
// CONFIG — abas
// ─────────────────────────────────────────

function mudarTabConfig(nome, botao) {
  document.querySelectorAll(".config-tab").forEach(b => b.classList.remove("active"));
  document.querySelectorAll(".config-panel").forEach(p => p.classList.remove("active"));
  botao.classList.add("active");
  document.getElementById("panel-" + nome).classList.add("active");

  if (nome === "cerebros") carregarCerebros();
  else if (nome === "mcp") carregarMcp();
  else if (nome === "plugins") carregarPlugins();
  else if (nome === "pastas") carregarPastas();
  else if (nome === "model") carregarConfigModel();
}

// ─────────────────────────────────────────
// CONFIG — Model (Prompt do Sistema, Temperature, Chamada de função, max_tokens)
// ─────────────────────────────────────────

async function carregarConfigModel() {
  try {
    const p = await (await fetch("/api/model_params")).json();
    document.getElementById("model-prompt-sistema").value = p.prompt_sistema_extra || "";
    document.getElementById("model-temperature").value = p.temperature ?? 0.7;
    document.getElementById("model-temp-valor").textContent = p.temperature ?? 0.7;
    document.getElementById("model-function-calling").checked = p.function_calling !== false;
    document.getElementById("model-max-tokens").value = p.max_tokens ?? 0;
    // avançados
    document.getElementById("model-num-gpu").value = p.num_gpu != null ? p.num_gpu : "";
    _sincronizarSliderDoCampo(p.num_gpu);
    document.getElementById("model-num-ctx").value = p.num_ctx != null ? p.num_ctx : "";
    document.getElementById("model-num-thread").value = p.num_thread != null ? p.num_thread : "";
    document.getElementById("model-use-mmap").checked = p.use_mmap !== false;
    document.getElementById("model-use-mlock").checked = p.use_mlock !== false;
    document.getElementById("model-pensar-modo").value = p.pensar_modo || "auto";
    document.getElementById("model-mtp").value = p.mtp_draft_num_predict != null ? p.mtp_draft_num_predict : "";
    document.getElementById("model-ssqp-ativo").checked = p.ssqp_ativo === true;
    document.getElementById("model-motor-busca").value = p.motor_busca || "auto";
    // avançados de inferência
    document.getElementById("model-num-batch").value = p.num_batch != null ? p.num_batch : "";
    document.getElementById("model-num-ubatch").value = p.num_ubatch != null ? p.num_ubatch : "";
    document.getElementById("model-seed").value = p.seed != null ? p.seed : "";
    document.getElementById("model-top-k").value = p.top_k != null ? p.top_k : "";
    document.getElementById("model-top-p").value = p.top_p != null ? p.top_p : "";
    document.getElementById("model-repeat-penalty").value = p.repeat_penalty != null ? p.repeat_penalty : "";
    document.getElementById("model-num-keep").value = p.num_keep != null ? p.num_keep : "";
    document.getElementById("model-mirostat").value = p.mirostat != null ? p.mirostat : "";
    document.getElementById("model-mirostat-tau").value = p.mirostat_tau != null ? p.mirostat_tau : "";
    document.getElementById("model-mirostat-eta").value = p.mirostat_eta != null ? p.mirostat_eta : "";
    carregarDadosModelo(true);
    _rotuloCamadas(document.getElementById("model-num-gpu").value.trim());
  } catch (e) {
    console.error("Erro ao carregar parâmetros do Model:", e);
  }
}

async function salvarConfigModel() {
  const btn = document.getElementById("btn-salvar-model");
  const textoOriginal = btn.textContent;

  function numOrEmpty(id) {
    const v = document.getElementById(id).value.trim();
    return v === "" ? null : parseInt(v, 10);
  }

  const dados = {
    prompt_sistema_extra: document.getElementById("model-prompt-sistema").value,
    temperature: parseFloat(document.getElementById("model-temperature").value),
    function_calling: document.getElementById("model-function-calling").checked,
    max_tokens: parseInt(document.getElementById("model-max-tokens").value || "0", 10),
    // avançados (só envia se tiver valor explícito)
    num_gpu: numOrEmpty("model-num-gpu"),
    num_ctx: numOrEmpty("model-num-ctx"),
    num_thread: numOrEmpty("model-num-thread"),
    use_mmap: document.getElementById("model-use-mmap").checked,
    use_mlock: document.getElementById("model-use-mlock").checked,
    pensar_modo: document.getElementById("model-pensar-modo").value,
    mtp_draft_num_predict: numOrEmpty("model-mtp"),
    ssqp_ativo: document.getElementById("model-ssqp-ativo").checked,
    motor_busca: document.getElementById("model-motor-busca").value,
    // avançados de inferência
    num_batch: numOrEmpty("model-num-batch"),
    num_ubatch: numOrEmpty("model-num-ubatch"),
    seed: numOrEmpty("model-seed"),
    top_k: numOrEmpty("model-top-k"),
    top_p: parseFloat(document.getElementById("model-top-p").value) || null,
    repeat_penalty: parseFloat(document.getElementById("model-repeat-penalty").value) || null,
    num_keep: numOrEmpty("model-num-keep"),
    mirostat: numOrEmpty("model-mirostat"),
    mirostat_tau: parseFloat(document.getElementById("model-mirostat-tau").value) || null,
    mirostat_eta: parseFloat(document.getElementById("model-mirostat-eta").value) || null
  };

  try {
    const r = await fetch("/api/model_params", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(dados)
    });
    const resp = await r.json();

    if (r.ok && resp.ok) {
      btn.textContent = "✅ Salvo!";
      btn.style.background = "linear-gradient(135deg, #10B981 0%, #059669 100%)";
      setTimeout(() => {
        btn.textContent = textoOriginal;
        btn.style.background = "";
      }, 2000);
    } else {
      alert("Erro ao salvar: " + (resp.erro || "desconhecido"));
    }
  } catch (e) {
    alert("Erro ao salvar: " + e);
  }
}

function toggleAvancados() {
  const el = document.getElementById("model-avancados");
  if (el) {
    el.style.display = el.style.display === "none" ? "block" : "none";
  }
}

// ─────────────────────────────────────────
// CONFIG — Model: slider de camadas na GPU (num_gpu) + dados do modelo
// ─────────────────────────────────────────
let _camadasTotal = null;

function _rotuloCamadas(valor) {
  const el = document.getElementById("model-camadas-total");
  if (!el) return;
  const total = _camadasTotal;
  if (valor === "" || valor == null) {
    el.textContent = total ? `(auto — máx ${total})` : "(auto)";
  } else {
    el.textContent = total ? `(${valor} de ${total} camadas)` : `(${valor} camadas)`;
  }
}

function _sincronizarSliderDoCampo(v) {
  const slider = document.getElementById("model-num-gpu-slider");
  if (slider && v != null && v !== "") {
    slider.value = v;
  }
}

function sincronizarNumGpuSlider(v) {
  document.getElementById("model-num-gpu").value = v;
  _rotuloCamadas(v);
}

function sincronizarNumGpuCampo(v) {
  const slider = document.getElementById("model-num-gpu-slider");
  const n = parseInt(v, 10);
  if (slider && !isNaN(n)) slider.value = Math.min(n, parseInt(slider.max, 10));
  _rotuloCamadas((v || "").trim());
}

function setNumGpuAuto() {
  document.getElementById("model-num-gpu").value = "";
  document.getElementById("model-num-gpu-slider").value = 0;
  _rotuloCamadas("");
}

function _fmtBytes(b) {
  if (b == null) return "—";
  const gb = b / (1024 * 1024 * 1024);
  return gb >= 1 ? gb.toFixed(2) + " GB" : (b / (1024 * 1024)).toFixed(0) + " MB";
}

function _htmlDadosModelo(info, vram, sugestao) {
  if (!info || info.erro) {
    return `<span style="color:#f87171">${(info && info.erro) || "Sem dados do modelo."}</span>`;
  }
  if (info.local === false) {
    return "Cérebro de nuvem — não há arquivo local pra detalhar (camadas/quantização não se aplicam).";
  }
  const linhas = [
    ["Modelo", info.modelo],
    ["Arquitetura", info.familia],
    ["Parâmetros", info.parametros],
    ["Quantização", info.quantizacao],
    ["Camadas", info.n_camadas],
    ["Contexto de treino", info.contexto_treino],
    ["Tamanho em disco", _fmtBytes(info.tamanho_bytes)],
    ["Visão", info.suporta_visao ? "sim" : "não"],
    ["Ferramentas", info.suporta_tools ? "sim" : "não"],
  ];
  if (vram) {
    linhas.push(["VRAM livre agora", `${_fmtBytes(vram.livre_bytes)} de ${_fmtBytes(vram.total_bytes)}`]);
  }
  if (sugestao != null) {
    linhas.push(["Sugestão (VRAM)", `${sugestao}${info.n_camadas ? " de " + info.n_camadas : ""} camadas`]);
  }
  const corpo = linhas
    .filter(l => l[1] != null && l[1] !== "")
    .map(l => `<tr><td style="padding:2px 10px 2px 0;color:var(--text-tertiary);white-space:nowrap">${l[0]}</td><td style="padding:2px 0">${l[1]}</td></tr>`)
    .join("");
  const botaoSug = sugestao != null
    ? `<button class="btn-mini" style="margin-top:8px" onclick="usarSugestaoCamadas(${sugestao})">Usar sugestão (${sugestao})</button>`
    : "";
  return `<table style="width:100%;border-collapse:collapse">${corpo}</table>${botaoSug}`;
}

function usarSugestaoCamadas(v) {
  document.getElementById("model-num-gpu").value = v;
  sincronizarNumGpuCampo(String(v));
}

async function carregarDadosModelo(silencioso) {
  const painel = document.getElementById("model-dados-panel");
  if (!silencioso && painel) {
    painel.style.display = "block";
    painel.innerHTML = "<em>Lendo dados do modelo carregado…</em>";
  }
  try {
    const d = await (await fetch("/api/modelo_info")).json();
    const info = d.info || {};
    if (info.n_camadas) {
      _camadasTotal = info.n_camadas;
      const slider = document.getElementById("model-num-gpu-slider");
      if (slider) {
        slider.max = info.n_camadas;
        if (document.getElementById("model-num-gpu").value.trim() === "") slider.value = 0;
      }
    }
    _rotuloCamadas(document.getElementById("model-num-gpu").value.trim());
    if (!silencioso && painel) painel.innerHTML = _htmlDadosModelo(info, d.vram, d.sugestao_num_gpu);
  } catch (e) {
    if (!silencioso && painel) {
      painel.innerHTML = `<span style="color:#f87171">Erro ao ler dados do modelo: ${e}</span>`;
    }
  }
}

// ─────────────────────────────────────────
// CONFIG — Cérebros de nuvem
// ─────────────────────────────────────────

function toggleFormCerebro() {
  const f = document.getElementById("form-cerebro");
  f.style.display = f.style.display === "none" ? "flex" : "none";
}

async function carregarCerebros() {
  const el = document.getElementById("lista-cerebros");
  try {
    const dados = await (await fetch("/api/cerebros")).json();
    const nomes = Object.keys(dados);
    if (!nomes.length) {
      el.innerHTML = `<p style="color:var(--text-tertiary);font-size:13px">Nenhum cérebro configurado ainda.</p>`;
      return;
    }
    el.innerHTML = nomes.map(nome => {
      const c = dados[nome] || {};
      const modelos = Array.isArray(c.modelos) ? c.modelos : [];
      const qtd = modelos.length;
      const origem = (c.url || c.url_env) ? "via .env" : (c.url || "");
      return `
        <div class="config-list-item">
          <div class="info">
            <div class="titulo">${escapeHtml(nome)} ${c.ativo === false ? "<span style='color:var(--text-tertiary);font-size:11px'>(desligado)</span>" : ""}</div>
            <div class="detalhe">${escapeHtml(origem)}${qtd ? ` — ${qtd} modelo${qtd > 1 ? "s" : ""}` : ""}</div>
          </div>
          <div class="acoes">
            <button class="btn-mini danger" onclick="removerCerebro('${escapeHtml(nome)}')">🗑️</button>
          </div>
        </div>`;
    }).join("");
  } catch (e) {
    el.innerHTML = `<p style="color:#EF4444;font-size:13px">Erro ao carregar: ${e}</p>`;
  }
}

async function salvarCerebro() {
  const nome = document.getElementById("cb-nome").value.trim();
  const tipo = document.getElementById("cb-tipo").value;
  const url = document.getElementById("cb-url").value.trim();
  const chave = document.getElementById("cb-chave").value.trim();
  const modelos = document.getElementById("cb-modelos").value.split(",").map(s => s.trim()).filter(Boolean);

  if (!nome || !url || !modelos.length) {
    alert("Preencha nome, URL e pelo menos 1 modelo.");
    return;
  }

  try {
    const r = await fetch("/api/cerebros", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({nome, tipo, url, chave, modelos, ativo: true})
    });
    const d = await r.json();
    if (!d.ok) { alert(d.erro || "Falha ao salvar."); return; }
    document.getElementById("form-cerebro").style.display = "none";
    ["cb-nome","cb-url","cb-chave","cb-modelos"].forEach(id => document.getElementById(id).value = "");
    carregarCerebros();
    carregarModelos(); // atualiza o dropdown do header também
  } catch (e) {
    alert("Erro de conexão: " + e);
  }
}

async function removerCerebro(nome) {
  if (!confirm(`Remover o cérebro "${nome}"?`)) return;
  await fetch("/api/cerebros/" + encodeURIComponent(nome), {method: "DELETE"});
  carregarCerebros();
  carregarModelos();
}

async function descobrirModelosCerebro() {
  const url = document.getElementById("cb-url").value.trim();
  const chave = document.getElementById("cb-chave").value.trim();
  if (!url) { alert("Preencha a URL primeiro."); return; }
  try {
    const r = await fetch("/api/cerebros/descobrir", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({url, chave})
    });
    const d = await r.json();
    if (!d.ok) { alert(d.erro || "Não consegui descobrir os modelos."); return; }
    document.getElementById("cb-modelos").value = d.modelos.join(", ");
  } catch (e) {
    alert("Erro de conexão: " + e);
  }
}

// ─────────────────────────────────────────
// CONFIG — MCP
// ─────────────────────────────────────────

function toggleFormMcp() {
  const f = document.getElementById("form-mcp");
  f.style.display = f.style.display === "none" ? "flex" : "none";
}

async function carregarMcp() {
  const el = document.getElementById("lista-mcp");
  try {
    const dados = await (await fetch("/api/mcp")).json();
    const nomes = Object.keys(dados);
    if (!nomes.length) {
      el.innerHTML = `<p style="color:var(--text-tertiary);font-size:13px">Nenhum servidor MCP configurado ainda.</p>`;
      return;
    }
    el.innerHTML = nomes.map(nome => {
      const s = dados[nome] || {};
      return `
        <div class="config-list-item">
          <div class="info">
            <div class="titulo">${escapeHtml(nome)} ${s.ativo === false ? "(desligado)" : ""}</div>
            <div class="detalhe">${escapeHtml(s.command || "")} ${escapeHtml((s.args || []).join(" "))}</div>
          </div>
          <div class="acoes">
            <button class="btn-mini danger" onclick="removerMcp('${escapeHtml(nome)}')">🗑️</button>
          </div>
        </div>`;
    }).join("");
  } catch (e) {
    el.innerHTML = `<p style="color:#EF4444;font-size:13px">Erro ao carregar: ${e}</p>`;
  }
}

async function salvarMcp() {
  const nome = document.getElementById("mcp-nome").value.trim();
  const command = document.getElementById("mcp-command").value.trim();
  const args = document.getElementById("mcp-args").value.split("\n").map(s => s.trim()).filter(Boolean);
  const envTexto = document.getElementById("mcp-env").value.split("\n").map(s => s.trim()).filter(Boolean);
  const env = {};
  envTexto.forEach(linha => {
    const i = linha.indexOf("=");
    if (i > 0) env[linha.slice(0, i).trim()] = linha.slice(i + 1).trim();
  });

  if (!nome || !command) { alert("Preencha nome e comando."); return; }

  try {
    const r = await fetch("/api/mcp", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({nome, command, args, env, ativo: true})
    });
    const d = await r.json();
    if (!d.ok) { alert(d.erro || "Falha ao salvar."); return; }
    document.getElementById("form-mcp").style.display = "none";
    ["mcp-nome","mcp-command","mcp-args","mcp-env"].forEach(id => document.getElementById(id).value = "");
    carregarMcp();
  } catch (e) {
    alert("Erro de conexão: " + e);
  }
}

async function removerMcp(nome) {
  if (!confirm(`Remover o servidor MCP "${nome}"?`)) return;
  await fetch("/api/mcp/" + encodeURIComponent(nome), {method: "DELETE"});
  carregarMcp();
}

// ─────────────────────────────────────────
// CONFIG — Plugins
// ─────────────────────────────────────────

async function carregarPlugins() {
  const el = document.getElementById("lista-plugins");
  try {
    const dados = await (await fetch("/api/plugins")).json();
    const nomes = Object.keys(dados);
    if (!nomes.length) {
      el.innerHTML = `<p style="color:var(--text-tertiary);font-size:13px">Nenhum plugin encontrado.</p>`;
      return;
    }
    el.innerHTML = nomes.map(nome => `
      <div class="config-list-item">
        <div class="info"><div class="titulo">${escapeHtml(nome)}</div></div>
        <label class="switch-mini">
          <input type="checkbox" ${dados[nome] ? "checked" : ""} onchange="togglePlugin('${escapeHtml(nome)}', this.checked)">
          <span class="slider"></span>
        </label>
      </div>`).join("");
  } catch (e) {
    el.innerHTML = `<p style="color:#EF4444;font-size:13px">Erro ao carregar: ${e}</p>`;
  }
}

async function togglePlugin(nome, ligado) {
  await fetch("/api/plugins", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({nome, ligado})
  });
}

// ─────────────────────────────────────────
// CONFIG — Pastas de trabalho
// ─────────────────────────────────────────

async function carregarPastas() {
  const el = document.getElementById("lista-pastas");
  try {
    const lista = await (await fetch("/api/pastas")).json();
    if (!lista.length) {
      el.innerHTML = `<p style="color:var(--text-tertiary);font-size:13px">Nenhuma pasta extra liberada.</p>`;
      return;
    }
    el.innerHTML = lista.map(caminho => `
      <div class="config-list-item">
        <div class="info"><div class="titulo">${escapeHtml(caminho)}</div></div>
        <div class="acoes">
          <button class="btn-mini danger" onclick="removerPasta('${escapeHtml(caminho).replace(/\\\\/g, "\\\\\\\\")}')">🗑️</button>
        </div>
      </div>`).join("");
  } catch (e) {
    el.innerHTML = `<p style="color:#EF4444;font-size:13px">Erro ao carregar: ${e}</p>`;
  }
}

async function adicionarPasta() {
  const caminho = document.getElementById("nova-pasta").value.trim();
  if (!caminho) return;
  try {
    const r = await fetch("/api/pastas", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({caminho})
    });
    const d = await r.json();
    if (!d.ok) { alert(d.erro || "Falha ao adicionar pasta."); return; }
    document.getElementById("nova-pasta").value = "";
    carregarPastas();
  } catch (e) {
    alert("Erro de conexão: " + e);
  }
}

async function removerPasta(caminho) {
  await fetch("/api/pastas", {
    method: "DELETE",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({caminho})
  });
  carregarPastas();
}

// ─────────────────────────────────────────
// CONFIG — Vídeo (ffmpeg) liga/desliga
// ─────────────────────────────────────────

async function toggleVideo(ligado) {
  try {
    const r = await fetch("/api/video-toggle", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ativo: ligado})
    });
    const d = await r.json();
    videoAtivoAtual = !!d.video_ativo;
    carregarStatus();
  } catch (e) {
    alert("Erro ao alternar vídeo: " + e);
  }
}

function fecharConfig() {
  document.getElementById("modal-config").classList.remove("open");
}

let _urlsOriginaisConfig = {ollama: "", sd: ""};

async function carregarDadosConfig() {
  try {
    // Carrega status do sistema
    const s = await (await fetch("/status")).json();
    document.getElementById("config-chroma-status").textContent = s.memoria || "-";

    // Carrega quantidade TOTAL de sessões (sem limite)
    const h = await (await fetch("/historico/listar?limite=9999")).json();
    document.getElementById("config-historico-count").textContent = (h.sessoes || []).length + " sessões";

    // Carrega modelos disponíveis
    const m = await (await fetch("/modelos")).json();
    const select = document.getElementById("config-modelo");
    select.innerHTML = m.modelos.map(x =>
      `<option value="${x.nome}" ${x.nome === s.modelo ? "selected" : ""}>` +
      `${x.nome} — ${x.descricao}` +
      `</option>`
    ).join("");

    // Carrega as URLs REAIS configuradas (antes disso eram só placeholders
    // fixos no HTML, nunca refletiam o .env de verdade)
    document.getElementById("config-ollama-url").value = s.ollama_url || "";
    document.getElementById("config-sd-url").value = s.sd_api_url || "";
    _urlsOriginaisConfig = {ollama: s.ollama_url || "", sd: s.sd_api_url || ""};

    // sincroniza o switch de auto-falar com a preferência salva
    const switchAutoFalar = document.getElementById("switch-auto-falar");
    if (switchAutoFalar) switchAutoFalar.checked = autoFalarRespostas;

    // sincroniza o switch de troca automática de modelo com o backend
    const switchAutoModelo = document.getElementById("switch-auto-modelo");
    if (switchAutoModelo) switchAutoModelo.checked = !!s.auto_modelo;

  } catch (e) {
    console.error("Erro ao carregar config:", e);
  }
}

async function toggleAutoModelo(ligado) {
  try {
    await fetch("/api/auto-modelo-toggle", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ativo: ligado})
    });
  } catch (e) {
    console.error("Erro ao trocar modo de modelo automático:", e);
  }
}

async function apagarHistorico() {
  const btn = document.getElementById("btn-apagar-historico");
  if (!btn) return;
  const textoOriginal = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Apagando...";

  try {
    const r = await fetch("/historico/apagar-tudo", {method: "POST"});
    const d = await r.json();
    if (d.ok) {
      btn.textContent = "✓ Apagado";
      carregarHistorico();
      carregarStatus();
    } else {
      btn.textContent = "Erro";
    }
  } catch (e) {
    console.error("Erro ao apagar histórico:", e);
    btn.textContent = "Erro";
  } finally {
    setTimeout(() => {
      btn.textContent = textoOriginal;
      btn.disabled = false;
    }, 2500);
  }
}

async function limparVram(ev) {
  // Botão pode ser o mini "Limpar" do painel Configurações OU o ejetor
  // ⏏ no header (ao lado do seletor de modelo) — os dois chamam a mesma
  // função e cada um recupera seu próprio texto original antes de mexer.
  const btns = [document.getElementById("btn-limpar-vram"), document.getElementById("btn-ejetar-modelo")]
    .filter(Boolean);
  const originais = btns.map(b => b.innerHTML);
  btns.forEach(b => { b.disabled = true; });
  const btnHeader = document.getElementById("btn-ejetar-modelo");
  if (btnHeader) btnHeader.innerHTML = "⏳";
  const btnConfig = document.getElementById("btn-limpar-vram");
  if (btnConfig) btnConfig.textContent = "Limpando...";

  try {
    const r = await fetch("/api/limpar-vram", {method: "POST"});
    const d = await r.json();
    const nadaALiberar = !d.modelo_descarregado && !d.sd_api_pausado;
    // Se backend local ta offline, mostra mensagem util em vez de "Nada a liberar"
    if (nadaALiberar && !d.backend_local_online) {
      if (btnHeader) btnHeader.innerHTML = "⚠";
      if (btnConfig) btnConfig.textContent = "Backend offline";
      if (btnConfig) btnConfig.title = "Ollama/LM Studio nao esta rodando.\nInicie o Ollama (ollama serve) no terminal.";
      if (btnHeader) btnHeader.title = btnConfig.title;
    } else if (nadaALiberar) {
      if (btnHeader) btnHeader.innerHTML = "—";
      if (btnConfig) btnConfig.textContent = "Nada a liberar";
    } else {
      if (btnHeader) btnHeader.innerHTML = "✓";
      if (btnConfig) btnConfig.textContent = "✓ Liberado";
    }
    carregarStatus();
  } catch (e) {
    console.error("Erro ao limpar VRAM:", e);
    if (btnHeader) btnHeader.innerHTML = "!";
    if (btnConfig) btnConfig.textContent = "Erro";
  } finally {
    setTimeout(() => {
      btns.forEach((b, i) => { b.innerHTML = originais[i]; b.disabled = false; });
    }, 2500);
  }
}

async function trocarModeloConfig() {
  const nome = document.getElementById("config-modelo").value;
  if (!nome) return;
  
  try {
    await fetch("/modelo", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({nome})
    });
  } catch (e) {
    console.error("Erro ao trocar modelo:", e);
  }
}

async function salvarConfig() {
  const btn = document.getElementById("btn-salvar-sistema");
  const textoOriginal = btn.textContent;

  try {
    // Coleta dados dos inputs
    const novaOllama = document.getElementById("config-ollama-url").value;
    const novaSd = document.getElementById("config-sd-url").value;
    const config = {
      ollama_url: novaOllama,
      sd_api_url: novaSd,
      modelo: document.getElementById("config-modelo").value
    };

    // só as URLs exigem reinício (são lidas 1x na subida do Jarvis) — o
    // modelo já troca ao vivo assim que você escolhe no dropdown
    const precisaReiniciar = (
      novaOllama !== _urlsOriginaisConfig.ollama ||
      novaSd !== _urlsOriginaisConfig.sd
    );

    // Envia para backend
    const r = await fetch("/config/salvar", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(config)
    });

    if (r.ok) {
      // Feedback visual de sucesso
      btn.textContent = "✅ Salvo!";
      btn.style.background = "linear-gradient(135deg, #10B981 0%, #059669 100%)";
      _urlsOriginaisConfig = {ollama: novaOllama, sd: novaSd};

      if (precisaReiniciar) {
        setTimeout(() => {
          btn.textContent = "🔄 Reiniciar?";
          btn.style.background = "linear-gradient(135deg, #F59E0B 0%, #D97706 100%)";
        }, 2000);

        setTimeout(() => {
          fecharConfig();
          alert("URL do Ollama/SD-API alterada — reinicie o Jarvis manualmente pra aplicar.");
        }, 4000);
      } else {
        setTimeout(() => {
          btn.textContent = textoOriginal;
          btn.style.background = "";
        }, 2000);
      }
    }
  } catch (e) {
    btn.textContent = "❌ Erro!";
    console.error("Erro ao salvar:", e);
    setTimeout(() => {
      btn.textContent = textoOriginal;
      btn.style.background = "";
    }, 2000);
  }
}

// ─────────────────────────────────────────
// MESSAGES
// ─────────────────────────────────────────

function _mediaGridHtml(anexos) {
  if (!anexos || !anexos.length) return "";
  const itens = anexos.map(a => {
    if (a.tipo === "documento") {
      return `<div class="chip-anexo" style="max-width:220px">
        <span>${_iconeAnexo(a)}</span>
        <span class="nome">${escapeHtml(a.nome)}</span>
      </div>`;
    }
    const src = "/imagem/" + a.caminho.split("/").map(encodeURIComponent).join("/");
    if (a.tipo === "video") {
      return `<div>
        <video class="msg-media" src="${src}" onclick="abrirLightbox('${src}', 'video')" muted></video>
        <div class="msg-media-legenda">🎬 ${escapeHtml(a.nome)}</div>
      </div>`;
    }
    return `<div>
      <img class="msg-media" src="${src}" alt="${escapeHtml(a.nome)}" onclick="abrirLightbox('${src}', 'imagem')">
      <div class="msg-media-legenda">${escapeHtml(a.nome)}</div>
    </div>`;
  }).join("");
  return `<div class="msg-media-grid">${itens}</div>`;
}

// Alguns resultados de ferramenta (gerar_imagem, tirar_print) trazem
// "IMAGEM_GERADA: nome.png", e gravar_tela/parar_gravacao_tela trazem
// "VIDEO_GERADO: nome.mp4" — detecta isso pra mostrar a miniatura/vídeo
// direto no balão, sem o operador precisar abrir o chip de ferramenta
// pra descobrir o que foi gerado.
function _imagensGeradasDosChips(toolChips) {
  if (!toolChips) return [];
  const achadas = [];
  toolChips.forEach(chip => {
    const resultado = chip.resultado || "";
    const mImg = /IMAGEM_GERADA:\\s*([^\\s(]+)/.exec(resultado);
    if (mImg) achadas.push({nome: mImg[1], caminho: mImg[1], tipo: "imagem"});
    const mVid = /VIDEO_GERADO:\\s*([^\\s(]+)/.exec(resultado);
    if (mVid) achadas.push({nome: mVid[1], caminho: mVid[1], tipo: "video"});
  });
  return achadas;
}

// Automatiza o que antes exigia clicar em 📎 de novo: sempre que um print
// ou uma gravação é gerado — seja pelo botão guiado, seja pelo próprio
// modelo chamando tirar_print/gravar_tela no meio da conversa — o arquivo
// já entra como anexo pendente (mesma fila do 📎), pronto pra ir junto na
// PRÓXIMA mensagem com visão de verdade. Sem isso o modelo só sabia que o
// arquivo existia (texto "IMAGEM_GERADA: nome"), nunca via os pixels.
function _autoAnexarCapturas(toolChips) {
  const achadas = _imagensGeradasDosChips(toolChips);
  if (!achadas.length) return;
  let mudou = false;
  achadas.forEach(a => {
    if (anexosAtuais.some(x => x.caminho === a.caminho)) return; // já pendente
    anexosAtuais.push({nome: a.nome, caminho: a.caminho, tipo: a.tipo, enviando: false, auto: true});
    mudou = true;
  });
  if (mudou) renderAnexosPreview();
}

function _metricasHtml(m) {
  if (!m) return "";
  const relogioIcon = `<img class="metric-icon" src="/imagem/_icones/relogio.png" alt="tempo">`;
  const partes = [];
  if (m.tempo_resposta_s) partes.push(`${relogioIcon}${m.tempo_resposta_s}s`);
  if (m.tempo_total_s && m.tempo_total_s !== m.tempo_resposta_s) partes.push(`(total ${m.tempo_total_s}s)`);
  if (m.tokens_entrada) partes.push(`📥 ${m.tokens_entrada} in`);
  if (m.tokens_saida) partes.push(`📤 ${m.tokens_saida} out`);
  if (m.tokens_por_segundo) partes.push(`⚡ ${m.tokens_por_segundo} tok/s`);
  if (m.contexto_tokens) partes.push(`🧠 ${m.contexto_tokens} ctx`);
  if (!partes.length) return "";
  return `<div class="msg-metricas">${partes.map(p => `<span>${p}</span>`).join("")}</div>`;
}

function addMessage(role, content, toolChips = null, anexos = null, metricas = null, msgId = null, sessaoId = null) {
  welcome.style.display = "none";
  
  // Wrapper é o elemento que tem a barra de rolagem
  const wrapper = document.getElementById("chat-zoom-wrapper") || chat;

  const msgDiv = document.createElement("div");
  msgDiv.className = "message";
  // ids do histórico persistente — usados só pelo botão 🗑️ Apagar pra
  // saber qual mensagem remover no disco (ver apagarMensagem). Ficam
  // vazios até a resposta do backend chegar (ver enviar()); nesse meio
  // tempo o botão some silenciosamente na hora do clique (nada a apagar
  // no servidor ainda).
  if (msgId !== null && msgId !== undefined) msgDiv.dataset.msgId = msgId;
  if (sessaoId) msgDiv.dataset.sessaoId = sessaoId;

  if (role === "user") {
    msgDiv.classList.add("message-user");
    msgDiv.innerHTML = `
      <div class="message-content">
        ${content ? `<div class="msg-texto">${escapeHtml(content)}</div>` : ""}
        ${_mediaGridHtml(anexos)}
      </div>
      <div class="msg-actions">
        <span class="msg-act" title="Copiar" onclick="copiarMensagem(this)">📋</span>
        <span class="msg-act" title="Editar e reenviar" onclick="editarMensagemUsuario(this)">✏️</span>
        <span class="msg-act" title="Apagar" onclick="apagarMensagem(this)">🗑️</span>
      </div>`;
  } else {
    msgDiv.classList.add("message-assistant");

    let html = `<div>${formatMessage(content)}</div>`;

    const geradas = _imagensGeradasDosChips(toolChips);
    if (geradas.length) html += _mediaGridHtml(geradas);

    if (toolChips && toolChips.length > 0) {
      html += `<div class="tool-chips">`;
      toolChips.forEach((chip, i) => {
        html += `
          <div class="tool-chip" onclick="toggleChip(this)">
            🔧 ${chip.ferramenta}
          </div>
          <div class="tool-chip-detail">
            <strong>Args:</strong> ${escapeHtml(JSON.stringify(chip.args))}<br><br>
            <strong>Resultado:</strong> ${escapeHtml(chip.resultado)}
          </div>
        `;
      });
      html += `</div>`;
    }

    html += _metricasHtml(metricas);

    html += `
      <div class="msg-actions">
        <span class="msg-act" title="Copiar" onclick="copiarMensagem(this)">📋</span>
        <span class="msg-act" title="Editar" onclick="editarMensagemAssistente(this)">✏️</span>
        <span class="msg-act" title="Ouvir" onclick="falarMensagem(this)">🔊</span>
        <select class="msg-export-select" title="Exportar (baixa direto pra pasta Downloads)" onchange="exportarMensagem(this)">
          <option value="">📥 Exportar</option>
          <option value="md">Markdown (.md)</option>
          <option value="docx">Word (.docx)</option>
          <option value="pdf">PDF (.pdf)</option>
          <option value="html">HTML (.html)</option>
          <option value="txt">Texto puro (.txt)</option>
          <option value="json">JSON estruturado (.json)</option>
          <option value="apkg">Anki (.apkg)</option>
        </select>
        <span class="msg-act" title="Salvar como... (abre o Salvar nativo do Windows, você escolhe pasta e formato)" onclick="salvarMensagemNativo(this)">💾 Salvar</span>
        <span class="msg-act" title="Apagar" onclick="apagarMensagem(this)">🗑️</span>
      </div>`;

    msgDiv.innerHTML = html;
    // guarda o markdown cru (não o HTML já renderizado) pra exportação fiel
    msgDiv.dataset.rawMd = content || "";
  }
  
    wrapper.appendChild(msgDiv);
    _scrollChatParaFim(role === "assistant");
    atualizarBotoesAcao();
    return msgDiv;
  }
  
  function addTypingIndicator() {
  welcome.style.display = "none";
  
  const div = document.createElement("div");
  div.className = "message";
  div.id = "typing";
  div.innerHTML = `
    <div class="message-assistant">
      <div class="typing-indicator">
        <span></span><span></span><span></span>
      </div>
    </div>
  `;
  wrapper.appendChild(div);
  _scrollChatParaFim(true);
}

function removeTypingIndicator() {
  const typing = document.getElementById("typing");
  if (typing) typing.remove();
}

// ─────────────────────────────────────────
// 💬 CHAT LIVRE — modelo responde cru (sem ferramentas) até confirmar
// ─────────────────────────────────────────

function _aplicarEstadoPlanejamento(ativo) {
  const btn = document.getElementById("btn-planejamento");
  if (!btn) return;
  btn.classList.toggle("planejamento-ativo", !!ativo);
  btn.title = ativo
    ? "Chat Livre LIGADO: o modelo responde cru, sem ferramentas, até você confirmar (faça, comece, concordo, prossiga...)"
    : "Chat Livre: o modelo responde cru (sem ferramentas) até você confirmar, aí o Jarvis executa";
  syncDrawerStates();
}

async function togglePlanejamento() {
  try {
    const r = await fetch("/planejamento/toggle", {method: "POST"});
    const data = await r.json();
    _aplicarEstadoPlanejamento(data.ativo);
    syncDrawerStates();
  } catch (e) {
    console.error("Erro ao alternar modo planejamento:", e);
  }
}

// ─────────────────────────────────────────
// VOZ — microfone (fala → texto) via SpeechRecognition nativo do navegador
// ─────────────────────────────────────────

let _reconhecimentoVoz = null;
let _ouvindoMicrofone = false;

function toggleMicrofone() {
  const SpeechRecognitionAPI = window.SpeechRecognition || window.webkitSpeechRecognition;
  const btn = document.getElementById("btn-microfone");

  if (!SpeechRecognitionAPI) {
    alert("Seu navegador não suporta reconhecimento de voz. Use o Edge ou o Chrome.");
    return;
  }

  if (_ouvindoMicrofone) {
    _reconhecimentoVoz.stop();
    return;
  }

  _reconhecimentoVoz = new SpeechRecognitionAPI();
  _reconhecimentoVoz.lang = "pt-BR";
  _reconhecimentoVoz.interimResults = false;
  _reconhecimentoVoz.maxAlternatives = 1;

  _reconhecimentoVoz.onstart = () => {
    _ouvindoMicrofone = true;
    btn.classList.add("gravando");
    btn.title = "Gravando... clique pra parar";
  };

  _reconhecimentoVoz.onresult = (event) => {
    const texto = event.results[0][0].transcript;
    input.value = (input.value.trim() ? input.value.trim() + " " : "") + texto;
    autoResize(input);
    input.focus();
  };

  _reconhecimentoVoz.onerror = (event) => {
    console.error("Erro no reconhecimento de voz:", event.error);
    if (event.error === "not-allowed" || event.error === "service-not-allowed") {
      alert("Permissão de microfone negada. Libere o microfone pro Jarvis nas configurações do navegador (cadeado ao lado do endereço).");
    } else if (event.error !== "no-speech" && event.error !== "aborted") {
      alert("Erro no reconhecimento de voz: " + event.error);
    }
  };

  _reconhecimentoVoz.onend = () => {
    _ouvindoMicrofone = false;
    btn.classList.remove("gravando");
    btn.title = "Falar (microfone)";
  };

  try {
    _reconhecimentoVoz.start();
  } catch (e) {
    console.error("Não consegui iniciar o microfone:", e);
  }
}

// ─────────────────────────────────────────
// VOZ — leitura em voz alta (texto → fala) via speechSynthesis nativo
// ─────────────────────────────────────────

let _vozPtBr = null;
let autoFalarRespostas = localStorage.getItem("jarvis_auto_falar") === "1";

if ("speechSynthesis" in window) {
  speechSynthesis.onvoiceschanged = () => { _vozPtBr = null; };
}

function _escolherVozPt() {
  if (_vozPtBr) return _vozPtBr;
  if (!("speechSynthesis" in window)) return null;
  const vozes = speechSynthesis.getVoices();
  _vozPtBr = vozes.find(v => v.lang && v.lang.toLowerCase().startsWith("pt")) || null;
  return _vozPtBr;
}

// Voz escolhida manualmente pelo usuário no dropdown da aba Sistema
// (salvo no localStorage pra persistir entre recargas).
let _vozEscolhida = localStorage.getItem("jarvis_voz") || "";

function _preencherDropdownVoz() {
  const sel = document.getElementById("config-voz");
  if (!sel || !("speechSynthesis" in window)) return;
  const vozes = speechSynthesis.getVoices();
  sel.innerHTML = '<option value="">(automática — pt-BR)</option>';
  vozes.forEach((v, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = `${v.name} — ${v.lang}`;
    if (String(i) === _vozEscolhida) opt.selected = true;
    sel.appendChild(opt);
  });
}

function salvarVoz() {
  const sel = document.getElementById("config-voz");
  if (!sel) return;
  _vozEscolhida = sel.value;
  localStorage.setItem("jarvis_voz", _vozEscolhida);
  // limpa o cache da voz automática pra forçar reavaliação na próxima fala
  _vozPtBr = null;
}

let _falandoBtn = null; // <span> do "Ouvir" que virou "Parar" enquanto fala

function _marcarFalando(btnEl) {
  if (_falandoBtn) {
    _falandoBtn.classList.remove("falando");
    _falandoBtn.textContent = "🔊";
    _falandoBtn.title = "Ouvir";
  }
  _falandoBtn = btnEl || null;
  if (_falandoBtn) {
    _falandoBtn.classList.add("falando");
    _falandoBtn.textContent = "⏹";
    _falandoBtn.title = "Parar leitura";
  }
}

function falarTexto(texto, btnEl) {
  if (!texto || !("speechSynthesis" in window)) return;
  speechSynthesis.cancel(); // não deixa empilhar falas se clicar em vários
  const utter = new SpeechSynthesisUtterance(texto);
  utter.lang = "pt-BR";
  // voz manual tem preferência sobre a automática (pt-BR)
  let voz = null;
  if (_vozEscolhida !== "" && _vozEscolhida !== "0") {
    const vozes = speechSynthesis.getVoices();
    voz = vozes[Number(_vozEscolhida)] || null;
  }
  if (!voz) voz = _escolherVozPt();
  if (voz) utter.voice = voz;
  utter.onend = () => _marcarFalando(null);
  utter.onerror = () => _marcarFalando(null);
  speechSynthesis.speak(utter);
  _marcarFalando(btnEl);
}

function pararFala() {
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  _marcarFalando(null);
}

function falarMensagem(el) {
  // clicou de novo no botão enquanto ele já está lendo → vira botão de parar
  if (el.classList.contains("falando")) {
    pararFala();
    return;
  }
  const msgDiv = el.closest(".message-assistant");
  const conteudo = msgDiv.querySelector("div"); // primeiro div = texto formatado
  const texto = conteudo ? conteudo.innerText : msgDiv.innerText;
  falarTexto(texto, el);
}

function toggleAutoFalar(ligado) {
  autoFalarRespostas = ligado;
  localStorage.setItem("jarvis_auto_falar", ligado ? "1" : "0");
  if (!ligado) pararFala();
}

function _nomeArquivoDoHeader(resp, fallback) {
  const cd = resp.headers.get("Content-Disposition") || "";
  const m = cd.match(/filename="?([^"]+)"?/i);
  return m ? m[1] : fallback;
}

async function exportarMensagem(select) {
  const formato = select.value;
  if (!formato) return;
  select.value = "";

  const msgDiv = select.closest(".message-assistant");
  const texto = (msgDiv && msgDiv.dataset.rawMd) || "";
  if (!texto.trim()) {
    alert("Nada para exportar.");
    return;
  }

  try {
    const resp = await fetch("/api/exportar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texto, formato })
    });

    if (!resp.ok) {
      const erro = await resp.json().catch(() => ({}));
      alert("Erro ao exportar: " + (erro.erro || resp.status));
      return;
    }

    const blob = await resp.blob();
    const nomeArquivo = _nomeArquivoDoHeader(resp, `jarvis_resposta.${formato}`);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = nomeArquivo;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert("Erro ao exportar: " + e);
  }
}

// ─────────────────────────────────────────
// "Salvar como..." NATIVO — abre o diálogo real do Windows (Explorer),
// igual qualquer programa de desktop, em vez de cair direto na pasta
// Downloads como o "Exportar" acima. Usa a File System Access API
// (showSaveFilePicker), suportada pelo Edge/Chrome mesmo rodando em modo
// --app. Se o navegador não suportar (ex: Firefox), cai de volta pro
// download comum em .md.
// ─────────────────────────────────────────
async function salvarMensagemNativo(el) {
  const msgDiv = el.closest(".message-assistant");
  const texto = (msgDiv && msgDiv.dataset.rawMd) || "";
  if (!texto.trim()) {
    alert("Nada para salvar.");
    return;
  }

  if (!("showSaveFilePicker" in window)) {
    // navegador sem suporte à File System Access API — usa o fluxo de
    // exportar comum (baixa .md pra pasta Downloads) como alternativa.
    alert("Seu navegador não suporta o Salvar nativo do Windows. Use o menu 'Exportar' ao lado.");
    return;
  }

  let handle;
  try {
    handle = await window.showSaveFilePicker({
      suggestedName: "jarvis_resposta.md",
      types: [
        { description: "Markdown", accept: { "text/markdown": [".md"] } },
        { description: "Word", accept: { "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [".docx"] } },
        { description: "PDF", accept: { "application/pdf": [".pdf"] } },
        { description: "HTML", accept: { "text/html": [".html"] } },
        { description: "Texto puro", accept: { "text/plain": [".txt"] } },
        { description: "JSON", accept: { "application/json": [".json"] } },
        { description: "Anki", accept: { "application/octet-stream": [".apkg"] } },
      ],
    });
  } catch (e) {
    return; // usuário cancelou o diálogo — não é erro
  }

  // descobre o formato pela extensão que o usuário escolheu no próprio
  // diálogo nativo (ex: "relatorio.docx" -> "docx")
  const nomeEscolhido = handle.name || "jarvis_resposta.md";
  const formato = (nomeEscolhido.split(".").pop() || "md").toLowerCase();

  try {
    const resp = await fetch("/api/exportar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texto, formato }),
    });
    if (!resp.ok) {
      const erro = await resp.json().catch(() => ({}));
      alert("Erro ao gerar o arquivo: " + (erro.erro || resp.status));
      return;
    }
    const blob = await resp.blob();
    const writable = await handle.createWritable();
    await writable.write(blob);
    await writable.close();
  } catch (e) {
    alert("Erro ao salvar: " + e);
  }
}

// Apaga um balão específico (pergunta OU resposta), sem tocar no resto da
// conversa. Some da tela na hora; se o balão já tiver id salvo (todo
// balão ganha um assim que a resposta termina — ver enviar()/carregarSessao),
// também apaga no histórico em disco. Balão ainda "ao vivo" (resposta
// chegando) não tem botão de apagar visível, então esse caso não aparece.
async function apagarMensagem(el) {
  const msgDiv = el.closest(".message");
  if (!msgDiv) return;
  if (!confirm("Apagar esta mensagem?")) return;

  const sessaoId = msgDiv.dataset.sessaoId;
  const msgId = msgDiv.dataset.msgId;

  if (sessaoId && msgId !== undefined && msgId !== "") {
    try {
      const r = await fetch(`/historico/${sessaoId}/mensagem/${msgId}`, {method: "DELETE"});
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        alert("Não consegui apagar no histórico: " + (d.erro || r.status));
        return; // não some da tela se não conseguiu apagar no disco
      }
    } catch (e) {
      alert("Erro de conexão ao apagar: " + e);
      return;
    }
  }

  msgDiv.remove();
}

// Print guiado: dispara a captura de tela direto (sem precisar digitar
// mensagem nenhuma no chat). O clique no botão já chama a ferramenta
// tirar_print via /api/tela/print, que abre o seletor de área (arrasta o
// mouse pra escolher a região, Esc cancela) e devolve o print pronto —
// é mostrado no chat reaproveitando o mesmo balão/miniatura dos prints
// tirados pelo próprio modelo.
// Captura instantânea da tela inteira — sem interação do mouse.
// Um clique no cérebro e o Jarvis já enxerga o que está na tela.
async function verTela() {
  const btn = document.getElementById("btn-ver-tela");
  if (btn && btn.classList.contains("piscando")) return;
  if (btn) { btn.classList.add("piscando"); }
  try {
    const r = await fetch("/ver-tela");
    const d = await r.json();
    if (!d.ok) { alert("Erro ao capturar tela: " + (d.erro || "?")); return; }
    anexosAtuais.push({nome: d.caminho, caminho: d.caminho, tipo: "imagem", enviando: false});
    renderAnexosPreview();
    // foca no input para o usuário digitar a pergunta
    document.getElementById("input").focus();
    // se não houver texto no input, sugere
    const inp = document.getElementById("input");
    if (!inp.value.trim()) {
      inp.value = "O que está acontecendo nessa tela?";
      autoResize(inp);
    }
  } catch(e) {
    alert("Erro de conexão ao capturar tela: " + e);
  } finally {
    if (btn) setTimeout(() => btn.classList.remove("piscando"), 1800);
  }
}

async function capturarPrintTela() {
  const btn = document.getElementById("btn-print-tela");
  if (btn.classList.contains("aguardando")) return; // já tem uma captura em andamento
  btn.classList.add("aguardando");
  const iconeOriginal = btn.innerHTML;
  btn.innerHTML = "⏳";
  btn.title = "Aponte a área na tela (arraste o mouse, Esc cancela)...";

  try {
    const resp = await fetch("/api/tela/print", { method: "POST" });
    const dados = await resp.json();

    if (dados.ok) {
      const chip = { ferramenta: "tirar_print", args: {}, resultado: dados.resultado };
      addMessage("assistant", "📸 Print capturado e já anexado — pode perguntar sobre ele agora.", [chip]);
      _autoAnexarCapturas([chip]);
    } else if (dados.cancelado) {
      // usuário apertou Esc — não polui o histórico do chat
    } else {
      alert("Erro ao capturar tela: " + (dados.mensagem || "falha desconhecida"));
    }
  } catch (e) {
    alert("Erro ao capturar tela: " + e);
  } finally {
    btn.classList.remove("aguardando");
    btn.innerHTML = iconeOriginal;
    btn.title = "Captura guiada de tela: clique e aponte a área com o mouse";
  }
}

// Gravação guiada, em duas etapas (o mesmo botão faz as duas):
// 1ª clique: chama /api/tela/gravar/iniciar, que abre o seletor de área
// (arrasta o mouse, Esc cancela) e já começa a gravar em background —
// o botão fica vermelho pulsando (.gravando) pra lembrar que tem uma
// gravação rolando enquanto o usuário continua usando o chat normalmente.
// 2ª clique (com o botão já "gravando"): chama /api/tela/gravar/parar,
// que encerra o ffmpeg e devolve o vídeo pronto, anexado no chat do
// mesmo jeito que o print guiado.
const TITULO_GRAVAR_IDLE = "Gravação guiada de tela: clique, aponte a área com o mouse, e clique de novo pra parar";

// Cronômetro do botão — complementa o cronômetro que já aparece DIRETO
// NA TELA (overlay "⏺ REC mm:ss" desenhado por cima da área gravada,
// ver plugins/captura_tela.py). Esse aqui é o reforço: se o overlay não
// aparecer por algum motivo (ex: Windows mais antigo, sem suporte à API
// que exclui a janela da captura), o usuário ainda enxerga o tempo
// decorrido aqui no botão do chat.
let _gravacaoIntervalo = null;
let _gravacaoInicio = null;

function _iniciarCronometroBotao(btn) {
  _gravacaoInicio = Date.now();
  _gravacaoIntervalo = setInterval(() => {
    const decorridos = Math.floor((Date.now() - _gravacaoInicio) / 1000);
    const mm = String(Math.floor(decorridos / 60)).padStart(2, "0");
    const ss = String(decorridos % 60).padStart(2, "0");
    btn.innerHTML = `⏺️ ${mm}:${ss}`;
  }, 1000);
}

function _pararCronometroBotao() {
  if (_gravacaoIntervalo) {
    clearInterval(_gravacaoIntervalo);
    _gravacaoIntervalo = null;
  }
}

async function alternarGravacaoTela() {
  const btn = document.getElementById("btn-gravar-tela");
  if (btn.classList.contains("aguardando")) return; // já tem uma ação em andamento

  if (btn.classList.contains("gravando")) {
    // parar gravação
    _pararCronometroBotao();
    btn.classList.add("aguardando");
    const iconeOriginal = btn.innerHTML;
    btn.innerHTML = "⏳";
    btn.title = "Finalizando gravação...";

    try {
      const resp = await fetch("/api/tela/gravar/parar", { method: "POST" });
      const dados = await resp.json();

      if (dados.ok) {
        const chip = { ferramenta: "parar_gravacao_tela", args: {}, resultado: dados.resultado };
        addMessage("assistant", "🎥 Gravação finalizada e já anexada — pode perguntar sobre ela agora.", [chip]);
        _autoAnexarCapturas([chip]);
      } else {
        alert("Erro ao finalizar gravação: " + (dados.mensagem || "falha desconhecida"));
      }
    } catch (e) {
      alert("Erro ao finalizar gravação: " + e);
    } finally {
      btn.classList.remove("aguardando", "gravando");
      btn.innerHTML = "🎥";
      btn.title = TITULO_GRAVAR_IDLE;
      syncDrawerStates();
    }
    return;
  }

  // iniciar gravação
  btn.classList.add("aguardando");
  const iconeOriginal = btn.innerHTML;
  btn.innerHTML = "🔴";
  btn.title = "Aponte a área na tela (arraste o mouse, Esc cancela)...";

  try {
    const resp = await fetch("/api/tela/gravar/iniciar", { method: "POST" });
    const dados = await resp.json();

    if (dados.ok) {
      btn.classList.add("gravando");
      _iniciarCronometroBotao(btn);
      btn.title = "Gravando... clique pra parar (contorno + cronômetro também aparecem na área selecionada)";
      syncDrawerStates();
    } else if (dados.cancelado) {
      btn.innerHTML = iconeOriginal;
      btn.title = TITULO_GRAVAR_IDLE;
    } else {
      alert("Erro ao iniciar gravação: " + (dados.mensagem || "falha desconhecida"));
      btn.innerHTML = iconeOriginal;
      btn.title = TITULO_GRAVAR_IDLE;
    }
  } catch (e) {
    alert("Erro ao iniciar gravação: " + e);
    btn.innerHTML = iconeOriginal;
    btn.title = TITULO_GRAVAR_IDLE;
  } finally {
    btn.classList.remove("aguardando");
  }
  syncDrawerStates();
}

function abrirLightbox(src, tipo) {
  const overlay = document.getElementById("modal-lightbox");
  const img = document.getElementById("lightbox-img");
  const video = document.getElementById("lightbox-video");
  if (tipo === "video") {
    video.src = src;
    video.style.display = "block";
    img.style.display = "none";
    img.src = "";
  } else {
    img.src = src;
    img.style.display = "block";
    video.style.display = "none";
    video.pause();
    video.src = "";
  }
  overlay.classList.add("active");
}

function fecharLightbox(event) {
  // só fecha clicando no fundo ou no X — clique na própria mídia não fecha
  if (event && event.target.id !== "modal-lightbox" && !event.target.classList.contains("lightbox-close")) {
    return;
  }
  const overlay = document.getElementById("modal-lightbox");
  const video = document.getElementById("lightbox-video");
  video.pause();
  overlay.classList.remove("active");
}

function toggleChip(el) {
  el.classList.toggle("active");
  const detail = el.nextElementSibling;
  if (detail) {
    detail.style.display = detail.style.display === "block" ? "none" : "block";
  }
}

// ─────────────────────────────────────────
// AÇÕES DISCRETAS NOS BALÕES: copiar / editar
// ─────────────────────────────────────────

function _feedbackMsgAct(el, emoji) {
  const original = el.textContent;
  el.textContent = emoji;
  setTimeout(() => { el.textContent = original; }, 1200);
}

function copiarMensagem(el) {
  const msgDiv = el.closest(".message");
  const conteudo = msgDiv.querySelector(".message-content") || msgDiv.querySelector(":scope > div");
  const texto = conteudo ? conteudo.innerText : msgDiv.innerText;
  navigator.clipboard.writeText(texto.trim()).then(() => {
    _feedbackMsgAct(el, "✅");
  }).catch(err => console.error("Erro ao copiar:", err));
}

function editarMensagemUsuario(el) {
  const msgDiv = el.closest(".message-user");
  const texto = msgDiv.querySelector(".msg-texto");
  if (!texto) return; // mensagem só com anexo, sem texto pra reeditar
  input.value = texto.innerText.trim();
  autoResize(input);
  input.focus();
}

function editarMensagemAssistente(el) {
  const msgDiv = el.closest(".message-assistant");
  const conteudo = msgDiv.querySelector("div"); // primeiro <div> = texto formatado
  if (!conteudo) return;

  const editando = conteudo.getAttribute("contenteditable") === "true";
  if (editando) {
    conteudo.removeAttribute("contenteditable");
    el.textContent = "✏️";
  } else {
    conteudo.setAttribute("contenteditable", "true");
    conteudo.focus();
    el.textContent = "💾";
  }
}

// ─────────────────────────────────────────
// API CALLS
// ─────────────────────────────────────────

let isProcessing = false;

async function enviar() {
  const msg = input.value.trim();
  const prontos = anexosAtuais.filter(a => !a.enviando && a.caminho);
  if ((!msg && !prontos.length) || isProcessing) return;

  input.value = "";
  input.style.height = "auto";
  isProcessing = true;
  sendBtn.disabled = true;
  iniciarCronometroTarefa();

  const userMsgDiv = addMessage("user", msg, null, prontos);
  addTypingIndicator();

  const imagensParaEnviar = prontos.filter(a => a.tipo !== "documento").map(a => a.caminho);
  const documentosParaEnviar = prontos.filter(a => a.tipo === "documento").map(a => a.caminho);
  anexosAtuais = [];
  renderAnexosPreview();

  let liveDiv = null;
  let liveTexto = "";

  try {
    const r = await fetch("/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({msg, imagens: imagensParaEnviar, documentos: documentosParaEnviar})
    });

    if (!r.ok || !r.body) throw new Error(`HTTP ${r.status}`);

    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});

      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const linha = buffer.slice(0, nl);
        buffer = buffer.slice(nl + 1);
        if (!linha.trim()) continue;

        let item;
        try { item = JSON.parse(linha); } catch (e) { continue; }

        if (item.tipo === "heartbeat") {
          // só mantém a conexão viva, nada pra desenhar
          continue;

        } else if (item.tipo === "token") {
          // primeiro token: troca o indicador de "digitando" por um balão
          // que vai crescendo em tempo real conforme o modelo responde
          if (!liveDiv) {
            removeTypingIndicator();
            liveDiv = document.createElement("div");
            liveDiv.className = "message message-assistant";
            liveDiv.innerHTML = `<div class="msg-live-text"></div>`;
            wrapper.appendChild(liveDiv);
          }
          liveTexto += item.texto;
          liveDiv.querySelector(".msg-live-text").textContent = liveTexto;
          _scrollChatParaFim(true);

        } else if (item.tipo === "fim") {
          // troca o balão "ao vivo" (texto puro) pelo balão final
          // (markdown renderizado, chips de ferramenta, métricas)
          if (liveDiv) { liveDiv.remove(); liveDiv = null; }
          removeTypingIndicator();
          addMessage("assistant", item.resposta, item.eventos, null, item.metricas, item.assistant_msg_id, item.sessao_id);
          // o balão do usuário foi criado antes de saber o id (só o backend
          // atribui) — marca ele agora que a resposta chegou, senão o
          // botão 🗑️ dele fica sem saber o que apagar no disco
          if (userMsgDiv) {
            if (item.user_msg_id !== null && item.user_msg_id !== undefined) userMsgDiv.dataset.msgId = item.user_msg_id;
            if (item.sessao_id) userMsgDiv.dataset.sessaoId = item.sessao_id;
          }
          _autoAnexarCapturas(item.eventos);
          if (autoFalarRespostas) falarTexto(item.resposta);

          // sincroniza o botão do modo planejamento: se a mensagem enviada
          // era a confirmação, o backend já desligou o modo pra essa sessão
          _aplicarEstadoPlanejamento(item.modo_planejamento_ativo);

          // o modelo automático pode ter trocado durante o processamento —
          // sincroniza o seletor do header na hora, sem esperar o refresh de 15s
          carregarStatus();

          // Atualiza ID da sessão se veio do backend
          if (item.sessao_id) {
            currentSessionId = item.sessao_id;
            carregarHistorico(); // Atualiza sidebar
          }
        }
        // item.tipo === "evento": as tool calls já aparecem no balão final
        // (via item.eventos do "fim"); não precisam de tratamento aqui.
      }
    }

  } catch (e) {
    if (liveDiv) liveDiv.remove();
    removeTypingIndicator();
    addMessage("assistant", `Erro de conexão: ${e}`);
  }

  isProcessing = false;
  sendBtn.disabled = false;
  input.focus();
  atualizarBotoesAcao();
}
  
  // ─────────────────────────────────────────
  // BOTÕES DE AÇÃO: PARAR e COPIAR
  // ─────────────────────────────────────────
  
  function atualizarBotoesAcao() {
    const btnParar = document.getElementById("btn-parar");
    if (isProcessing) {
      btnParar.style.display = "flex";
    } else {
      btnParar.style.display = "none";
      pararCronometroTarefa();
    }
  }

  // ─────────────────────────────────────────
  // CRONÔMETRO DA TAREFA
  // ─────────────────────────────────────────

  let cronometroInterval = null;
  let cronometroInicio = null;

  function iniciarCronometroTarefa() {
    const el = document.getElementById("cronometro-tarefa");
    const txt = document.getElementById("cronometro-texto");
    if (!el || !txt) return;

    cronometroInicio = Date.now();
    txt.textContent = "0:00";
    el.classList.add("ativo");

    if (cronometroInterval) clearInterval(cronometroInterval);
    cronometroInterval = setInterval(() => {
      const decorridoMs = Date.now() - cronometroInicio;
      const totalSeg = Math.floor(decorridoMs / 1000);
      const min = Math.floor(totalSeg / 60);
      const seg = totalSeg % 60;
      txt.textContent = `${min}:${String(seg).padStart(2, "0")}`;
    }, 1000);
  }

  function pararCronometroTarefa() {
    if (cronometroInterval) {
      clearInterval(cronometroInterval);
      cronometroInterval = null;
    }
    const el = document.getElementById("cronometro-tarefa");
    if (el) el.classList.remove("ativo");
  }

  async function pararProcessamento() {
    try {
      // Aguarda confirmação do backend
      const r = await fetch("/parar", {method: "POST"});
      const data = await r.json();
      
      if (data.ok) {
        removeTypingIndicator();
        pararFala();
        addMessage("assistant", "⏹ Processamento interrompido pelo usuário.");
        isProcessing = false;
        sendBtn.disabled = false;
        atualizarBotoesAcao();
      }
    } catch (e) {
      console.error("Erro ao parar:", e);
      addMessage("assistant", "⚠️ Erro ao interromper: " + e);
    }
  }
  
  async function novaSessao() {
    try {
      const r = await fetch("/historico/nova", {method: "POST"});
      const data = await r.json();
      currentSessionId = data.id;
      
      // Limpa SÓ o conteúdo rolável (wrapper) — limpar chat.innerHTML
      // destruía o wrapper, o input e a gaveta junto, deixando tudo preto
      wrapper.innerHTML = "";
      renderWelcome();
      
      input.value = "";
      carregarHistorico();
    } catch (e) {
      console.error("Erro ao criar sessão:", e);
    }
  }
  
  function renderWelcome() {
    const welcomeDiv = document.createElement("div");
    welcomeDiv.className = "welcome-screen";
    welcomeDiv.id = "welcome";
    welcomeDiv.innerHTML = `
      <div class="welcome-logo"><img src="/imagem/_icones/jarvis_mascote.png" alt="Jarvis" style="width:100%;height:100%;object-fit:contain"></div>
      <h1 class="welcome-title">Bem-vindo ao Jarvis AI</h1>
      <p class="welcome-subtitle">
        Seu assistente pessoal de inteligência artificial com capacidades agênticas, 
        memória persistente e integração com ferramentas.
      </p>
      
      <div class="quick-actions">
        <div class="quick-action" onclick="quickAction('criar_arquivo')">
          <div class="quick-action-icon"><img src="/imagem/_icones/criar_arquivo.png" alt="Criar Arquivo" style="width:100%;height:100%;object-fit:contain"></div>
          <div class="quick-action-title">Criar Arquivo</div>
          <div class="quick-action-desc">Gere arquivos de texto, código ou documentos</div>
        </div>
        <div class="quick-action" onclick="quickAction('buscar_web')">
          <div class="quick-action-icon"><img src="/imagem/_icones/buscar.png" alt="Buscar na Web" style="width:100%;height:100%;object-fit:contain"></div>
          <div class="quick-action-title">Buscar na Web</div>
          <div class="quick-action-desc">Pesquise informações atualizadas na internet</div>
        </div>
        <div class="quick-action" onclick="analisarPasta()">
          <div class="quick-action-icon"><img src="/imagem/_icones/pasta_projeto.png" alt="Pasta" style="width:100%;height:100%;object-fit:contain"></div>
          <div class="quick-action-title">Analisar Projeto</div>
          <div class="quick-action-desc">Entenda a estrutura de uma pasta</div>
        </div>
        <div class="quick-action" onclick="quickAction('gerar_imagem')">
          <div class="quick-action-icon"><img src="/imagem/_icones/camera.png" alt="Gerar Imagem" style="width:100%;height:100%;object-fit:contain"></div>
          <div class="quick-action-title">Gerar Imagem</div>
          <div class="quick-action-desc">Crie imagens com IA a partir de descrições</div>
        </div>
      </div>
    `;
    wrapper.appendChild(welcomeDiv);
    // reponta a referência global — sem isso, addMessage esconderia o nó
    // antigo (removido do DOM) e a welcome nova ficaria visível pra sempre
    welcome = welcomeDiv;
  }

async function carregarHistorico() {
  try {
    const r = await fetch("/historico/listar?limite=50");
    const data = await r.json();
    
    const list = document.getElementById("history-list");
    list.innerHTML = '<div class="section-title">Recentes</div>';
    
    if (!data.sessoes || data.sessoes.length === 0) {
      list.innerHTML += '<div style="padding:16px;color:var(--text-tertiary);font-size:13px">Nenhuma sessão salva</div>';
      return;
    }
    
    data.sessoes.forEach(sessao => {
      const data = new Date(sessao.atualizada_em || sessao.criada_em);
      const dateStr = data.toLocaleDateString('pt-BR', {day: '2-digit', month: '2-digit'});
      const timeStr = data.toLocaleTimeString('pt-BR', {hour: '2-digit', minute: '2-digit'});
      
      const item = document.createElement("div");
      item.className = "history-item";
      item.style.position = "relative";
      if (currentSessionId === sessao.id) item.classList.add("active");

      item.innerHTML = `
        <div class="history-item-header">
          <div class="history-title" style="flex:1" title="${escapeHtml(sessao.titulo)}">${escapeHtml(sessao.titulo)}</div>
          <button class="history-kebab" title="Opções" onclick="event.stopPropagation(); toggleHistoryMenu(event, '${sessao.id}')">⋮</button>
        </div>
        <div class="history-meta">
          <span>${dateStr}</span>
          <span>•</span>
          <span>${timeStr}</span>
          <span>•</span>
          <span>${sessao.mensagens_count} msgs</span>
        </div>
      `;

      item.onclick = () => carregarSessao(sessao.id);
      list.appendChild(item);
    });
  } catch (e) {
    console.error("Erro ao carregar histórico:", e);
  }
}

// Menu "⋮" ÚNICO e global (fixed, fora da sidebar/lista) — evita o problema
// de dropdown cortado por overflow:hidden/overflow:auto dos containers pais.
// Reaproveitado por todo item da lista; guarda qual sessão está "no alvo"
// em _historyMenuAlvo e reposiciona via getBoundingClientRect no clique.
let _historyMenuAlvo = null;

function fecharHistoryMenus() {
  const menu = document.getElementById("history-global-menu");
  if (menu) menu.classList.remove("open");
  _historyMenuAlvo = null;
}

function toggleHistoryMenu(event, id) {
  const menu = document.getElementById("history-global-menu");
  if (!menu) return;
  const jaAbertoNesteId = menu.classList.contains("open") && _historyMenuAlvo === id;
  fecharHistoryMenus();
  if (jaAbertoNesteId) return;

  _historyMenuAlvo = id;
  const rect = event.currentTarget.getBoundingClientRect();
  menu.style.top = `${rect.bottom + 4}px`;
  const left = Math.min(rect.right - menu.offsetWidth, window.innerWidth - menu.offsetWidth - 8);
  menu.style.left = `${Math.max(8, left)}px`;
  menu.classList.add("open");
}

function historyMenuExportar() {
  if (_historyMenuAlvo) exportarSessao(_historyMenuAlvo);
  fecharHistoryMenus();
}

function historyMenuExcluir() {
  if (_historyMenuAlvo) excluirSessao(_historyMenuAlvo);
  fecharHistoryMenus();
}

// fecha qualquer menu aberto ao clicar fora dele
document.addEventListener("click", (e) => {
  if (!e.target.closest("#history-global-menu") && !e.target.closest(".history-kebab")) {
    fecharHistoryMenus();
  }
});

async function carregarSessao(id) {
  try {
    const r = await fetch(`/historico/carregar/${id}`);
    const sessao = await r.json();
    
    if (sessao.erro) {
      alert(sessao.erro);
      return;
    }
    
    currentSessionId = id;
    // limpa só as mensagens — nunca chat.innerHTML (apagaria input/gaveta)
    wrapper.innerHTML = "";
    
    // Renderiza mensagens da sessão — cada balão recebe o id real salvo no
    // disco (msg.id) + o id da sessão, pra o botão 🗑️ Apagar funcionar
    // direto, sem precisar recarregar nada.
    (sessao.mensagens || []).forEach(msg => {
      if (msg.role === "user") {
        addMessage("user", msg.content, null, null, null, msg.id, id);
      } else if (msg.role === "assistant") {
        addMessage("assistant", msg.content, null, null, null, msg.id, id);
      } else if (msg.role === "tool") {
        // Tool calls podem ser exibidos como chips se necessário
      }
    });
    
    carregarHistorico();
    
    if (window.innerWidth <= 1024) {
  
    }
  } catch (e) {
    console.error("Erro ao carregar sessão:", e);
  }
}

async function exportarSessao(id) {
  try {
    const r = await fetch(`/historico/exportar/${id}?formato=txt`);
    const texto = await r.text();
    
    const blob = new Blob([texto], {type: "text/plain;charset=utf-8"});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `jarvis-${id}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    console.error("Erro ao exportar:", e);
  }
}

async function excluirSessao(id) {
  if (!confirm("Tem certeza que deseja excluir esta sessão permanentemente?")) return;
  
  try {
    const r = await fetch(`/historico/excluir/${id}`, {method: "DELETE"});
    if (r.ok) {
      // Se estava vendo esta sessão, limpa o chat
      if (currentSessionId === id) {
        wrapper.innerHTML = "";
        renderWelcome();
        currentSessionId = null;
      }
      carregarHistorico(); // Atualiza lista
    }
  } catch (e) {
    console.error("Erro ao excluir:", e);
  }
}

async function buscarHistorico(query) {
  try {
    const r = await fetch(`/historico/buscar?q=${encodeURIComponent(query)}`);
    const data = await r.json();
    
    const list = document.getElementById("history-list");
    list.innerHTML = '<div class="section-title">Resultados</div>';
    
    if (!data.resultados || data.resultados.length === 0) {
      list.innerHTML += '<div style="padding:16px;color:var(--text-tertiary);font-size:13px">Nenhum resultado encontrado</div>';
      return;
    }
    
    data.resultados.forEach(sessao => {
      const item = document.createElement("div");
      item.className = "history-item";
      item.innerHTML = `
        <div class="history-title">${escapeHtml(sessao.titulo)}</div>
        <div class="history-meta">Match: ${sessao.match}</div>
      `;
      item.onclick = () => carregarSessao(sessao.id);
      list.appendChild(item);
    });
  } catch (e) {
    console.error("Erro na busca:", e);
  }
}

async function trocarModelo() {
  const nome = modelSelector.value;
  if (!nome) return;
  
  try {
    await fetch("/modelo", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({nome})
    });
  } catch (e) {
    console.error("Erro ao trocar modelo:", e);
  }
}

async function analisarPasta() {
  try {
    const r = await fetch('/selecionar-pasta');
    const d = await r.json();
    if (d.pasta) {
      input.value = `Analise a pasta ${d.pasta} e me diga a estrutura dos arquivos e pastas`;
      input.focus();
      input.dispatchEvent(new Event('input'));
    }
  } catch(e) {
    console.error('Erro ao abrir seletor de pasta:', e);
  }
}

async function quickAction(action) {
  const prompts = {
    'criar_arquivo': 'Crie um arquivo Python chamado hello.py que imprime "Olá, Mundo!"',
    'buscar_web': 'Busque as últimas notícias sobre inteligência artificial hoje',
    'gerar_imagem': 'Gere uma imagem de um robô futurista em uma cidade cyberpunk ao entardecer'
  };

  input.value = prompts[action] || '';
  autoResize(input);
  input.focus();
}

// ─────────────────────────────────────────
// STATUS & MODELS
// ─────────────────────────────────────────

let _ultimoStatus = {};

async function carregarStatus() {
    // Inicializa 's' com estrutura padrao para evitar erros
    let s = { ollama: '', cpu: 0, ram: 0, memoria: "" };

    try {
        const res = await fetch("/status");
        if (res.ok) {
            s = await res.json();
            _ultimoStatus = s;
        }
    } catch (e) {
        console.warn("[STATUS] Erro ao buscar status do servidor:", e);
    }

    try {
    // A partir daqui, 's' é sempre um objeto valido e seguro de ser lido
    const ollamaInd = document.getElementById("status-ollama");
    const ollamaText = document.getElementById("text-ollama");

    if (ollamaInd && ollamaText) {
        const ollamaOk = typeof s.ollama === 'string' && s.ollama.includes('online');
        ollamaInd.classList.toggle("offline", !ollamaOk);
        ollamaText.textContent = ollamaOk ? "Online" : "Offline";
        ollamaInd.title = "Ollama: " + ollamaText.textContent;
    }

    // Memory
    const memInd = document.getElementById("status-memory");
    const memText = document.getElementById("text-memory");
    
    if (memInd && memText) {
        const memOk = s.memoria === 'online';
        memInd.classList.toggle("offline", !memOk);
        memText.textContent = memOk ? "Online" : "Offline";
        memInd.title = "Memoria: " + memText.textContent;
    }

    // Images
    const imgInd = document.getElementById("status-images");
    const imgText = document.getElementById("text-images");
    const imgOk = s.imagens === 'online';
    imgInd.classList.toggle("offline", !imgOk);
    imgText.textContent = imgOk ? "Online" : "Offline";
    imgInd.title = "Imagens: " + imgText.textContent;

    // Vídeo (ffmpeg)
    const ffmpegInd = document.getElementById("status-ffmpeg");
    const ffmpegText = document.getElementById("text-ffmpeg");
    videoAtivoAtual = !!s.video_ativo;
    let ffmpegStatus;
    if (!s.ffmpeg) { ffmpegStatus = "Não achado"; }
    else if (s.video_ativo) { ffmpegStatus = "Ativo"; }
    else { ffmpegStatus = "Desligado"; }
    ffmpegInd.classList.toggle("offline", !s.ffmpeg || !s.video_ativo);
    ffmpegText.textContent = ffmpegStatus;
    ffmpegInd.title = "Vídeo: " + ffmpegStatus;
    const switchVideo = document.getElementById("switch-video");
    if (switchVideo) switchVideo.checked = videoAtivoAtual;

    // Model
      const modelText = document.getElementById("text-model");
      modelText.textContent = s.modelo || "-";

      // Modo Visão (imagem)
      if (s.visao) {
        modelText.textContent += " 👁️";
        modelText.title = "Modelo com visão - pode analisar imagens";
      }

      // Painel de Capacidades (ícones canto inferior direito)
      atualizarPainelCapacidades(s);

      // Reatualiza aviso de visão na área de anexos (quando modelo muda)
      renderAnexosPreview();

      // Sincroniza o dropdown do header com o modelo REAL do backend —
      // sem isso, o <select> só mostra a 1ª opção da lista (comportamento
      // padrão do HTML quando ninguém marca .value), o que fazia o
      // cabeçalho mostrar um modelo diferente do que está realmente ativo
      // (visível no painel "Status do Sistema" da sidebar).
      if (s?.modelo) sincronizarSeletor(s?.modelo);

      const statDot = document.getElementById("stat-dot");
      const statText = document.getElementById("stat-text");
      statDot.classList.remove("offline");
      statText.textContent = "Pronto";
  } catch (e) {
    console.error("Erro ao carregar status:", e);
  }

  // Atualiza botão de troca de backend (Ollama / LM Studio)
  if (s) {
    const backendAtual = s.backend_local || 'ollama';
    _atualizarBotaoBackend(backendAtual);
  }
}

// Painel de Capacidades — acende/apaga cada ícone conforme o /status
// mais recente. Chamado de dentro de carregarStatus(), então atualiza
// sozinho ao trocar de modelo, ligar/desligar function calling na aba
// Model, ou SD-API subir/cair (afeta o ícone de geração de imagem).
function atualizarPainelCapacidades(s) {
  const defs = [
    {id: "cap-visao", ativo: !!s.visao, titulo: "Visão: analisa imagens anexadas"},
    {id: "cap-web", ativo: !!s.web, titulo: "Busca na web (funciona com qualquer modelo)"},
    {id: "cap-pensamento", ativo: !!s.pensamento, titulo: "Pensamento: raciocínio nativo (thinking) antes de responder"},
    {id: "cap-ferramentas", ativo: !!s.ferramentas, titulo: "Outras ferramentas: criar arquivo, rodar código, MCP, missões etc"},
    {id: "cap-imagem", ativo: !!s.imagem_geracao, titulo: "Geração de imagem (Stable Diffusion) — funciona com qualquer modelo, só depende do SD-API estar ligado"},
  ];
  for (const d of defs) {
    const el = document.getElementById(d.id);
    if (!el) continue;
    el.classList.toggle("ativo", d.ativo);
    el.title = d.titulo + (d.ativo ? " — disponível agora" : " — indisponível com o modelo/config atual");
  }
  syncDrawerCapacidades();
}

// Mapeia descrição → categoria
function _categoriaPorDescricao(desc) {
  const d = (desc || "").toLowerCase();
  if (d.includes("nuvem")) return "Nuvem";
  if (d.includes("raciocínio") || d.includes("raciocinio")) return "Raciocínio";
  if (d.includes("código") || d.includes("codigo") || d.includes("code")) return "Código";
  if (d.includes("escrita") || d.includes("criatividade")) return "Escrita";
  if (d.includes("imagens") || d.includes("visão") || d.includes("visao") || d.includes("entende imagens")) return "Visão";
  if (d.includes("leve")) return "Leve";
  return "Uso Geral";
}

const _CATEGORIA_ORDER = ["Raciocínio", "Código", "Visão", "Escrita", "Leve", "Uso Geral", "Nuvem"];

async function carregarModelos() {
  try {
    const r = await (await fetch("/modelos")).json();

    // Atualiza o <select> oculto (compatibilidade com trocarModelo())
    modelSelector.innerHTML = r.modelos.map(x =>
      `<option value="${x.nome}">${x.nome}</option>`
    ).join("") || `<option value="">modelo</option>`;

    // Monta o painel custom agrupado por categoria
    const panel = document.getElementById("model-picker-panel");
    const sorted = _applyPickerOrder(r.modelos);
    const grupos = {};
    sorted.forEach(x => {
      const cat = _categoriaPorDescricao(x.descricao);
      if (!grupos[cat]) grupos[cat] = [];
      grupos[cat].push(x);
    });

    panel.innerHTML = _CATEGORIA_ORDER
      .filter(c => grupos[c])
      .map(cat => `
        <div class="model-picker-category">${cat}</div>
        ${grupos[cat].map(x => `
          <div class="model-picker-item" draggable="true" data-nome="${escapeHtml(x.nome)}" data-tools="${x.tools ? "true" : "false"}">
            <span class="model-picker-item-dot${x.tools ? " tools" : ""}"></span>
            <div class="model-picker-item-info">
              <div class="model-picker-item-name" title="${escapeHtml(x.nome)}">${escapeHtml(_nomeCurtoModelo(x.nome))}</div>
              <div class="model-picker-item-desc">${escapeHtml(x.descricao || "")}</div>
            </div>
            <span class="model-picker-drag" title="Reordenar">⠿</span>
          </div>`).join("")}
      `).join("");

    _initPickerDrag();

    // Aplica modelo real
    const s = await (await fetch("/status")).json();
    if (s?.modelo) sincronizarSeletor(s?.modelo);

  } catch (e) {
    console.error("Erro ao carregar modelos:", e);
  }
}

let _dragSrc = null, _wasDragging = false;

function _extrairTamanho(nome) {
  // Extrai Nb do tag (após ":"), ex: "qwen3:8b" → 8, "deepseek-r1:1.5b" → 1.5
  const tag = nome.includes(':') ? nome.split(':')[1] : nome;
  const m = tag.toLowerCase().match(/(\d+\.?\d*)b/);
  return m ? parseFloat(m[1]) : 9999;
}

function _applyPickerOrder(modelos) {
  try {
    const raw = localStorage.getItem('jarvis_picker_order');
    if (raw) {
      const idx = {};
      JSON.parse(raw).forEach((n, i) => idx[n] = i);
      return [...modelos].sort((a, b) => (idx[a.nome] ?? 9999) - (idx[b.nome] ?? 9999));
    }
  } catch {}
  // Padrão: locais do menor para o maior; nuvem ao final
  return [...modelos].sort((a, b) => {
    const aNuvem = (a.descricao || "").toLowerCase().includes("nuvem");
    const bNuvem = (b.descricao || "").toLowerCase().includes("nuvem");
    if (aNuvem !== bNuvem) return aNuvem ? 1 : -1;
    return _extrairTamanho(a.nome) - _extrairTamanho(b.nome);
  });
}

function _savePickerOrder() {
  const order = [];
    try { localStorage.setItem('jarvis_picker_order', JSON.stringify(order)); } catch {}
}

function _initPickerDrag() {
  document.querySelectorAll('.model-picker-item').forEach(item => {
    // clique seleciona — via data-nome (não onclick inline), porque
    // caminhos .gguf do Windows têm "\" que somem dentro de string JS
    item.addEventListener('click', () => {
      if (_wasDragging) return;
      _selecionarModelo(item.dataset.nome, item.dataset.tools === 'true');
    });
    item.addEventListener('dragstart', e => {
      _dragSrc = item;
      e.dataTransfer.effectAllowed = 'move';
      setTimeout(() => item.classList.add('dragging'), 0);
    });
    item.addEventListener('dragend', () => {
      _wasDragging = true;
      item.classList.remove('dragging');
      item.classList.remove('drag-over-top', 'drag-over-bottom');
      _savePickerOrder();
      setTimeout(() => { _wasDragging = false; }, 150);
    });
    item.addEventListener('dragover', e => {
      e.preventDefault();
      if (item === _dragSrc) return;
      const rect = item.getBoundingClientRect();
      item.classList.remove('drag-over-top', 'drag-over-bottom');
      item.classList.add(e.clientY < rect.top + rect.height / 2 ? 'drag-over-top' : 'drag-over-bottom');
    });
    item.addEventListener('dragleave', e => {
      if (!item.contains(e.relatedTarget))
        item.classList.remove('drag-over-top', 'drag-over-bottom');
    });
    item.addEventListener('drop', e => {
      e.preventDefault();
      if (!_dragSrc || _dragSrc === item) return;
      const rect = item.getBoundingClientRect();
      const before = e.clientY < rect.top + rect.height / 2;
      item.classList.remove('drag-over-top', 'drag-over-bottom');
      item.parentNode.insertBefore(_dragSrc, before ? item : item.nextSibling);
    });
  });
}

function _selecionarModelo(nome, tools) {
  // Atualiza select oculto e dispara troca
  modelSelector.value = nome;
  trocarModelo().then(() => carregarStatus());
  // Atualiza botão do picker
  document.getElementById("model-picker-name").textContent = _nomeCurtoModelo(nome);
  const dot = document.getElementById("model-picker-dot");
  dot.className = "model-picker-dot" + (tools ? " tools" : "");
  // Marca selecionado no painel
  document.querySelectorAll(".model-picker-item").forEach(el => {
    el.classList.toggle("selected", el.dataset.nome === nome);
  });
  // Fecha painel
  _fecharModelPicker();
}

function toggleModelPicker(e) {
  e.stopPropagation();
  const panel = document.getElementById("model-picker-panel");
  const btn = document.getElementById("model-picker-btn");
  const isOpen = panel.classList.toggle("open");
  btn.classList.toggle("open", isOpen);
  if (isOpen) {
    // posiciona sob o botão
    const rect = btn.getBoundingClientRect();
    panel.style.left = rect.left + "px";
    setTimeout(() => document.addEventListener("click", _fecharModelPickerOutside), 0);
  }
}

function _fecharModelPicker() {
  document.getElementById("model-picker-panel").classList.remove("open");
  document.getElementById("model-picker-btn").classList.remove("open");
  document.removeEventListener("click", _fecharModelPickerOutside);
}

function _fecharModelPickerOutside(e) {
  const panel = document.getElementById("model-picker-panel");
  const btn = document.getElementById("model-picker-btn");
  if (!panel.contains(e.target) && e.target !== btn) _fecharModelPicker();
}

// garante que o dropdown do header SEMPRE mostra o modelo real do
// backend, mesmo que ele não esteja na lista agora (ex: cérebro de nuvem
// momentaneamente fora do ar) — sem isso o navegador escolhe a 1ª opção
// sozinho, e o header mostra um modelo "fantasma" diferente do ativo.
function sincronizarSeletor(modeloReal) {
  if (!modelSelector.querySelector(`option[value="${CSS.escape(modeloReal)}"]`)) {
    const opt = document.createElement("option");
    opt.value = modeloReal;
    opt.textContent = modeloReal;
    modelSelector.prepend(opt);
  }
  if (modelSelector.value !== modeloReal) modelSelector.value = modeloReal;
  // Atualiza o botão visual do picker
  const nomeExibido = _nomeCurtoModelo(modeloReal);
  const nameEl = document.getElementById("model-picker-name");
  if (nameEl) nameEl.textContent = nomeExibido;
  // Marca selecionado no painel (se já renderizou)
  document.querySelectorAll(".model-picker-item").forEach(el => {
    el.classList.toggle("selected", el.dataset.nome === modeloReal);
  });
  // Atualiza dot (tenta detectar tools pelo item selecionado)
  const itemSel = document.querySelector(`.model-picker-item[data-nome="${CSS.escape(modeloReal)}"]`);
  const dot = document.getElementById("model-picker-dot");
  if (dot && itemSel) {
    const hasTools = itemSel.querySelector(".model-picker-item-dot.tools") !== null;
    dot.className = "model-picker-dot" + (hasTools ? " tools" : "");
  }
}

// Auto-refresh status
setInterval(() => {
  carregarStatus();
  carregarHistorico();
}, 15000);

// ── Zoom ──────────────────────────────────
//
// O zoom customizado via CSS "zoom" (usado antes) não é padrão e no
// Edge/Chromium moderno causa inconsistência nas coordenadas do mouse,
// impedindo seleção de texto. O usuário agora usa o zoom nativo do
// navegador (Ctrl + scroll / Ctrl + +/-) — viewport permite
// user-scalable=yes. O botão A+/A− da rail ainda funciona via
// ajustarZoom(); o Ctrl+scroll foi removido pro zoom do navegador
// fluir livremente.
let _zoom = parseInt(localStorage.getItem('jarvis_zoom') || '100');
let _zoomTimer = null;

function _aplicarZoom() {
  // Aplica zoom apenas ao conteúdo do chat, mantendo a barra de input fixa.
  // USA transform: scale() em vez da propriedade nativa "zoom" porque:
  //   - zoom: redimensiona o layout inteiro, quebrando a barra de rolagem
  //     (o scroll do mouse para de funcionar quando zoom != 100%).
  //   - transform: scale() só afeta a renderização visual, mantendo a
  //     área rolável e os pontos de clique alinhados com o conteúdo real.
  const zoomWrapper = document.getElementById('chat-zoom-wrapper');
  if (zoomWrapper) {
    zoomWrapper.style.transform = '';
    zoomWrapper.style.width = '';
    zoomWrapper.style.height = '';
    zoomWrapper.style.minHeight = '';
    zoomWrapper.style.zoom = '';
    zoomWrapper.style.transformOrigin = 'top center';
    zoomWrapper.style.transform = `scale(${_zoom / 100})`;
    // Garante que o scroll do mouse continue funcionando após a aplicação do zoom
    zoomWrapper.style.overflowY = 'auto';
    zoomWrapper.style.overflowX = 'hidden';
    zoomWrapper.style.scrollBehavior = 'smooth';
  }
  localStorage.setItem('jarvis_zoom', String(_zoom));
}

function ajustarZoom(delta) {
  _zoom = Math.max(50, Math.min(200, _zoom + delta));
  _aplicarZoom();
  const el = document.getElementById('zoom-indicator');
  if (el) {
    el.textContent = _zoom + '%';
    el.classList.add('visible');
    clearTimeout(_zoomTimer);
    _zoomTimer = setTimeout(() => el.classList.remove('visible'), 1400);
  }
}

// NOTE: Zoom NÃO é aplicado automaticamente ao carregar para não quebrar o scroll.
// O usuário deve clicar nos botões A+/A− da barra superior para ajustar o zoom se desejar.
// _aplicarZoom(); // restaura zoom salvo ao carregar

// ── Backend Switch (Ollama / LM Studio / KoboldCpp) ──────────────
const _BACKENDS = ['ollama', 'lmstudio', 'koboldcpp'];
const _BACKEND_NOMES = {ollama: 'Ollama', lmstudio: 'LM Studio', koboldcpp: 'KoboldCpp'};

function _atualizarBotaoBackend(backend) {
  const btn = document.getElementById('btn-backend-switch');
  if (!btn) return;
  const atual = _BACKENDS.includes(backend) ? backend : 'ollama';
  btn.dataset.backend = atual;
  const i = _BACKENDS.indexOf(atual);
  const proximo = _BACKEND_NOMES[_BACKENDS[(i + 1) % _BACKENDS.length]];
  btn.title = `Backend atual: ${_BACKEND_NOMES[atual]} — clique para trocar para ${proximo}`;
}

async function trocarBackend() {
  const btn = document.getElementById('btn-backend-switch');
  const atual = btn?.dataset.backend || 'ollama';
  const i = _BACKENDS.indexOf(atual);
  const novo = _BACKENDS[(i + 1) % _BACKENDS.length];

  // feedback visual imediato
  btn.disabled = true;
  btn.style.opacity = '0.5';

  try {
    const r = await fetch('/api/backend/trocar', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({backend: novo})
    });
    const d = await r.json();
    if (d.ok) {
      _atualizarBotaoBackend(d.backend);
      // recarrega modelos e status
      carregarModelos();
      carregarStatus();
    } else {
      alert('Erro ao trocar backend: ' + (d.erro || 'desconhecido'));
    }
  } catch (e) {
    console.error('Erro ao trocar backend:', e);
    alert('Não foi possível conectar ao backend.');
  } finally {
    btn.disabled = false;
    btn.style.opacity = '';
  }
}

// Init
carregarStatus();
carregarModelos();
carregarHistorico();
input.focus();
</script>
</body>
</html>
"""

@app.route("/")
def home():
    return HTML

@app.route("/imagem/<path:nome>")
def imagem(nome):
    return send_from_directory(WORKSPACE, nome)

@app.route("/ver-tela")
def ver_tela():
    """Captura a tela inteira com PIL.ImageGrab e salva no workspace."""
    try:
        from PIL import ImageGrab
        import time as _time
        img = ImageGrab.grab()
        nome = f"tela_{int(_time.time())}.png"
        caminho = os.path.join(WORKSPACE, nome)
        img.save(caminho, "PNG")
        return jsonify({"caminho": nome, "ok": True})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})

@app.route("/selecionar-pasta")
def selecionar_pasta():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes('-topmost', 1)
        pasta = filedialog.askdirectory(title="Selecionar pasta para análise")
        root.destroy()
        return jsonify({"pasta": pasta or ""})
    except Exception as e:
        return jsonify({"pasta": "", "erro": str(e)})

@app.route("/status")
def status():
    aviso = llm.verificar_ollama()
    mem = memory.status()
    
    stats = get_system_stats()
    
    try:
        imagens = "online" if requests.get(f"{SD_API_URL}/health", timeout=1.5).ok else "offline"
    except Exception:
        imagens = "offline"
    
    # Verifica se modelo atual suporta visão
    modelo_atual = llm.modelo_atual()
    modo_visao = llm.suporta_visao(modelo_atual)

    # 🔧 painel de capacidades (ícones): ferramentas = suporte real do
    # modelo E o toggle "Chamada de função" da aba Model estar ligado —
    # web/imagem são tools específicas, só disparam se ferramentas=True
    modo_ferramentas = llm.suporta_tools(modelo_atual) and model_params.obter().get("function_calling", True)
    modo_pensamento = llm.suporta_pensamento(modelo_atual)

    return jsonify({
        "modelo": modelo_atual,
        "ollama": "online" if aviso is None else str(aviso),
        "memoria": "online" if mem == "online" else "offline",
        "imagens": imagens,
        "visao": modo_visao,  # True se modelo suporta visão
        "pensamento": modo_pensamento,  # True se modelo tem raciocínio nativo (thinking)
        "ferramentas": modo_ferramentas,  # True só cobre as OUTRAS tools (criar
        # arquivo, rodar_python, MCP, missão etc) — essas sim exigem function
        # calling nativo, sem fallback.
        #
        # web e imagem NÃO dependem de ferramentas: agent.py tem um "modo
        # híbrido" (ver comentário "🌐🎨 modo híbrido" em agent.py) que, quando
        # o modelo NÃO suporta tool calling (ex: qwen2.5vl), classifica a
        # intenção da mensagem por fora e chama busca_web/gerar_imagem
        # diretamente — o modelo nunca precisa "saber" usar a ferramenta.
        # Por isso os dois funcionam com QUALQUER modelo.
        "web": True,  # busca_web sempre disponível (tool nativa OU fallback híbrido; Tavily com fallback pro DuckDuckGo)
        "imagem_geracao": imagens == "online",  # só depende do SD-API estar no ar, não do modelo
        # vídeo anexado no chat depende do ffmpeg pra extrair 1 frame —
        # mostra achado/não achado na hora, em vez de só falhar depois
        "ffmpeg": bool(_encontrar_ffmpeg()),
        "video_ativo": config.VIDEO_ATIVO,
        "auto_modelo": config.AUTO_MODELO_ATIVO,
        "ollama_url": config.OLLAMA_URL,
        "sd_api_url": SD_API_URL,
        "backend_local": llm.servidor_local_atual(),
        "stats": stats
    })

@app.route("/api/video-toggle", methods=["POST"])
def api_video_toggle():
    """Liga/desliga o processamento de vídeo anexado (extração de frame
    via ffmpeg) — efeito imediato (sem precisar reiniciar) e também
    salvo no .env pra persistir entre execuções."""
    d = request.json or {}
    ligado = bool(d.get("ativo", True))
    config.VIDEO_ATIVO = ligado
    try:
        config.atualizar_env({"VIDEO_ATIVO": "1" if ligado else "0"})
    except Exception:
        pass
    return jsonify({"ok": True, "video_ativo": config.VIDEO_ATIVO})

@app.route("/api/auto-modelo-toggle", methods=["POST"])
def api_auto_modelo_toggle():
    """Liga/desliga a troca automática de modelo LOCAL conforme o pedido
    (visão/código/raciocínio) — efeito imediato e salvo no .env."""
    d = request.get_json(silent=True) or {}
    ligado = bool(d.get("ativo", True))
    config.AUTO_MODELO_ATIVO = ligado
    try:
        config.atualizar_env({"AUTO_MODELO_ATIVO": "1" if ligado else "0"})
    except Exception:
        pass
    return jsonify({"ok": True, "auto_modelo": config.AUTO_MODELO_ATIVO})

@app.route("/api/backend/trocar", methods=["POST"])
def api_backend_trocar():
    """Troca o backend local entre Ollama e LM Studio sem reiniciar."""
    import llm as _llm_mod
    d = request.json or {}
    nome = (d.get("backend") or "").lower().strip()
    ok = _llm_mod.trocar_servidor_local(nome)
    return jsonify({"ok": ok, "backend": _llm_mod.servidor_local_atual()})

@app.route("/api/limpar-vram", methods=["POST"])
def api_limpar_vram():
    """Libera a VRAM agora: descarrega o modelo local atual do Ollama e
    pausa o SD-API (se estiver rodando) — sob demanda, em vez de esperar
    o timeout automático. Seguro: é a mesma liberação que já acontece
    sozinha depois de um tempo sem uso, só que na hora.

    Retorna tambem se o backend local esta online (pra UI saber dar
    feedback melhor quando Ollama/LM Studio estiver desligado)."""
    modelo_descarregado = llm.descarregar_modelo_atual()
    sd_api_pausado = _matar_processo_na_porta(urlparse(SD_API_URL).port or 8200)
    # health-check rapido pros dois backends locais
    ollama_online = False
    try:
        r = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=2)
        ollama_online = r.ok
    except Exception:
        pass
    if not ollama_online:
        try:
            r = requests.get(f"{config.LMSTUDIO_URL}/v1/models", timeout=2)
            ollama_online = r.ok
        except Exception:
            pass
    return jsonify({
        "ok": True,
        "modelo_descarregado": modelo_descarregado,
        "sd_api_pausado": sd_api_pausado,
        "backend_local_online": ollama_online,
    })

@app.route("/modelos")
def modelos():
    return jsonify({"modelos": llm.listar_modelos_detalhado()})

@app.route("/parar", methods=["POST"])
def parar():
    pedir_parada()
    return jsonify({"ok": True})

@app.route("/upload_imagem", methods=["POST"])
def upload_imagem():
    """Recebe imagem colada (Ctrl+V, data URL em base64) e salva no
    workspace. Usado só pelo fluxo de colar — upload de arquivo real
    (clicar no 📎 ou arrastar) usa /api/anexo, que aceita qualquer tipo
    (imagem OU vídeo) via multipart."""
    import base64
    import time

    data = request.json or {}
    img_data = data.get("imagem", "")

    if not img_data:
        return jsonify({"erro": "Nenhuma imagem"}), 400

    try:
        # Extrai base64 da data URL
        base64_str = img_data.split(",")[1]
        img_bytes = base64.b64decode(base64_str)

        # Salva no workspace/anexos
        nome_arquivo = f"pasted_{int(time.time())}.png"
        caminho = os.path.join(WORKSPACE, "anexos", nome_arquivo)
        os.makedirs(os.path.dirname(caminho), exist_ok=True)

        with open(caminho, "wb") as f:
            f.write(img_bytes)

        return jsonify({"arquivo": f"anexos/{nome_arquivo}", "tipo": "imagem"})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

_EXT_IMAGEM = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
_EXT_VIDEO_UPLOAD = (".mp4", ".mov", ".avi", ".mkv", ".webm")
_EXT_DOCUMENTO = (".pdf", ".docx", ".doc", ".xlsx", ".xlsm", ".csv", ".txt", ".md")

@app.route("/api/anexo", methods=["POST"])
def api_anexo():
    """Upload de arquivo real (clicar no 📎 ou arrastar/soltar) — aceita
    imagem, vídeo (vira 1 frame no backend, via ffmpeg, na hora de mandar
    pro modelo — ver agent._extrair_frame_video) OU documento (PDF, Word,
    Excel, CSV, texto — o texto é extraído automaticamente e injetado na
    mensagem, ver /chat). Sem limite de quantidade de arquivos por
    mensagem; o front manda um POST por arquivo solto/selecionado."""
    import uuid
    from werkzeug.utils import secure_filename

    if "arquivo" not in request.files:
        return jsonify({"ok": False, "erro": "nenhum arquivo enviado"}), 400

    f = request.files["arquivo"]
    if not f.filename:
        return jsonify({"ok": False, "erro": "nome de arquivo vazio"}), 400

    pasta_anexos = os.path.join(WORKSPACE, "anexos")
    os.makedirs(pasta_anexos, exist_ok=True)

    nome_seguro = secure_filename(f.filename) or "arquivo"
    nome_final = f"{uuid.uuid4().hex[:8]}_{nome_seguro}"
    caminho = os.path.join(pasta_anexos, nome_final)
    f.save(caminho)

    ext = os.path.splitext(nome_final)[1].lower()
    tipo = ("imagem" if ext in _EXT_IMAGEM else
            "video" if ext in _EXT_VIDEO_UPLOAD else
            "documento" if ext in _EXT_DOCUMENTO else
            "outro")

    if tipo == "outro":
        try:
            os.remove(caminho)
        except Exception:
            pass  # não crítico — o pior caso é sobrar um arquivo órfão em anexos/
        return jsonify({"ok": False, "erro": f"tipo de arquivo não suportado ({ext or 'sem extensão'})"}), 400

    relativo = os.path.join("anexos", nome_final).replace("\\", "/")
    return jsonify({
        "ok": True,
        "nome": f.filename,
        "caminho": relativo,
        "tipo": tipo,
        "tamanho_kb": os.path.getsize(caminho) // 1024,
        "url": f"/imagem/{relativo}" if tipo == "imagem" else None,
    })

# ─────────────────────────────────────────
# Gerenciamento — Cérebros de nuvem / MCP / Plugins / Pastas
# (backend já existia em cerebros.py, mcp_config.py, pastas_config.py e
# tools.py — só faltava expor via rota pra essa interface usar)
# ─────────────────────────────────────────

@app.route("/api/cerebros", methods=["GET"])
def api_cerebros_listar():
    return jsonify(cerebros.listar_todos_raw())

@app.route("/api/cerebros", methods=["POST"])
def api_cerebros_salvar():
    d = request.json or {}
    try:
        c = cerebros.salvar(
            d.get("nome"), d.get("tipo") or "https", d.get("url"),
            d.get("chave"), d.get("modelos"), d.get("ativo", True),
        )
        return jsonify({"ok": True, "cerebro": c})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 400

@app.route("/api/cerebros/<nome>", methods=["DELETE"])
def api_cerebros_remover(nome):
    return jsonify({"ok": cerebros.remover(nome)})

@app.route("/api/cerebros/descobrir", methods=["POST"])
def api_cerebros_descobrir():
    d = request.json or {}
    try:
        modelos = cerebros.descobrir_modelos(d.get("url", ""), d.get("chave", ""))
        return jsonify({"ok": True, "modelos": modelos})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 400

@app.route("/api/mcp", methods=["GET"])
def api_mcp_listar():
    return jsonify(mcp_config.ler())

@app.route("/api/mcp", methods=["POST"])
def api_mcp_salvar():
    d = request.json or {}
    try:
        s = mcp_config.salvar_servidor(
            d.get("nome"), d.get("command"), d.get("args") or [],
            d.get("env") or {}, d.get("ativo", True),
        )
        return jsonify({"ok": True, "servidor": s})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 400

@app.route("/api/mcp/<nome>", methods=["DELETE"])
def api_mcp_remover(nome):
    return jsonify({"ok": mcp_config.remover_servidor(nome)})

@app.route("/api/plugins", methods=["GET"])
def api_plugins_listar():
    return jsonify(listar_plugins())

@app.route("/api/plugins", methods=["POST"])
def api_plugins_definir():
    d = request.json or {}
    ok = definir_plugin(d.get("nome"), d.get("ligado", True))
    return jsonify({"ok": ok})

@app.route("/api/pastas", methods=["GET"])
def api_pastas_listar():
    return jsonify(pastas_config.listar())

@app.route("/api/pastas", methods=["POST"])
def api_pastas_adicionar():
    d = request.json or {}
    try:
        lista = pastas_config.adicionar(d.get("caminho", ""))
        return jsonify({"ok": True, "pastas": lista})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 400

@app.route("/api/pastas", methods=["DELETE"])
def api_pastas_remover():
    d = request.json or {}
    return jsonify({"ok": pastas_config.remover(d.get("caminho", ""))})

@app.route("/api/model_params", methods=["GET"])
def api_model_params_obter():
    return jsonify(model_params.obter())

@app.route("/api/model_params", methods=["POST"])
def api_model_params_salvar():
    d = request.json or {}
    try:
        atual = model_params.salvar(d)
        return jsonify({"ok": True, "parametros": atual})
    except ValueError as e:
        return jsonify({"ok": False, "erro": str(e)}), 400

@app.route("/api/modelo_info", methods=["GET"])
def api_modelo_info():
    """Ficha técnica do modelo ATIVO (ou ?nome=...) pra aba Model: nº de
    camadas (teto real do slider num_gpu), contexto de treino, arquitetura,
    tamanho em disco, e a SUGESTÃO de camadas pela VRAM livre AGORA
    (desempenho.py) — pra adicionar camadas à GPU aos poucos e testar."""
    nome = (request.args.get("nome") or "").strip() or None
    info = llm.informacoes_modelo(nome)

    vram = desempenho.vram_status()
    sugestao = None
    if isinstance(info, dict) and info.get("local") and not info.get("erro"):
        sugestao = desempenho.sugerir_num_gpu(
            (vram or {}).get("livre_bytes"),
            info.get("n_camadas"),
            info.get("tamanho_bytes"),
        )

    return jsonify({"info": info, "vram": vram, "sugestao_num_gpu": sugestao})

@app.route("/modelo", methods=["POST"])
def trocar_modelo():
    nome = (request.json or {}).get("nome", "").strip()
    if nome:
        llm.definir_modelo(nome)
        print(f"[CEREBRO] trocado para: {nome}")
        # persiste no .env para que reinicializações partam do mesmo modelo
        try:
            config.atualizar_env({"JARVIS_MODELO": nome})
        except Exception:
            pass
    return jsonify({"modelo": llm.modelo_atual()})

@app.route("/config/salvar", methods=["POST"])
def salvar_configuracoes():
    """Salva configurações do usuário no .env."""
    data = request.json or {}
    
    from config import atualizar_env
    
    # Atualiza chaves no .env
    valores = {}
    if "ollama_url" in data:
        valores["OLLAMA_URL"] = data["ollama_url"]
    
    if "sd_api_url" in data:
        valores["SD_API_URL"] = data["sd_api_url"]
    
    if "modelo" in data:
        valores["JARVIS_MODELO"] = data["modelo"]
    
    if valores:
        atualizar_env(valores)
    
    return jsonify({
        "ok": True, 
        "mensagem": "Configurações salvas! Reinicie o Jarvis para aplicar as mudanças."
    })

@app.route("/chat", methods=["POST"])
def chat_endpoint():
    global historico_curto, sessao_atual

    dados = request.json or {}
    msg = (dados.get("msg") or "").strip()
    # caminhos (relativos ao workspace, ex: "anexos/xxx.png") de imagens
    # OU vídeos anexados nesta mensagem — ver agent.processar_mensagem
    imagens = [c for c in (dados.get("imagens") or []) if isinstance(c, str) and c.strip()]
    # caminhos de documentos anexados (PDF, Word, Excel, CSV, texto) —
    # arrastados/soltos direto na caixa de mensagem, sem precisar digitar
    # caminho nenhum. O texto é extraído aqui (mesma leitura que a
    # ferramenta ler_arquivo faz) e injetado só nesta rodada, pro modelo
    # já responder com o conteúdo em mãos.
    documentos = [c for c in (dados.get("documentos") or []) if isinstance(c, str) and c.strip()]

    if not msg and not imagens and not documentos:
        return jsonify({"resposta": "(mensagem vazia)", "eventos": [], "sessao_id": sessao_atual.get("id")})

    # 🚀 INTERCEPTAÇÃO DIRETA: "abra X" → abre app sem passar pelo LLM
    # Só intercepta se for mensagem SÓ de texto (sem imagens/documentos)
    # e se o abridor estiver disponível.
    if msg and not imagens and not documentos and ABRIDOR_DISPONIVEL:
        resposta_direta = interceptar_abrir(msg)
        if resposta_direta is not None:
            # App aberto com sucesso (ou erro) — responde direto, sem LLM
            historico_curto.append({"role": "user", "content": msg})
            historico_curto.append({"role": "assistant", "content": resposta_direta})
            # Salva no histórico persistente também
            historico.adicionar_mensagem(sessao_atual, "user", msg)
            historico.adicionar_mensagem(sessao_atual, "assistant", resposta_direta)
            historico.salvar_sessao(sessao_atual)
            return jsonify({
                "resposta": resposta_direta,
                "eventos": [{"ferramenta": "abrir_app", "args": {"entrada": msg}, "resultado": resposta_direta}],
                "metricas": {"tempo_total_s": 0.01, "direto_sem_llm": True},
                "sessao_id": sessao_atual.get("id"),
                "direto_sem_llm": True
            })

    # Adiciona mensagem do usuário no histórico CURTO (para contexto da conversa)
    msg_historico = msg or "(anexo enviado sem texto)"
    historico_curto.append({"role": "user", "content": msg_historico})

    # Adiciona no histórico PERSISTENTE (para salvar em disco) — guarda o
    # id atribuído pra poder marcar o balão do usuário na hora (ver "fim"
    # mais abaixo), sem precisar recarregar a sessão inteira do disco.
    msg_usuario_salva = historico.adicionar_mensagem(sessao_atual, "user", msg_historico)
    user_msg_id = msg_usuario_salva.get("id")

    # Monta a mensagem que vai de fato pro modelo: texto do usuário +
    # conteúdo extraído de cada documento anexado. Só entra na chamada
    # desta rodada (msg_para_modelo) — o histórico guarda a mensagem
    # limpa (msg_historico), pra não inflar o contexto das próximas
    # rodadas com o PDF inteiro de novo.
    msg_para_modelo = msg
    for doc in documentos:
        nome_doc = os.path.basename(doc)
        try:
            conteudo_doc = ler_arquivo(doc)
        except Exception as e:
            conteudo_doc = f"ERRO ao ler arquivo: {e}"
        msg_para_modelo += f"\n\n[Arquivo anexado pelo usuário: {nome_doc}]\n{conteudo_doc}"
    if not msg and documentos:
        msg_para_modelo = msg_para_modelo.strip()

    eventos = []
    fila = queue.Queue()

    def on_evento(nome, args, resultado):
        evento = {
            "ferramenta": nome,
            "args": args,
            "resultado": str(resultado)[:400]
        }
        eventos.append(evento)
        # Tool calls também vão para ambos históricos
        historico_curto.append({"role": "tool", "content": f"{nome}: {resultado}"})
        historico.adicionar_mensagem(
            sessao_atual,
            "tool",
            f"Ferramenta: {nome}\\nArgs: {args}\\nResultado: {resultado}"
        )
        fila.put({"tipo": "evento", "evento": evento})

    def on_token(texto):
        if texto:
            fila.put({"tipo": "token", "texto": texto})

    sessao_id_atual = sessao_atual.get("id")
    planejamento_ativo = MODO_PLANEJAMENTO.get(sessao_id_atual, False)
    assistant_msg_id = None

    def trabalhar():
        global historico_curto
        nonlocal assistant_msg_id

        metricas = {}
        try:
            resposta, metricas = processar_mensagem(
                msg_para_modelo, historico_curto, on_evento, imagens=imagens,
                modo_planejamento=planejamento_ativo, on_token=on_token
            )

            # a mensagem pode ter sido a confirmação que encerra o modo
            # planejamento — agent.py avisa isso via metricas["modo_planejamento_ativo"]
            if planejamento_ativo and not metricas.get("modo_planejamento_ativo", False):
                MODO_PLANEJAMENTO[sessao_id_atual] = False

            # Adiciona resposta em ambos históricos
            historico_curto.append({"role": "assistant", "content": resposta})
            msg_assistente_salva = historico.adicionar_mensagem(sessao_atual, "assistant", resposta)
            assistant_msg_id = msg_assistente_salva.get("id")
            historico.salvar_sessao(sessao_atual)

        except Exception as e:
            erro_str = str(e)
            if "11434" in erro_str and ("403" in erro_str or "Forbidden" in erro_str):
                resposta = (
                    "Erro: o Ollama recusou a requisição (403 Forbidden em "
                    "localhost:11434). Isso é o serviço do Ollama, não o "
                    "Jarvis — confira se ele ainda está rodando ('ollama "
                    "list' num terminal) e se nenhum outro programa tomou a "
                    "porta 11434."
                )
            else:
                resposta = f"Erro: {e}"
            historico_curto.append({"role": "assistant", "content": resposta})
            msg_assistente_salva = historico.adicionar_mensagem(sessao_atual, "assistant", f"Erro: {e}")
            assistant_msg_id = msg_assistente_salva.get("id")
            historico.salvar_sessao(sessao_atual)

        # teto no histórico curto (contexto passado ao modelo): sem isso, uma
        # conversa longa acumula tokens sem limite e até "boa noite" começa a
        # carregar milhares de tokens de contexto à toa — mantém só as
        # últimas trocas, o histórico PERSISTENTE (em disco) continua completo
        historico_curto = historico_curto[-MAX_HISTORICO_CURTO:]

        fila.put({
            "tipo": "fim",
            "resposta": resposta,
            "eventos": eventos,
            "metricas": metricas,
            "sessao_id": sessao_atual.get("id"),
            "user_msg_id": user_msg_id,
            "assistant_msg_id": assistant_msg_id,
            "modo_planejamento_ativo": MODO_PLANEJAMENTO.get(sessao_id_atual, False),
        })
        fila.put(None)  # sentinela: fim do stream

    threading.Thread(target=trabalhar, daemon=True).start()

    def gerar():
        # heartbeat: sem isso, quando o modelo demora muito pra responder
        # (Gemini/9router travando, por ex.) a conexao HTTP fica minutos
        # sem nenhum byte trafegando — e alguns proxies/navegadores acabam
        # encerrando essa conexao "silenciosamente" sozinhos, o que o front
        # confunde com "terminou normalmente" (stream fecha sem erro) e
        # esconde o botao de parar mesmo sem ter chegado resposta nenhuma
        # (bug relatado em 19/08/2026: botao some, "digitando..." fica
        # preso). Manda um pingue a cada 15s pra manter a conexao viva.
        while True:
            try:
                item = fila.get(timeout=15)
            except queue.Empty:
                yield json.dumps({"tipo": "heartbeat"}, ensure_ascii=False) + "\n"
                continue
            if item is None:
                break
            yield json.dumps(item, ensure_ascii=False) + "\n"

    return Response(stream_with_context(gerar()), mimetype="application/x-ndjson")

@app.route("/planejamento/toggle", methods=["POST"])
def planejamento_toggle():
    """Liga/desliga o modo planejamento da sessão ativa. Enquanto ligado,
    o Jarvis só conversa em texto (sem tools/thinking) até o usuário
    confirmar com uma palavra-gatilho (faça, comece, concordo, prossiga...),
    aí ele libera o fluxo completo (incluindo missão/multiagente) sozinho."""
    sessao_id_atual = sessao_atual.get("id")
    novo_estado = not MODO_PLANEJAMENTO.get(sessao_id_atual, False)
    MODO_PLANEJAMENTO[sessao_id_atual] = novo_estado
    return jsonify({"ativo": novo_estado})

@app.route("/api/exportar", methods=["POST"])
def exportar_resposta():
    """Exporta uma resposta do Jarvis (markdown puro) para .md/.docx/.pdf."""
    dados = request.json or {}
    texto = dados.get("texto") or ""
    formato = (dados.get("formato") or "md").lower()

    if not texto.strip():
        return jsonify({"erro": "Nada para exportar"}), 400

    try:
        conteudo, mimetype, extensao = exportar.exportar(texto, formato)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        return jsonify({"erro": f"Falha ao gerar {formato}: {e}"}), 500

    nome_arquivo = f"jarvis_resposta_{int(time.time())}.{extensao}"
    return send_file(
        io.BytesIO(conteudo),
        mimetype=mimetype,
        as_attachment=True,
        download_name=nome_arquivo,
    )

@app.route("/api/tela/print", methods=["POST"])
def api_tela_print():
    """Captura de tela guiada: dispara o seletor de área (tkinter, o usuário
    arrasta o mouse na tela) e devolve o resultado — sem passar pelo loop
    do LLM. Chamado pelo botão dedicado de print na barra de ações."""
    resultado = executar_ferramenta("tirar_print", {})
    m = re.search(r"IMAGEM_GERADA:\s*(\S+)", resultado or "")
    if m:
        return jsonify({"ok": True, "nome": m.group(1), "resultado": resultado})
    if "cancelad" in (resultado or "").lower():
        return jsonify({"ok": False, "cancelado": True, "mensagem": resultado})
    return jsonify({"ok": False, "mensagem": resultado}), 500

@app.route("/api/tela/gravar/iniciar", methods=["POST"])
def api_tela_gravar_iniciar():
    """Gravação de tela guiada: dispara o mesmo seletor de área do print,
    mas começa a GRAVAR vídeo dessa região (ffmpeg) até o usuário clicar
    de novo pra parar — sem passar pelo loop do LLM. Chamado pelo botão
    dedicado de gravação na barra de ações."""
    resultado = executar_ferramenta("gravar_tela", {})
    if resultado and resultado.startswith("Gravação iniciada"):
        return jsonify({"ok": True, "resultado": resultado})
    if "cancelad" in (resultado or "").lower():
        return jsonify({"ok": False, "cancelado": True, "mensagem": resultado})
    return jsonify({"ok": False, "mensagem": resultado}), 500

@app.route("/api/tela/gravar/parar", methods=["POST"])
def api_tela_gravar_parar():
    """Encerra a gravação de tela iniciada por /api/tela/gravar/iniciar e
    devolve o vídeo pronto — sem passar pelo loop do LLM."""
    resultado = executar_ferramenta("parar_gravacao_tela", {})
    m = re.search(r"VIDEO_GERADO:\s*(\S+)", resultado or "")
    if m:
        return jsonify({"ok": True, "nome": m.group(1), "resultado": resultado})
    return jsonify({"ok": False, "mensagem": resultado}), 500

@app.route("/historico/listar", methods=["GET"])
def listar_historico():
    limite = request.args.get("limite", 50, type=int)
    sessoes = historico.listar_sessoes(limite)
    return jsonify({"sessoes": sessoes})

@app.route("/historico/carregar/<id_sessao>", methods=["GET"])
def carregar_historico(id_sessao):
    sessao = historico.carregar_sessao(id_sessao)
    if not sessao:
        return jsonify({"erro": "Sessão não encontrada"}), 404
    return jsonify(sessao)

@app.route("/historico/nova", methods=["POST"])
def nova_sessao():
    global sessao_atual, historico_curto
    
    titulo = (request.get_json(silent=True) or {}).get("titulo") or None
    sessao_atual = historico.criar_sessao(titulo)
    historico_curto = []
    
    return jsonify({
        "id": sessao_atual["id"],
        "titulo": sessao_atual["titulo"],
        "criada_em": sessao_atual["criada_em"]
    })

@app.route("/historico/excluir/<id_sessao>", methods=["DELETE"])
def excluir_sessao(id_sessao):
    if historico.excluir_sessao(id_sessao):
        return jsonify({"ok": True})
    return jsonify({"erro": "Sessão não encontrada"}), 404

@app.route("/historico/<id_sessao>/mensagem/<msg_id>", methods=["DELETE"])
def excluir_mensagem_historico(id_sessao, msg_id):
    """Apaga só UM balão (mensagem) de uma sessão, sem mexer no resto da
    conversa — usado pelo botão 🗑️ que fica embaixo de cada balão."""
    ok = historico.excluir_mensagem(id_sessao, msg_id)
    if not ok:
        return jsonify({"ok": False, "erro": "mensagem não encontrada"}), 404

    # sessao_atual é mantida em memória e só vai pro disco de novo na
    # próxima resposta — se a exclusão foi na sessão ATIVA, recarrega do
    # disco agora, senão a próxima gravação (historico.salvar_sessao)
    # reescreveria o arquivo com a versão antiga (sem a exclusão) por cima.
    global sessao_atual
    if sessao_atual.get("id") == id_sessao:
        recarregada = historico.carregar_sessao(id_sessao)
        if recarregada:
            sessao_atual = recarregada

    return jsonify({"ok": True})

@app.route("/historico/apagar-tudo", methods=["POST"])
def apagar_tudo_historico():
    """Apaga TODO o histórico salvo em disco. Se houver uma sessão aberta
    no momento, ela é preserva (pra não zerar a conversa atual de repente)."""
    sessao_aberta = sessao_atual.get("id") if sessao_atual else None
    removidas = historico.apagar_todas_sessoes(manter_id=sessao_aberta)
    return jsonify({"ok": True, "removidas": removidas})

@app.route("/historico/exportar/<id_sessao>", methods=["GET"])
def exportar_sessao(id_sessao):
    formato = request.args.get("formato", "json")
    conteudo = historico.exportar_sessao(id_sessao, formato)
    
    if not conteudo:
        return jsonify({"erro": "Sessão não encontrada"}), 404
    
    return app.response_class(
        response=conteudo,
        status=200,
        mimetype="application/json" if formato == "json" else "text/plain"
    )

@app.route("/historico/buscar", methods=["GET"])
def buscar_historico():
    query = request.args.get("q", "")
    limite = request.args.get("limite", 10, type=int)
    
    if not query:
        return jsonify({"erro": "Query necessária"}), 400
    
    resultados = historico.buscar_sessoes(query, limite)
    return jsonify({"resultados": resultados})

if __name__ == "__main__":
    import sys
    # --flask-only: roda só o servidor Flask (sem bandeja, sem tray, sem SD-API auto)
    # usado pelo jarvis-configurar.bat pra abrir a aba Model antes do chat
    if "--flask-only" in sys.argv:
        print("[Jarvis] Modo --flask-only: apenas servidor Flask na porta 5000")
        app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False, threaded=True)
    else:
        # modo normal (bandeja.py ou app.py chamam app.run direto)
        pass
