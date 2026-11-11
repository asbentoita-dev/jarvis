# 🔌 Plugin: captura de tela do DESKTOP (print e gravação de vídeo)
#
# Diferente de navegador.capturar_tela (que só printa uma página que o
# PRÓPRIO Jarvis abriu num navegador headless), este aqui captura a tela
# REAL do usuário: mostra um overlay escurecido, o usuário arrasta o
# mouse pra desenhar um retângulo em cima de QUALQUER parte da tela, e o
# Jarvis tira o print (ou começa a gravar) só daquela área.
#
# Dependências: Pillow (já está no requirements.txt) + tkinter (vem de
# fábrica no Python) + ffmpeg (mesmo executável já configurado em
# config.FFMPEG_PATH / detectado por agent._encontrar_ffmpeg — reaproveita
# a busca que já existe pra extração de frame de vídeo anexado).
#
# ⚠️ Roda no Windows (usa GetSystemMetrics do user32 pra achar os limites
# da área virtual — soma de TODOS os monitores — e ffmpeg -f gdigrab, que
# é a captura de tela nativa do ffmpeg pro Windows). Multi-monitor: o
# print funciona certo em qualquer monitor (Pillow ImageGrab com
# all_screens=True lida bem com coordenadas negativas de monitores à
# esquerda/acima do principal). A GRAVAÇÃO em vídeo com gdigrab é mais
# confiável quando a área selecionada fica dentro do monitor principal —
# monitores secundários com coordenada negativa podem falhar dependendo
# da versão do ffmpeg; se acontecer, selecione a área no monitor
# principal.

import ctypes
import os
import re
import subprocess
import threading
import time

# Sem isso, todo processo que a gente abre com Popen/run (ffmpeg, tanto
# a listagem de dispositivos quanto a gravação em si) puxa uma janela de
# terminal preta junto — porque o Jarvis quando empacotado com PyInstaller
# não tem console próprio, e o Windows abre um NOVO console só pro
# processo filho. CREATE_NO_WINDOW pede pra não abrir nenhuma janela de
# console pro processo filho. Só existe no Windows; getattr com default 0
# evita AttributeError se alguém rodar isso fora do Windows (mesmo o
# plugin sendo Windows-only, não custa nada ser defensivo aqui).
_SEM_JANELA = getattr(subprocess, "CREATE_NO_WINDOW", 0)

from PIL import ImageGrab

import config


def _achar_ffmpeg():
    """Import TARDIO (só na hora de chamar, não no topo do arquivo) —
    proposital. agent.py importa tools.py logo na linha 16, e é o
    tools.py quem carrega este plugin; ou seja, quando este arquivo é
    carregado, agent.py ainda está no meio da própria inicialização e a
    função _encontrar_ffmpeg (definida bem mais abaixo em agent.py) ainda
    nem existe. Um `from agent import _encontrar_ffmpeg` aqui no topo
    sempre falhava (import circular) e ficava None pra sempre — por isso
    a gravação dava 'ffmpeg não encontrado' mesmo com FFMPEG_PATH certo
    no .env. Resolvendo em tempo de CHAMADA, agent.py já está 100%
    carregado."""
    try:
        import agent
        return agent._encontrar_ffmpeg()
    except Exception:
        return None


# ── seleção de área na tela (overlay com tkinter) ──

def _area_virtual():
    """Retorno (x, y, largura, altura) cobrindo TODOS os monitores juntos
    (área virtual do Windows), não só o monitor principal — pra quem tem
    mais de uma tela poder selecionar em qualquer uma delas."""
    u32 = ctypes.windll.user32
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    x = u32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = u32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    largura = u32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    altura = u32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return x, y, largura, altura


def _selecionar_area():
    """Abre um overlay escurecido cobrindo toda a área virtual (todos os
    monitores). Usuário arrasta o mouse pra desenhar o retângulo; solta
    o botão pra confirmar, ou Esc pra cancelar. Devolve (x1, y1, x2, y2)
    em coordenadas absolutas de tela (compatíveis com ImageGrab e
    ffmpeg -offset_x/-offset_y), ou None se cancelado/área vazia."""
    import tkinter as tk

    vx, vy, vw, vh = _area_virtual()

    resultado = {"bbox": None}

    janela = tk.Tk()
    janela.overrideredirect(True)  # sem borda/barra de título
    janela.attributes("-topmost", True)
    janela.attributes("-alpha", 0.35)  # semitransparente, dá pra ver a tela por baixo
    janela.geometry(f"{vw}x{vh}+{vx}+{vy}")
    janela.configure(bg="black", cursor="cross")

    canvas = tk.Canvas(janela, bg="black", highlightthickness=0, cursor="cross")
    canvas.pack(fill="both", expand=True)
    canvas.create_text(
        vw // 2, 30, fill="white",
        font=("Segoe UI", 14, "bold"),
        text="Arraste o mouse pra selecionar a área — Esc pra cancelar"
    )

    estado = {"x0": None, "y0": None, "retangulo": None}

    def ao_pressionar(evento):
        estado["x0"], estado["y0"] = evento.x, evento.y
        if estado["retangulo"]:
            canvas.delete(estado["retangulo"])
        estado["retangulo"] = canvas.create_rectangle(
            evento.x, evento.y, evento.x, evento.y,
            outline="#D85A30", width=2
        )

    def ao_arrastar(evento):
        if estado["retangulo"] is not None:
            canvas.coords(estado["retangulo"], estado["x0"], estado["y0"], evento.x, evento.y)

    def ao_soltar(evento):
        x0, y0 = estado["x0"], estado["y0"]
        x1, y1 = evento.x, evento.y
        # normaliza (o usuário pode arrastar em qualquer direção)
        esquerda, direita = sorted((x0, x1))
        topo, baixo = sorted((y0, y1))
        if direita - esquerda > 4 and baixo - topo > 4:  # ignora clique sem arrastar
            # converte de coordenada da janela (relativa) pra coordenada
            # absoluta de tela (soma o offset da área virtual)
            resultado["bbox"] = (vx + esquerda, vy + topo, vx + direita, vy + baixo)
        janela.destroy()

    def ao_cancelar(evento=None):
        resultado["bbox"] = None
        janela.destroy()

    canvas.bind("<ButtonPress-1>", ao_pressionar)
    canvas.bind("<B1-Motion>", ao_arrastar)
    canvas.bind("<ButtonRelease-1>", ao_soltar)
    janela.bind("<Escape>", ao_cancelar)

    # ⚠️ safety net: se o loop de eventos travar (raro, mas acontece com
    # Tkinter em threads não-main), a janela morre sozinha após 30s e
    # devolve bbox=None em vez de ficar travada bloqueando o mouse
    # pra sempre. Ver _agendamento_destruicao().
    _agendamento_destruicao(janela, 30000)

    janela.focus_force()
    janela.mainloop()

    return resultado["bbox"]


# ── print (screenshot) ──

_FORMATOS_IMAGEM = {
    # formato: (extensão, nome pro Pillow salvar)
    "png": ("png", "PNG"),    # sem perda — aceito em toda rede social (Instagram, X, Facebook, LinkedIn, TikTok)
    "jpg": ("jpg", "JPEG"),   # menor tamanho — padrão de foto na maioria das redes
    "jpeg": ("jpg", "JPEG"),
    "webp": ("webp", "WEBP"),  # mais leve/moderno — aceito por Instagram/Facebook/X (TikTok é mais restrito com esse)
}


def tirar_print(formato="png"):
    """Deixa o usuário selecionar uma área da tela com o mouse e tira um
    print dela. formato: png (padrão, sem perda), jpg/jpeg (menor,
    padrão de foto nas redes sociais) ou webp (mais leve)."""
    formato = (formato or "png").strip().lower()
    if formato not in _FORMATOS_IMAGEM:
        return f"ERRO: formato '{formato}' não suportado. Use: {', '.join(sorted(set(v[0] for v in _FORMATOS_IMAGEM.values())))}."

    bbox = _selecionar_area()
    if not bbox:
        return "Seleção cancelada pelo usuário (nenhuma área foi escolhida)."

    ext, pillow_formato = _FORMATOS_IMAGEM[formato]
    nome = f"print_{time.strftime('%Y%m%d_%H%M%S')}.{ext}"
    caminho = os.path.join(config.WORKSPACE, nome)

    try:
        # all_screens=True: essencial pra bbox com coordenada negativa
        # funcionar certo em quem tem monitor secundário à esquerda/acima
        imagem = ImageGrab.grab(bbox=bbox, all_screens=True)
        if pillow_formato == "JPEG" and imagem.mode in ("RGBA", "P"):
            imagem = imagem.convert("RGB")  # JPEG não aceita canal alfa
        imagem.save(caminho, pillow_formato)
    except Exception as e:
        return f"ERRO ao capturar tela: {type(e).__name__}: {e}"

    largura = bbox[2] - bbox[0]
    altura = bbox[3] - bbox[1]
    return f"IMAGEM_GERADA: {nome} (print de {largura}x{altura}px)"


# ── overlay de "tô gravando aqui" (contorno + cronômetro na tela) ──
#
# Objetivo: o usuário pediu pra CONFERIR visualmente se a área escolhida
# está certa enquanto grava, sem que essa marcação apareça no vídeo
# salvo. A saída é a API nativa do Windows SetWindowDisplayAffinity com
# WDA_EXCLUDEFROMCAPTURE — ela marca uma janela como "invisível pra
# QUALQUER captura de tela" (BitBlt/GDI — que é exatamente o que o
# gdigrab do ffmpeg usa —, Desktop Duplication, Windows.Graphics.Capture
# etc). O olho humano continua vendo a janela normalmente; só a captura
# de tela é que "pula" ela. Disponível desde o Windows 10 versão 2004
# (maio/2020) — em versões mais antigas a chamada falha, e nesse caso a
# gravação continua funcionando normal, só sem o contorno/cronômetro
# (fail-safe: preferimos gravar sem overlay a arriscar vazar o overlay
# pro vídeo).

WDA_EXCLUDEFROMCAPTURE = 0x00000011
_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_COR_CHAVE = "#123456"  # cor "impossível" usada só como marcador de transparência


def _excluir_da_captura(hwnd):
    """Aplica WDA_EXCLUDEFROMCAPTURE na janela. Devolve True se aplicou."""
    try:
        ok = ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        return bool(ok)
    except Exception:
        return False


def _deixar_clique_atravessar(hwnd):
    """Faz cliques do mouse passarem DIRETO pra janela/app que está por
    baixo do overlay — sem isso, o contorno (mesmo invisível no vídeo)
    ficaria bloqueando o usuário de clicar em qualquer coisa embaixo dele
    enquanto grava."""
    try:
        estilo = ctypes.windll.user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            hwnd, _GWL_EXSTYLE, estilo | _WS_EX_LAYERED | _WS_EX_TRANSPARENT
        )
    except Exception:
        pass


def _agendamento_destruicao(janela, ms):
    """Agenda a destruição da janela Tkinter após `ms` milisegundos, só
    se ela ainda estiver viva. Safety net: Tkinter em threads não-main
    pode travar o loop de eventos (raro, mas real), e sem isso a janela
    fica presa na tela pra sempre, bloqueando o mouse no Jarvis."""
    try:
        def _destruir():
            try:
                if janela.winfo_exists():
                    janela.destroy()
            except Exception:
                pass
        janela.after(ms, _destruir)
    except Exception:
        pass


def _rodar_overlay_gravacao(bbox, parar_evento, inicio_ts):
    """Roda numa thread própria (precisa da SUA própria instância de Tk() +
    mainloop, separada da que fez a seleção de área). Desenha só o
    CONTORNO do retângulo escolhido (centro fica com a "cor-chave", que
    o Tkinter torna clicável-através-e-transparente na tela) mais um
    textinho "⏺ REC mm:ss" no canto superior esquerdo, atualizado a cada
    segundo. Fecha sozinho quando parar_evento é sinalizado (chamado por
    parar_gravacao_tela)."""
    try:
        import tkinter as tk
    except Exception:
        return  # sem tkinter, sem overlay — gravação continua normal

    x0, y0, x1, y1 = bbox
    largura, altura = x1 - x0, y1 - y0
    if largura <= 0 or altura <= 0:
        return

    try:
        janela = tk.Tk()
        janela.overrideredirect(True)
        janela.attributes("-topmost", True)
        janela.geometry(f"{largura}x{altura}+{x0}+{y0}")
        janela.configure(bg=_COR_CHAVE)
        janela.attributes("-transparentcolor", _COR_CHAVE)

        canvas = tk.Canvas(janela, bg=_COR_CHAVE, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        # contorno fino, 2px pra dentro da borda pra não cortar em telas
        # com escala/DPI diferente
        canvas.create_rectangle(1, 1, largura - 2, altura - 2,
                                 outline="#00E08F", width=2)
        texto_id = canvas.create_text(
            10, 10, anchor="nw", fill="#00E08F",
            font=("Segoe UI", 12, "bold"), text="⏺ REC 00:00"
        )
        # fundo escuro só atrás do textinho, pra ler mesmo em tela clara
        canvas.tag_lower(
            canvas.create_rectangle(4, 4, 110, 26, fill="#000000", outline=""),
            texto_id
        )

        janela.update_idletasks()
        hwnd = janela.winfo_id()
        excluida = _excluir_da_captura(hwnd)
        _deixar_clique_atravessar(hwnd)

        if not excluida:
            # não conseguiu excluir da captura (Windows antigo) — não
            # arrisca deixar o contorno vazar pro vídeo, fecha e sai.
            janela.destroy()
            return

        def atualizar():
            if parar_evento.is_set():
                janela.destroy()
                return
            decorridos = int(time.time() - inicio_ts)
            mm, ss = divmod(decorridos, 60)
            canvas.itemconfig(texto_id, text=f"⏺ REC {mm:02d}:{ss:02d}")
            janela.after(500, atualizar)

        atualizar()
        # ⚠️ safety net: se o Jarvis morrer sem sinalizar parar_evento
        # (ex: crash no Flask), o overlay não fica preso na tela pra
        # sempre. 10 min é mais do que qualquer gravação razoável.
        _agendamento_destruicao(janela, 600000)
        janela.mainloop()
    except Exception:
        try:
            janela.destroy()
        except Exception:
            pass


_PISTAS_LINHA_DE_ERRO = [
    "error", "erro", "could not", "cannot", "invalid", "failed", "no such",
    "not found", "unable", "permission denied", "i/o error",
]


def _ler_fim_do_log(log_path, max_linhas=25):
    """Lê o log de erro do ffmpeg pra mostrar pro usuário. Prioriza
    linhas que PARECEM ser a mensagem de erro de verdade (contêm
    "Error", "Could not", "Invalid" etc.) em vez de só pegar as últimas
    N linhas cruas — o ffmpeg costuma imprimir um bocado de informação
    "neutra" (versão das libs, formato do stream) DEPOIS de já ter
    logado o motivo real do problema mais acima, então "as últimas
    linhas" nem sempre é "as linhas úteis". Nunca lança exceção: se não
    conseguir ler, devolve uma mensagem genérica em vez de quebrar o
    fluxo de erro (que já está tratando outro erro)."""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            linhas = [l.rstrip("\n") for l in f.readlines()]
        if not linhas:
            return "(o ffmpeg não deixou nenhuma mensagem de log)"

        relevantes = [l for l in linhas if any(p in l.lower() for p in _PISTAS_LINHA_DE_ERRO)]
        partes = []
        if relevantes:
            partes.append("Linhas de erro encontradas no log do ffmpeg:\n" + "\n".join(relevantes[-10:]))
        partes.append("Últimas linhas do log (contexto):\n" + "\n".join(linhas[-max_linhas:]))
        return "\n\n".join(partes).strip()
    except Exception:
        return "(não consegui ler o log do ffmpeg)"


# ── gravação de vídeo (ffmpeg -f gdigrab) + ÁUDIO (ffmpeg -f dshow) ──

_FORMATOS_VIDEO = {
    # formato: (extensão, args de codec de vídeo, args de codec de áudio)
    #
    # Áudio em MP3 (libmp3lame) sempre que o container aceitar — é o
    # formato de áudio mais universalmente compatível no Windows (toca
    # em QUALQUER player, sem precisar de codec extra instalado, ao
    # contrário do AAC que às vezes depende de codec pack). A exceção é
    # o .webm: por especificação do próprio formato, ele só aceita
    # áudio Opus ou Vorbis — MP3 dentro de um .webm gera um arquivo
    # inválido que nem o Chrome toca, então ali mantém Opus.
    "mp4": ("mp4", ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"],
            ["-c:a", "libmp3lame", "-b:a", "192k"]),
    "mov": ("mov", ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"],
            ["-c:a", "libmp3lame", "-b:a", "192k"]),
    "webm": ("webm", ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0"],
             ["-c:a", "libopus", "-b:a", "128k"]),
    "avi": ("avi", ["-c:v", "mpeg4", "-q:v", "3"],
            ["-c:a", "libmp3lame", "-b:a", "192k"]),
    "mkv": ("mkv", ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"],
            ["-c:a", "libmp3lame", "-b:a", "192k"]),
}

# nomes (em pt-BR e en-US, é comum o Windows vir num idioma e o driver de
# som noutro) de dispositivos dshow que costumam ser "loopback" do que
# está tocando no sistema — não é um microfone, é o próprio áudio de
# saída sendo espelhado de volta como entrada. "Stereo Mix" é nativo da
# Realtek/maioria das placas de som, mas vem DESLIGADO por padrão no
# Windows; "VB-Audio"/"CABLE Output" é de um driver virtual grátis
# (VB-CABLE) pra quem não tem Stereo Mix disponível.
_PISTAS_AUDIO_SISTEMA = [
    "stereo mix", "mixagem estéreo", "mixagem estereo", "mistura estéreo",
    "what u hear", "wave out mix", "loopback", "vb-audio", "cable output",
    "virtual-audio-capturer",
]
_PISTAS_AUDIO_MICROFONE = ["microphone", "microfone", "mic ", "headset"]

# só uma gravação por vez — estado global do processo ffmpeg ativo
_gravacao = {"processo": None, "caminho": None, "inicio": None, "parar_overlay": None, "log_path": None}
_lock = threading.Lock()


def _listar_dispositivos_audio_dshow(ffmpeg):
    """Pergunta pro próprio ffmpeg quais dispositivos de ÁUDIO o dshow
    enxerga (mesma lista que aparece em Configurações de Som do Windows).
    Pra cada dispositivo, o ffmpeg lista o NOME amigável (ex: "Mixagem
    Estéreo (Realtek...)") e, na linha seguinte, um "Alternative name"
    — um identificador tipo "@device_cm_{...}\\wave_{...}", só ASCII/GUID,
    sem acento. Devolve [{"nome": ..., "alt": ... ou None}, ...].
    Lista vazia se o ffmpeg não respondeu nada reconhecível (não é erro
    fatal, só significa que a gravação vai seguir sem tentar casar um
    dispositivo)."""
    try:
        resultado = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=10,
            creationflags=_SEM_JANELA,
        )
    except Exception:
        return []

    saida = (resultado.stderr or "") + (resultado.stdout or "")
    dispositivos = []
    esperando_alt = False
    for linha in saida.splitlines():
        if "(audio)" in linha:
            m = re.search(r'"([^"]+)"', linha)
            if m:
                dispositivos.append({"nome": m.group(1), "alt": None})
                esperando_alt = True
            continue
        if "(video)" in linha:
            esperando_alt = False
            continue
        if esperando_alt and "Alternative name" in linha:
            m = re.search(r'"([^"]+)"', linha)
            if m and dispositivos:
                dispositivos[-1]["alt"] = m.group(1)
            esperando_alt = False
    return dispositivos


def _identificador_dispositivo(d):
    """Pra USAR o dispositivo no comando do ffmpeg, prefere sempre o
    'Alternative name' (GUID puro ASCII) em vez do nome amigável — nomes
    amigáveis com acento (ex: 'Mixagem Estéreo') podem chegar corrompidos
    aqui dependendo da codepage do console do Windows, e aí o dshow não
    reconhece o dispositivo e o ffmpeg morre com erro de argumento
    inválido. O Alternative name não tem esse problema (é só ASCII)."""
    return d["alt"] if d.get("alt") else d["nome"]


def _achar_dispositivo(dispositivos, pistas):
    for d in dispositivos:
        if any(p in d["nome"].lower() for p in pistas):
            return d
    return None


def _montar_entradas_audio(ffmpeg, audio):
    """Decide QUAIS dispositivos dshow entram na gravação, a partir do
    parâmetro `audio` de gravar_tela. Devolve (lista_de_identificadores,
    lista_de_nomes_legiveis, aviso). Os identificadores vão DIRETO pro
    comando do ffmpeg (Alternative name, se disponível); os nomes
    legíveis são só pra mensagem mostrada pro usuário. aviso é uma
    string pra avisar quando o pedido não pôde ser totalmente atendido
    (ex: pediu 'sistema' mas não achou Stereo Mix habilitado) — None se
    não há nada a avisar."""
    if audio == "nenhum":
        return [], [], None

    dispositivos = _listar_dispositivos_audio_dshow(ffmpeg)
    sistema = _achar_dispositivo(dispositivos, _PISTAS_AUDIO_SISTEMA)
    microfone = _achar_dispositivo(dispositivos, _PISTAS_AUDIO_MICROFONE)
    # fallback: nenhum nome bateu com as pistas conhecidas, mas existe
    # pelo menos 1 dispositivo de áudio — assume que é o microfone
    # (é o caso mais comum de só ter 1 dispositivo de captura).
    if not microfone and not sistema and dispositivos:
        microfone = dispositivos[0]

    def _empacotar(lista_d):
        return [_identificador_dispositivo(d) for d in lista_d], [d["nome"] for d in lista_d]

    if audio == "microfone":
        if not microfone:
            return [], [], ("Pedi pra gravar com áudio do microfone, mas não encontrei nenhum "
                             "dispositivo de captura de áudio no Windows. Gravando só o vídeo.")
        ids, nomes = _empacotar([microfone])
        return ids, nomes, None

    if audio == "sistema":
        if not sistema:
            return [], [], ("Pedi pra gravar o áudio do sistema, mas o Windows não tem 'Stereo "
                             "Mix' (ou equivalente) habilitado. Pra ligar: clique com o botão "
                             "direito no ícone de som na barra de tarefas → Sons → aba "
                             "Gravação → botão direito num espaço vazio → 'Mostrar "
                             "Dispositivos Desabilitados' → habilite 'Stereo Mix'/'Mixagem "
                             "Estéreo'. Sem isso, gravando só o vídeo.")
        ids, nomes = _empacotar([sistema])
        return ids, nomes, None

    if audio == "ambos":
        achados = [d for d in (microfone, sistema) if d]
        if not achados:
            return [], [], ("Pedi pra gravar com áudio, mas não encontrei nem microfone nem "
                             "Stereo Mix disponível no Windows. Gravando só o vídeo.")
        ids, nomes = _empacotar(achados)
        if len(achados) == 1:
            return ids, nomes, ("Só encontrei 1 fonte de áudio disponível "
                                 f"('{nomes[0]}') — gravando só com ela.")
        return ids, nomes, None

    # audio == "auto" (padrão): tenta microfone + sistema juntos; se só
    # achar 1 dos dois, usa esse; se não achar nenhum, segue sem áudio
    # mas AVISA (silenciosamente gravar sem som numa aula seria pior).
    achados = [d for d in (microfone, sistema) if d]
    if not achados:
        return [], [], ("Não encontrei microfone nem áudio do sistema (Stereo Mix) disponível "
                         "no Windows — gravando só o vídeo, sem som. Se quiser áudio, habilite "
                         "o Stereo Mix (Configurações de Som → Gravação) ou conecte um "
                         "microfone.")
    ids, nomes = _empacotar(achados)
    return ids, nomes, None


def gravar_tela(formato="mp4", audio="auto"):
    """Deixa o usuário selecionar uma área da tela com o mouse e começa a
    GRAVAR essa área em vídeo, sem limite de duração — grava até chamar
    parar_gravacao_tela(). formato: mp4/mov (padrão redes sociais,
    H.264), webm (padrão web/YouTube, VP9), avi (clássico), mkv
    (contêiner flexível, alta qualidade). audio: 'auto' (padrão — tenta
    microfone E o áudio que está tocando no sistema, do jeito que der),
    'microfone', 'sistema' (só o que está tocando no PC, útil pra
    gravar uma aula em vídeo sem precisar narrar), 'ambos' (força os
    dois, erro se faltar algum) ou 'nenhum' (sem áudio, comportamento
    antigo)."""
    formato = (formato or "mp4").strip().lower()
    if formato not in _FORMATOS_VIDEO:
        return f"ERRO: formato '{formato}' não suportado. Use: {', '.join(_FORMATOS_VIDEO)}."

    audio = (audio or "auto").strip().lower()
    if audio not in ("auto", "microfone", "sistema", "ambos", "nenhum"):
        return f"ERRO: audio '{audio}' não reconhecido. Use: auto, microfone, sistema, ambos ou nenhum."

    with _lock:
        if _gravacao["processo"] is not None:
            return ("ERRO: já existe uma gravação em andamento "
                    f"({os.path.basename(_gravacao['caminho'])}). "
                    "Chame parar_gravacao_tela primeiro.")

    ffmpeg = _achar_ffmpeg()
    if not ffmpeg:
        return ("ERRO: ffmpeg não encontrado. Configure FFMPEG_PATH no .env "
                "(veja o comentário sobre ffmpeg em config.py).")

    bbox = _selecionar_area()
    if not bbox:
        return "Seleção cancelada pelo usuário (nenhuma área foi escolhida)."

    x0, y0, x1, y1 = bbox
    largura, altura = x1 - x0, y1 - y0
    # gdigrab exige largura/altura PAR (limitação do codec de vídeo)
    largura -= largura % 2
    altura -= altura % 2

    ext, codec_video, codec_audio = _FORMATOS_VIDEO[formato]
    nome = f"gravacao_{time.strftime('%Y%m%d_%H%M%S')}.{ext}"
    caminho = os.path.join(config.WORKSPACE, nome)

    dispositivos_audio, nomes_audio, aviso_audio = _montar_entradas_audio(ffmpeg, audio)
    if audio in ("microfone", "sistema", "ambos") and not dispositivos_audio:
        # esses 3 modos são pedido EXPLÍCITO de áudio — se não achou
        # dispositivo nenhum, é melhor avisar e não gravar do que gravar
        # silenciosamente sem o que foi pedido.
        return f"ERRO: {aviso_audio}"

    comando = [ffmpeg, "-y",
               "-f", "gdigrab", "-framerate", "30",
               "-offset_x", str(x0), "-offset_y", str(y0),
               "-video_size", f"{largura}x{altura}", "-i", "desktop"]

    for dispositivo in dispositivos_audio:
        comando += ["-f", "dshow", "-i", f"audio={dispositivo}"]

    # codec_video já vem com "-c:v" como primeiro item (ver
    # _FORMATOS_VIDEO) — NÃO prefixar de novo aqui, senão vira "-c:v
    # -c:v libx264 ...", o ffmpeg lê o 2º "-c:v" como se fosse o VALOR
    # do primeiro, e "libx264" sobra solto e é interpretado como nome de
    # ARQUIVO DE SAÍDA (daí o erro "Unable to choose an output format
    # for 'libx264'"). Foi exatamente esse bug que quebrou a gravação.
    comando += codec_video

    if len(dispositivos_audio) == 2:
        # dois microfones/fontes -> mistura os dois num canal só de
        # áudio (senão o container ficaria com 2 trilhas separadas, e a
        # maioria dos players só toca a primeira)
        comando += [
            "-filter_complex", "[1:a][2:a]amix=inputs=2:duration=longest[aout]",
            "-map", "0:v", "-map", "[aout]",
            *codec_audio,
        ]
    elif len(dispositivos_audio) == 1:
        comando += ["-map", "0:v", "-map", "1:a", *codec_audio]
    # sem dispositivos de áudio -> segue só com o vídeo (comando de
    # antes desta mudança), nem precisa de -map explícito

    comando.append(caminho)

    # stderr do ffmpeg vai pra um arquivo de log (não DEVNULL) — se a
    # gravação falhar (device de áudio inválido, argumento errado etc.)
    # a gente precisa conseguir LER o motivo em vez de só saber "deu
    # errado". O ffmpeg é bem verboso em erro, então isso é o suficiente
    # pra diagnosticar sem precisar reproduzir o problema.
    log_path = caminho + ".log"
    try:
        log_arquivo = open(log_path, "wb")
    except Exception:
        log_arquivo = subprocess.DEVNULL

    try:
        processo = subprocess.Popen(
            comando, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=log_arquivo,
            creationflags=_SEM_JANELA,
        )
    except Exception as e:
        return f"ERRO ao iniciar gravação: {type(e).__name__}: {e}"
    finally:
        # o Popen já duplicou o handle pro processo filho — fecha o
        # nosso lado aqui (senão vaza descritor de arquivo aberto)
        if log_arquivo is not subprocess.DEVNULL:
            log_arquivo.close()

    inicio_ts = time.time()

    # Alguns erros (device de áudio inexistente, argumento inválido) o
    # ffmpeg detecta e MORRE nos primeiros milissegundos — dá pra pegar
    # isso aqui e avisar na hora, em vez do usuário só descobrir quando
    # for parar a gravação e o vídeo estiver vazio. 1.5s é tempo de sobra
    # pro ffmpeg validar os inputs mas pouco o bastante pra não atrasar
    # quem gravou certinho.
    time.sleep(1.5)
    if processo.poll() is not None:
        detalhe = _ler_fim_do_log(log_path)
        return ("ERRO: o ffmpeg encerrou sozinho logo ao iniciar (código "
                f"{processo.returncode}) — a gravação NÃO começou.\n{detalhe}")

    # Overlay de contorno + cronômetro (ver comentário lá em cima). Roda
    # numa thread separada, própria, porque precisa da SUA mainloop do
    # Tkinter — não dá pra reaproveitar a do seletor de área, que já
    # fechou. Se o Tkinter/Windows não colaborar, o pior caso é ela não
    # aparecer; a gravação em si não depende disso.
    parar_overlay = threading.Event()
    threading.Thread(
        target=_rodar_overlay_gravacao,
        args=(bbox, parar_overlay, inicio_ts),
        daemon=True,
    ).start()

    with _lock:
        _gravacao["processo"] = processo
        _gravacao["caminho"] = caminho
        _gravacao["inicio"] = inicio_ts
        _gravacao["parar_overlay"] = parar_overlay
        _gravacao["log_path"] = log_path

    if nomes_audio:
        info_audio = f"com áudio ({', '.join(nomes_audio)})"
    else:
        info_audio = "sem áudio"
    msg = (f"Gravação iniciada ({largura}x{altura}px, formato {formato.upper()}, {info_audio}). "
           "Sem limite de tempo — quando quiser parar, é só pedir "
           "('para a gravação') que eu chamo parar_gravacao_tela.")
    if aviso_audio:
        msg += f"\n\n⚠️ {aviso_audio}"
    return msg


def parar_gravacao_tela():
    """Encerra a gravação de tela em andamento (iniciada por gravar_tela)
    e devolve o vídeo pronto."""
    with _lock:
        processo = _gravacao["processo"]
        caminho = _gravacao["caminho"]
        inicio = _gravacao["inicio"]
        parar_overlay = _gravacao["parar_overlay"]
        log_path = _gravacao["log_path"]

    if processo is None:
        return "ERRO: não há nenhuma gravação em andamento."

    # sinaliza a thread do overlay pra se fechar sozinha (ela mesma chama
    # janela.destroy() na próxima checagem, dentro da sua mainloop) —
    # feito ANTES de esperar o ffmpeg fechar, pra sumir da tela o quanto
    # antes.
    if parar_overlay is not None:
        parar_overlay.set()

    duracao = time.time() - inicio

    try:
        # 'q' via stdin é o jeito CERTO de parar o ffmpeg — ele fecha o
        # arquivo de vídeo direitinho (finaliza o container). Matar o
        # processo (kill/terminate) corrompe o arquivo na maioria dos
        # formatos, então nunca faça isso aqui.
        processo.stdin.write(b"q")
        processo.stdin.flush()
        processo.wait(timeout=15)
    except Exception:
        try:
            processo.terminate()
        except Exception:
            pass

    with _lock:
        _gravacao["processo"] = None
        _gravacao["caminho"] = None
        _gravacao["inicio"] = None
        _gravacao["parar_overlay"] = None
        _gravacao["log_path"] = None

    if not os.path.exists(caminho) or os.path.getsize(caminho) == 0:
        detalhe = _ler_fim_do_log(log_path) if log_path else ""
        return ("ERRO: a gravação parou mas o arquivo de vídeo ficou vazio ou não foi "
                f"criado.\n{detalhe}")

    # deu tudo certo — não precisa mais do log, some com ele pra não
    # acumular lixo no workspace
    if log_path and os.path.exists(log_path):
        try:
            os.remove(log_path)
        except Exception:
            pass

    nome = os.path.basename(caminho)
    return f"VIDEO_GERADO: {nome} (gravação de {duracao:.0f}s)"


# ── descrição para o modelo ──

TOOLS = [
    {"type": "function", "function": {
        "name": "tirar_print",
        "description": "Deixa o USUÁRIO selecionar com o mouse (arrastando) qualquer área da tela DELE (não de uma página web) e tira um print dessa área, mostrando no chat. Use quando o usuário pedir para 'tirar um print', 'capturar a tela', 'printar minha tela' etc — SEM citar nenhum site (nesse caso use capturar_tela do navegador).",
        "parameters": {"type": "object", "properties": {
            "formato": {"type": "string", "enum": ["png", "jpg", "jpeg", "webp"],
                        "description": "Formato da imagem (padrão: png)"}}}}},
    {"type": "function", "function": {
        "name": "gravar_tela",
        "description": "Deixa o USUÁRIO selecionar com o mouse qualquer área da tela e começa a GRAVAR um vídeo dessa área, sem limite de duração — grava até o usuário pedir para parar. Use quando o usuário pedir para 'gravar a tela', 'gravar um vídeo da tela', 'gravar isso que eu vou fazer', 'gravar essa aula/vídeo', 'gravar com áudio' etc.",
        "parameters": {"type": "object", "properties": {
            "formato": {"type": "string", "enum": ["mp4", "mov", "webm", "avi", "mkv"],
                        "description": "Formato do vídeo (padrão: mp4, o mais compatível com redes sociais)"},
            "audio": {"type": "string", "enum": ["auto", "microfone", "sistema", "ambos", "nenhum"],
                      "description": (
                          "Qual áudio capturar junto do vídeo. 'auto' (padrão): tenta "
                          "microfone e o áudio que está tocando no PC, usa o que "
                          "estiver disponível. 'microfone': só a voz do usuário. "
                          "'sistema': só o que está tocando no PC (ex: gravar uma "
                          "aula/vídeo/reunião sem narrar por cima — use isso quando o "
                          "usuário disser algo como 'quero poder ouvir depois' sobre "
                          "uma aula/vídeo que está passando na tela). 'ambos': exige "
                          "os dois, dá erro se faltar um. 'nenhum': vídeo mudo."
                      )}}}}},
    {"type": "function", "function": {
        "name": "parar_gravacao_tela",
        "description": "Encerra a gravação de tela em andamento (iniciada por gravar_tela) e entrega o vídeo pronto no chat. Use quando o usuário pedir para 'parar a gravação', 'terminar o vídeo' etc.",
        "parameters": {"type": "object", "properties": {}}}},
]

FERRAMENTAS = {
    "tirar_print": tirar_print,
    "gravar_tela": gravar_tela,
    "parar_gravacao_tela": parar_gravacao_tela,
}
