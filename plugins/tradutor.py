# 🔌 Plugin: Tradutor Profissional — motor dedicado + OCR + captura de tela
#
# Combina o melhor do editor.py (Google Translate API gratuita) com o
# TraduniviA (prompts especializados, glossário, cache, formalidade) e
# adiciona captura de tela → OCR → tradução → overlay na tela com opção
# de salvar em vários formatos.
#
# Motores disponíveis:
#   • google   — API gratuita (translate.googleapis.com), rápida, sem custo
#   • local    — modelo Ollama ativo, com prompts especializados por idioma
#
# Dependências obrigatórias: nenhuma (urllib já vem com Python)
# Dependências opcionais:
#   pip install Pillow pytesseract python-docx fpdf2
#   (Pillow já está no Jarvis; pytesseract precisa do Tesseract instalado)

import ctypes
import hashlib
import json
import os
import re
import sys
import time
import threading
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

# ── imports do Jarvis ─────────────────────────────────────────────────
import config

try:
    import llm
    HAS_LLM = True
except ImportError:
    HAS_LLM = False

# ── imports opcionais ─────────────────────────────────────────────────
try:
    from PIL import Image, ImageGrab
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import pytesseract
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

try:
    from docx import Document as DocxDocument
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

try:
    from fpdf import FPDF
    HAS_FPDF = True
except ImportError:
    HAS_FPDF = False

_SEM_JANELA = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)


# =====================================================================
# CONSTANTES E CONFIGURAÇÃO
# =====================================================================

# Pasta de dados do plugin (dentro do workspace do Jarvis)
_DADOS_DIR = Path(config.BASE_DIR) / "tradutor_data"
_DADOS_DIR.mkdir(exist_ok=True)

_CACHE_FILE = _DADOS_DIR / "cache_traducoes.json"
_GLOSSARIO_FILE = _DADOS_DIR / "glossario.json"
_CONFIG_FILE = _DADOS_DIR / "tradutor_config.json"

# Google Translate — mesma API gratuita do editor.py
_GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
_GOOGLE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}

# Idiomas suportados (código Google Translate → nome + bandeira)
IDIOMAS = {
    "pt":    {"nome": "Português (BR)", "bandeira": "🇧🇷", "tesseract": "por"},
    "en":    {"nome": "English",        "bandeira": "🇺🇸", "tesseract": "eng"},
    "es":    {"nome": "Español",        "bandeira": "🇪🇸", "tesseract": "spa"},
    "fr":    {"nome": "Français",       "bandeira": "🇫🇷", "tesseract": "fra"},
    "de":    {"nome": "Deutsch",        "bandeira": "🇩🇪", "tesseract": "deu"},
    "it":    {"nome": "Italiano",       "bandeira": "🇮🇹", "tesseract": "ita"},
    "ja":    {"nome": "日本語",          "bandeira": "🇯🇵", "tesseract": "jpn"},
    "zh":    {"nome": "中文",            "bandeira": "🇨🇳", "tesseract": "chi_sim"},
    "ko":    {"nome": "한국어",          "bandeira": "🇰🇷", "tesseract": "kor"},
    "ru":    {"nome": "Русский",        "bandeira": "🇷🇺", "tesseract": "rus"},
    "ar":    {"nome": "العربية",        "bandeira": "🇸🇦", "tesseract": "ara"},
    "hi":    {"nome": "हिन्दी",          "bandeira": "🇮🇳", "tesseract": "hin"},
    "tr":    {"nome": "Türkçe",         "bandeira": "🇹🇷", "tesseract": "tur"},
    "pl":    {"nome": "Polski",         "bandeira": "🇵🇱", "tesseract": "pol"},
    "nl":    {"nome": "Nederlands",     "bandeira": "🇳🇱", "tesseract": "nld"},
    "sv":    {"nome": "Svenska",        "bandeira": "🇸🇪", "tesseract": "swe"},
    "uk":    {"nome": "Українська",     "bandeira": "🇺🇦", "tesseract": "ukr"},
    "vi":    {"nome": "Tiếng Việt",     "bandeira": "🇻🇳", "tesseract": "vie"},
    "th":    {"nome": "ไทย",            "bandeira": "🇹🇭", "tesseract": "tha"},
    "id":    {"nome": "Bahasa Indonesia","bandeira": "🇮🇩", "tesseract": "ind"},
    "he":    {"nome": "עברית",          "bandeira": "🇮🇱", "tesseract": "heb"},
    "cs":    {"nome": "Čeština",        "bandeira": "🇨🇿", "tesseract": "ces"},
    "ro":    {"nome": "Română",         "bandeira": "🇷🇴", "tesseract": "ron"},
    "da":    {"nome": "Dansk",          "bandeira": "🇩🇰", "tesseract": "dan"},
    "auto":  {"nome": "Auto-detectar",  "bandeira": "🔍", "tesseract": ""},
}

# Prompts especializados por par de idiomas (para motor "local"/Ollama)
_PROMPTS_ESPECIALIZADOS = {
    "pt->en": (
        "Você é um tradutor profissional português-inglês. Traduza o "
        "texto de forma natural e nativa em inglês americano. Evite "
        "traduções literais — adapte expressões idiomáticas, gírias e "
        "referências culturais para equivalentes naturais em inglês."
    ),
    "en->pt": (
        "Você é um tradutor profissional inglês-português. Traduza para "
        "português brasileiro natural e fluente. Adapte expressões "
        "idiomáticas e referências culturais. Use 'você' (não 'tu')."
    ),
    "es->pt": (
        "Você é um tradutor profissional espanhol-português. Traduza "
        "para português brasileiro natural. Cuidado com falsos cognatos "
        "(embarazada≠embaraçada, exquisito≠esquisito). Adapte o registro "
        "de formalidade ao padrão brasileiro."
    ),
    "pt->es": (
        "Eres un traductor profesional portugués-español. Traduce al "
        "español neutro (latinoamericano). Cuidado con falsos cognatos "
        "(aceitar≠aceptar, escritório≠escritorio). Adapta expresiones "
        "brasileñas al español natural."
    ),
    "ja->en": (
        "You are a professional Japanese-English translator. Preserve "
        "honorifics only when culturally relevant. Convert keigo levels "
        "to appropriate English register. Handle untranslatable concepts "
        "(仕方がない, 空気を読む) with natural English equivalents."
    ),
    "zh->en": (
        "You are a professional Chinese-English translator. Handle "
        "chengyu (成语) and classical references with context-appropriate "
        "English equivalents. Preserve the original's register and tone."
    ),
}

# Prompt genérico (quando não há especializado para o par)
_PROMPT_GENERICO = (
    "You are a professional translator. Translate the following text "
    "from {origem_nome} to {destino_nome}. Produce a natural, fluent "
    "translation — not word-for-word. Preserve formatting, paragraph "
    "breaks, and special characters. Keep proper nouns, brand names, "
    "and technical terms in their original form when appropriate."
)

_FORMALIDADE = {
    "formal": " Use formal register throughout.",
    "informal": " Use informal, conversational register.",
    "neutro": "",
}


# =====================================================================
# CACHE DE TRADUÇÕES (memória de tradução)
# =====================================================================

class CacheTraducao:
    """Cache persistente em JSON — salva traduções para evitar repetição.
    Chave: hash(texto+origem+destino+motor). Armazena até 5000 entradas
    (LRU: remove as mais antigas quando cheio)."""

    MAX_ENTRADAS = 5000

    def __init__(self, caminho=_CACHE_FILE):
        self._caminho = Path(caminho)
        self._dados = {}
        self._carregar()

    def _carregar(self):
        try:
            if self._caminho.exists():
                self._dados = json.loads(self._caminho.read_text("utf-8"))
        except Exception:
            self._dados = {}

    def _salvar(self):
        try:
            self._caminho.write_text(
                json.dumps(self._dados, ensure_ascii=False, indent=1),
                encoding="utf-8"
            )
        except Exception:
            pass

    def _chave(self, texto, origem, destino, motor):
        raw = f"{texto}|{origem}|{destino}|{motor}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def obter(self, texto, origem, destino, motor):
        chave = self._chave(texto, origem, destino, motor)
        entrada = self._dados.get(chave)
        if entrada:
            entrada["acessos"] = entrada.get("acessos", 0) + 1
            entrada["ultimo_acesso"] = time.time()
            return entrada["traducao"]
        return None

    def salvar(self, texto, origem, destino, motor, traducao):
        chave = self._chave(texto, origem, destino, motor)
        self._dados[chave] = {
            "original": texto[:200],  # amostra para busca
            "traducao": traducao,
            "origem": origem,
            "destino": destino,
            "motor": motor,
            "criado": time.time(),
            "ultimo_acesso": time.time(),
            "acessos": 1,
        }
        # LRU: remove mais antigos se excedeu limite
        if len(self._dados) > self.MAX_ENTRADAS:
            ordenados = sorted(
                self._dados.items(),
                key=lambda x: x[1].get("ultimo_acesso", 0)
            )
            for chave_antiga, _ in ordenados[:len(self._dados) - self.MAX_ENTRADAS]:
                del self._dados[chave_antiga]
        self._salvar()

    def buscar(self, termo, limite=10):
        """Busca na memória de tradução por termo (no original ou tradução)."""
        termo_lower = termo.lower()
        resultados = []
        for entrada in self._dados.values():
            if (termo_lower in entrada.get("original", "").lower() or
                    termo_lower in entrada.get("traducao", "").lower()):
                resultados.append(entrada)
                if len(resultados) >= limite:
                    break
        return resultados

    def estatisticas(self):
        return {
            "total_entradas": len(self._dados),
            "max_entradas": self.MAX_ENTRADAS,
        }

    def limpar(self):
        self._dados = {}
        self._salvar()
        return "Cache de traduções limpo."


_cache = CacheTraducao()


# =====================================================================
# GLOSSÁRIO CUSTOMIZÁVEL
# =====================================================================

class Glossario:
    """Glossário de termos — substitui ANTES de traduzir (protege termos
    técnicos, nomes de produto, etc.) e restaura DEPOIS."""

    def __init__(self, caminho=_GLOSSARIO_FILE):
        self._caminho = Path(caminho)
        self._termos = {}  # {idioma_destino: {original: tradução}}
        self._carregar()

    def _carregar(self):
        try:
            if self._caminho.exists():
                self._termos = json.loads(self._caminho.read_text("utf-8"))
        except Exception:
            self._termos = {}

    def _salvar(self):
        try:
            self._caminho.write_text(
                json.dumps(self._termos, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def adicionar(self, termo_original, traducao, idioma_destino="*"):
        """Adiciona um termo ao glossário. idioma_destino='*' aplica a todos."""
        if idioma_destino not in self._termos:
            self._termos[idioma_destino] = {}
        self._termos[idioma_destino][termo_original] = traducao
        self._salvar()
        return f"Glossário: '{termo_original}' → '{traducao}' ({idioma_destino})"

    def remover(self, termo_original, idioma_destino="*"):
        if idioma_destino in self._termos:
            if termo_original in self._termos[idioma_destino]:
                del self._termos[idioma_destino][termo_original]
                self._salvar()
                return f"Glossário: '{termo_original}' removido de ({idioma_destino})"
        return f"Termo '{termo_original}' não encontrado no glossário."

    def listar(self, idioma_destino=None):
        if not self._termos:
            return "Glossário vazio."
        linhas = ["📖 Glossário de Tradução:"]
        for idioma, termos in self._termos.items():
            if idioma_destino and idioma != idioma_destino and idioma != "*":
                continue
            flag = IDIOMAS.get(idioma, {}).get("bandeira", idioma)
            linhas.append(f"\n  [{flag} {idioma}]")
            for orig, trad in termos.items():
                linhas.append(f"    {orig} → {trad}")
        return "\n".join(linhas)

    def aplicar(self, texto, idioma_destino):
        """Substitui termos do glossário no texto ANTES de traduzir.
        Usa placeholders numerados para proteger os termos."""
        termos_aplicaveis = {}
        # termos globais (*) + termos do idioma específico
        termos_aplicaveis.update(self._termos.get("*", {}))
        termos_aplicaveis.update(self._termos.get(idioma_destino, {}))

        if not termos_aplicaveis:
            return texto, {}

        mapeamento = {}  # placeholder → tradução final
        for i, (original, traducao) in enumerate(termos_aplicaveis.items()):
            placeholder = f"⟪GLOSS{i:03d}⟫"
            # case-insensitive replace preservando fronteiras de palavra
            padrao = re.compile(re.escape(original), re.IGNORECASE)
            if padrao.search(texto):
                texto = padrao.sub(placeholder, texto)
                mapeamento[placeholder] = traducao

        return texto, mapeamento

    def restaurar(self, texto, mapeamento):
        """Restaura os placeholders com as traduções do glossário."""
        for placeholder, traducao in mapeamento.items():
            texto = texto.replace(placeholder, traducao)
        return texto


_glossario = Glossario()


# =====================================================================
# MOTOR: GOOGLE TRANSLATE (API GRATUITA)
# =====================================================================

def _google_translate(texto, origem="auto", destino="pt"):
    """Traduz via API gratuita do Google Translate.
    Mesmo método do editor.py — funciona de IP residencial."""
    if not texto or not texto.strip():
        return ""

    # Divide textos muito longos (limite ~5000 chars por request)
    if len(texto) > 4500:
        partes = _dividir_texto(texto, 4500)
        resultados = [_google_translate(p, origem, destino) for p in partes]
        return "\n".join(resultados)

    params = urllib.parse.urlencode({
        "client": "gtx",
        "sl": origem,
        "tl": destino,
        "dt": "t",
        "q": texto,
    })
    url = f"{_GOOGLE_URL}?{params}"

    req = urllib.request.Request(url, headers=_GOOGLE_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            dados = json.loads(resp.read().decode("utf-8"))
        # A resposta é uma lista aninhada: [[["tradução","original",...],...],...,idioma_detectado]
        if dados and dados[0]:
            traducao = "".join(
                seg[0] for seg in dados[0] if seg and seg[0]
            )
            return traducao.strip()
        return texto
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return f"[ERRO: Google Translate bloqueou temporariamente (429). Tente novamente em alguns minutos ou use motor='local'.]"
        return f"[ERRO Google Translate: HTTP {e.code}]"
    except Exception as e:
        return f"[ERRO Google Translate: {e}]"


def _dividir_texto(texto, max_chars):
    """Divide texto em partes respeitando parágrafos e frases."""
    partes = []
    atual = ""
    for paragrafo in texto.split("\n"):
        if len(atual) + len(paragrafo) + 1 > max_chars and atual:
            partes.append(atual)
            atual = paragrafo
        else:
            atual = atual + "\n" + paragrafo if atual else paragrafo
    if atual:
        partes.append(atual)
    return partes


# =====================================================================
# MOTOR: OLLAMA LOCAL (com prompts especializados)
# =====================================================================

def _ollama_translate(texto, origem="auto", destino="pt", formalidade="formal"):
    """Traduz via modelo Ollama local com prompt especializado."""
    if not HAS_LLM:
        return "[ERRO: módulo llm não disponível]"
    if not texto or not texto.strip():
        return ""

    origem_nome = IDIOMAS.get(origem, {}).get("nome", origem)
    destino_nome = IDIOMAS.get(destino, {}).get("nome", destino)

    # Busca prompt especializado para o par de idiomas
    chave_par = f"{origem}->{destino}"
    prompt_base = _PROMPTS_ESPECIALIZADOS.get(chave_par)

    if not prompt_base:
        prompt_base = _PROMPT_GENERICO.format(
            origem_nome=origem_nome, destino_nome=destino_nome
        )

    # Adiciona instrução de formalidade
    prompt_base += _FORMALIDADE.get(formalidade, "")

    prompt_completo = (
        f"{prompt_base}\n\n"
        f"Regras:\n"
        f"- Devolva SOMENTE o texto traduzido, sem comentários.\n"
        f"- Mantenha formatação, parágrafos e caracteres especiais.\n"
        f"- Mantenha termos técnicos e nomes próprios na forma original.\n"
        f"- Preserve quebras de linha.\n\n"
        f"Texto:\n{texto}"
    )

    try:
        resposta = llm.chat(
            [{"role": "user", "content": prompt_completo}],
            opcoes_extra={"temperature": 0.3, "top_p": 0.9}
        )
        resultado = (resposta.get("content") or "").strip()

        # Limpa thinking tags se o modelo usar
        resultado = re.sub(r"<think>.*?</think>", "", resultado, flags=re.DOTALL).strip()

        return resultado if resultado else texto
    except Exception as e:
        return f"[ERRO Ollama: {e}]"


# =====================================================================
# FUNÇÃO PRINCIPAL DE TRADUÇÃO (unifica motores + glossário + cache)
# =====================================================================

def traduzir_texto(texto, origem="auto", destino="en", motor="google",
                   formalidade="formal"):
    """Traduz texto usando o motor escolhido (google ou local).
    Aplica glossário, consulta cache, e salva resultado no cache.

    Args:
        texto: Texto a traduzir
        origem: Código do idioma de origem (ou 'auto')
        destino: Código do idioma de destino
        motor: 'google' (API gratuita) ou 'local' (Ollama)
        formalidade: 'formal', 'informal' ou 'neutro'
    """
    if not texto or not texto.strip():
        return "Nenhum texto para traduzir."

    origem = (origem or "auto").strip().lower()
    destino = (destino or "en").strip().lower()
    motor = (motor or "google").strip().lower()
    formalidade = (formalidade or "formal").strip().lower()

    # Validações
    if destino not in IDIOMAS and destino != "auto":
        idiomas_validos = ", ".join(f"{k} ({v['nome']})" for k, v in IDIOMAS.items() if k != "auto")
        return f"ERRO: idioma destino '{destino}' não reconhecido. Disponíveis: {idiomas_validos}"
    if motor not in ("google", "local"):
        return "ERRO: motor deve ser 'google' ou 'local'."

    # 1. Consulta cache
    cached = _cache.obter(texto, origem, destino, motor)
    if cached:
        return f"{cached}\n\n📋 (do cache — tradução anterior reutilizada)"

    # 2. Aplica glossário (protege termos)
    texto_preparado, mapeamento_gloss = _glossario.aplicar(texto, destino)

    # 3. Traduz
    if motor == "google":
        traducao = _google_translate(texto_preparado, origem, destino)
    else:
        traducao = _ollama_translate(texto_preparado, origem, destino, formalidade)

    # Verifica se houve erro
    if traducao.startswith("[ERRO"):
        return traducao

    # 4. Restaura termos do glossário
    if mapeamento_gloss:
        traducao = _glossario.restaurar(traducao, mapeamento_gloss)

    # 5. Salva no cache
    _cache.salvar(texto, origem, destino, motor, traducao)

    # Info do motor usado
    if motor == "google":
        info = "🌐 Google Translate"
    else:
        modelo = llm.modelo_atual() if HAS_LLM else "?"
        info = f"🤖 Ollama ({modelo})"

    bandeira_orig = IDIOMAS.get(origem, {}).get("bandeira", "")
    bandeira_dest = IDIOMAS.get(destino, {}).get("bandeira", "")
    nome_orig = IDIOMAS.get(origem, {}).get("nome", origem)
    nome_dest = IDIOMAS.get(destino, {}).get("nome", destino)

    return (
        f"{traducao}\n\n"
        f"── {info} | {bandeira_orig} {nome_orig} → {bandeira_dest} {nome_dest} "
        f"| {formalidade}"
    )


# =====================================================================
# OCR — EXTRAIR TEXTO DE IMAGEM
# =====================================================================

def _ocr_imagem(img, idioma_origem="auto"):
    """Extrai texto de uma imagem PIL.
    Tenta pytesseract primeiro; fallback para Ollama vision."""
    if HAS_TESSERACT:
        # Monta string de idiomas para Tesseract
        langs = []
        if idioma_origem != "auto" and idioma_origem in IDIOMAS:
            tess_lang = IDIOMAS[idioma_origem].get("tesseract", "")
            if tess_lang:
                langs.append(tess_lang)
        if not langs:
            langs = ["por", "eng", "spa"]  # padrão multi-idioma
        lang_str = "+".join(langs)

        try:
            texto = pytesseract.image_to_string(img, lang=lang_str)
            if texto and texto.strip():
                return texto.strip()
        except Exception:
            pass  # cai pro fallback

    # Fallback: Ollama vision (llava/bakllava)
    if HAS_LLM and HAS_PIL:
        try:
            import base64
            import io
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

            resposta = llm.chat([{
                "role": "user",
                "content": "Extract ALL text from this image. Return ONLY the "
                           "extracted text, nothing else. Preserve line breaks "
                           "and formatting.",
                "images": [img_b64],
            }])
            resultado = (resposta.get("content") or "").strip()
            if resultado:
                return resultado
        except Exception:
            pass

    return ""


# =====================================================================
# CAPTURA DE TELA → OCR → TRADUÇÃO (com overlay)
# =====================================================================

def _area_virtual():
    """Retorna (x, y, largura, altura) cobrindo todos os monitores."""
    try:
        u32 = ctypes.windll.user32
        x = u32.GetSystemMetrics(76)
        y = u32.GetSystemMetrics(77)
        w = u32.GetSystemMetrics(78)
        h = u32.GetSystemMetrics(79)
        return x, y, w, h
    except Exception:
        return 0, 0, 1920, 1080


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


def _selecionar_area_traducao():
    """Overlay escurecido — usuário arrasta para selecionar área.
    Idêntico ao captura_tela.py mas com texto diferente."""
    try:
        import tkinter as tk
    except ImportError:
        return None

    vx, vy, vw, vh = _area_virtual()
    resultado = {"bbox": None}

    janela = tk.Tk()
    janela.overrideredirect(True)
    janela.attributes("-topmost", True)
    janela.attributes("-alpha", 0.35)
    janela.geometry(f"{vw}x{vh}+{vx}+{vy}")
    janela.configure(bg="black", cursor="cross")

    canvas = tk.Canvas(janela, bg="black", highlightthickness=0, cursor="cross")
    canvas.pack(fill="both", expand=True)
    canvas.create_text(
        vw // 2, 30, fill="#06b6d4",
        font=("Segoe UI", 14, "bold"),
        text="🔍 Selecione a área para SCANNER/TRADUÇÃO — Esc para cancelar"
    )

    estado = {"x0": None, "y0": None, "rect": None}

    def ao_pressionar(e):
        estado["x0"], estado["y0"] = e.x, e.y
        if estado["rect"]:
            canvas.delete(estado["rect"])
        estado["rect"] = canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="#7c3aed", width=2
        )

    def ao_arrastar(e):
        if estado["rect"]:
            canvas.coords(estado["rect"], estado["x0"], estado["y0"], e.x, e.y)

    def ao_soltar(e):
        esq, dir_ = sorted((estado["x0"], e.x))
        top, bot = sorted((estado["y0"], e.y))
        if dir_ - esq > 4 and bot - top > 4:
            resultado["bbox"] = (vx + esq, vy + top, vx + dir_, vy + bot)
        janela.destroy()

    def cancelar(e=None):
        resultado["bbox"] = None
        janela.destroy()

    canvas.bind("<ButtonPress-1>", ao_pressionar)
    canvas.bind("<B1-Motion>", ao_arrastar)
    canvas.bind("<ButtonRelease-1>", ao_soltar)
    janela.bind("<Escape>", cancelar)

    # ⚠️ safety net: Tkinter em thread não-main pode travar o loop de
    # eventos. 30s é mais do que o suficiente pra selecionar uma área.
    _agendamento_destruicao(janela, 30000)

    janela.focus_force()
    janela.mainloop()
    return resultado["bbox"]


def _mostrar_overlay_traducao(texto_original, texto_traduzido, bbox):
    """Mostra uma janela overlay com a tradução sobre a área selecionada.
    Janela semitransparente, arrastável, com botão de fechar."""
    try:
        import tkinter as tk
        from tkinter import scrolledtext
    except ImportError:
        return

    def _criar():
        x0, y0, x1, y1 = bbox
        largura = max(x1 - x0, 400)
        altura = max(y1 - y0, 200)

        janela = tk.Tk()
        janela.title("TraduEasy — Tradução")
        janela.overrideredirect(True)
        janela.attributes("-topmost", True)
        janela.geometry(f"{largura}x{altura}+{x0}+{y0}")
        janela.configure(bg="#0d1226")

        # Barra de título customizada (arrastável)
        barra = tk.Frame(janela, bg="#7c3aed", height=28)
        barra.pack(fill="x")
        barra.pack_propagate(False)

        lbl_titulo = tk.Label(
            barra, text="🔍 Tradução", bg="#7c3aed", fg="white",
            font=("Segoe UI", 10, "bold")
        )
        lbl_titulo.pack(side="left", padx=8)

        btn_fechar = tk.Label(
            barra, text="✕", bg="#7c3aed", fg="white",
            font=("Segoe UI", 12, "bold"), cursor="hand2"
        )
        btn_fechar.pack(side="right", padx=8)
        btn_fechar.bind("<Button-1>", lambda e: janela.destroy())

        # ⚠️ safety net: Tkinter em thread não-main pode travar o loop de
        # eventos. 10 min é mais do que qualquer uso razoável.
        _agendamento_destruicao(janela, 600000)

        # Arrastar a janela pela barra
        _drag = {"x": 0, "y": 0}

        def iniciar_arraste(e):
            _drag["x"], _drag["y"] = e.x, e.y

        def arrastar(e):
            dx = e.x - _drag["x"]
            dy = e.y - _drag["y"]
            x = janela.winfo_x() + dx
            y = janela.winfo_y() + dy
            janela.geometry(f"+{x}+{y}")

        barra.bind("<ButtonPress-1>", iniciar_arraste)
        barra.bind("<B1-Motion>", arrastar)
        lbl_titulo.bind("<ButtonPress-1>", iniciar_arraste)
        lbl_titulo.bind("<B1-Motion>", arrastar)

        # Área de texto com a tradução
        area = scrolledtext.ScrolledText(
            janela, wrap="word", bg="#0d1226", fg="#e2e8f0",
            font=("Segoe UI", 11), borderwidth=0, highlightthickness=0,
            insertbackground="#e2e8f0"
        )
        area.pack(fill="both", expand=True, padx=4, pady=4)
        area.insert("1.0", texto_traduzido)

        # Barra inferior com info
        rodape = tk.Frame(janela, bg="#1a2040", height=22)
        rodape.pack(fill="x")
        rodape.pack_propagate(False)
        tk.Label(
            rodape, text="Ctrl+A = Selecionar tudo | Ctrl+C = Copiar | Clique ✕ para fechar",
            bg="#1a2040", fg="#64748b", font=("Segoe UI", 8)
        ).pack(side="left", padx=6)

        janela.mainloop()

    # Roda em thread separada para não bloquear o Jarvis
    threading.Thread(target=_criar, daemon=True).start()


def traduzir_captura_tela(origem="auto", destino="pt", motor="google",
                          formalidade="formal", formato_saida="txt"):
    """Captura uma área da tela via mouse, faz OCR, traduz e mostra
    o resultado num overlay na tela. Opcionalmente salva em arquivo.

    Args:
        origem: Idioma do texto na tela (ou 'auto')
        destino: Idioma para traduzir
        motor: 'google' ou 'local'
        formalidade: 'formal', 'informal' ou 'neutro'
        formato_saida: 'txt', 'docx', 'pdf', 'md' ou 'nenhum' (só exibe)
    """
    if not HAS_PIL:
        return "ERRO: Pillow não instalado. Execute: pip install Pillow"

    formato_saida = (formato_saida or "txt").strip().lower()
    if formato_saida not in ("txt", "docx", "pdf", "md", "nenhum"):
        return "ERRO: formato_saida deve ser 'txt', 'docx', 'pdf', 'md' ou 'nenhum'."

    # 1. Seleção de área
    bbox = _selecionar_area_traducao()
    if not bbox:
        return "Seleção cancelada (nenhuma área escolhida)."

    # 2. Captura
    try:
        img = ImageGrab.grab(bbox=bbox, all_screens=True)
    except Exception as e:
        return f"ERRO ao capturar tela: {e}"

    # 3. Salva imagem original
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    img_path = os.path.join(config.WORKSPACE, f"scanner_{timestamp}.png")
    img.save(img_path, "PNG")

    # 4. OCR
    texto_original = _ocr_imagem(img, origem)
    if not texto_original:
        return (f"IMAGEM_GERADA: scanner_{timestamp}.png "
                f"(captura de {bbox[2]-bbox[0]}x{bbox[3]-bbox[1]}px)\n\n"
                f"⚠️ OCR não detectou texto na área selecionada. "
                f"Verifique se o texto está legível e tente novamente.")

    # 5. Traduz
    # Usa a função principal (que aplica glossário + cache)
    resultado_traducao = traduzir_texto(
        texto_original, origem, destino, motor, formalidade
    )

    # Extrai só o texto traduzido (sem a linha de info)
    linhas = resultado_traducao.split("\n")
    texto_traduzido = "\n".join(
        l for l in linhas if not l.startswith("──") and not l.startswith("📋")
    ).strip()

    # 6. Mostra overlay na tela
    _mostrar_overlay_traducao(texto_original, texto_traduzido, bbox)

    # 7. Salva em arquivo (se pedido)
    arquivo_saida = ""
    if formato_saida != "nenhum":
        nome_base = f"traducao_{timestamp}"
        arquivo_saida = _salvar_traducao(
            texto_original, texto_traduzido,
            formato_saida, nome_base, origem, destino
        )

    # 8. Monta resposta
    largura = bbox[2] - bbox[0]
    altura = bbox[3] - bbox[1]
    resposta = (
        f"IMAGEM_GERADA: scanner_{timestamp}.png "
        f"(captura de {largura}x{altura}px)\n\n"
        f"📝 Texto detectado (OCR):\n{texto_original}\n\n"
        f"🔄 Tradução:\n{resultado_traducao}"
    )
    if arquivo_saida:
        resposta += f"\n\n💾 Salvo em: {arquivo_saida}"

    return resposta


# =====================================================================
# SALVAR TRADUÇÃO EM DIVERSOS FORMATOS
# =====================================================================

def _salvar_traducao(original, traduzido, formato, nome_base, origem, destino):
    """Salva a tradução no formato escolhido. Retorna o nome do arquivo."""
    caminho = os.path.join(config.WORKSPACE, f"{nome_base}.{formato}")

    if formato == "txt":
        with open(caminho, "w", encoding="utf-8") as f:
            f.write(f"=== ORIGINAL ({origem}) ===\n\n")
            f.write(original)
            f.write(f"\n\n=== TRADUÇÃO ({destino}) ===\n\n")
            f.write(traduzido)
        return f"{nome_base}.txt"

    if formato == "md":
        with open(caminho, "w", encoding="utf-8") as f:
            f.write(f"# Tradução {origem} → {destino}\n\n")
            f.write(f"## Original\n\n{original}\n\n")
            f.write(f"## Tradução\n\n{traduzido}\n\n")
            f.write(f"---\n*Gerado por Jarvis TraduEasy em "
                    f"{time.strftime('%d/%m/%Y %H:%M')}*\n")
        return f"{nome_base}.md"

    if formato == "docx" and HAS_DOCX:
        doc = DocxDocument()
        doc.add_heading(f"Tradução {origem} → {destino}", level=1)
        doc.add_heading("Original", level=2)
        for para in original.split("\n"):
            if para.strip():
                doc.add_paragraph(para)
        doc.add_heading("Tradução", level=2)
        for para in traduzido.split("\n"):
            if para.strip():
                doc.add_paragraph(para)
        doc.add_paragraph(
            f"Gerado por Jarvis TraduEasy em {time.strftime('%d/%m/%Y %H:%M')}"
        ).italic = True
        doc.save(caminho)
        return f"{nome_base}.docx"

    if formato == "pdf" and HAS_FPDF:
        pdf = FPDF()
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=15)
        # Tenta usar fonte com suporte Unicode
        try:
            font_path = Path(__file__).parent / "DejaVuSans.ttf"
            if font_path.exists():
                pdf.add_font("DejaVu", "", str(font_path), uni=True)
                pdf.set_font("DejaVu", size=11)
            else:
                pdf.set_font("Helvetica", size=11)
        except Exception:
            pdf.set_font("Helvetica", size=11)

        pdf.set_font_size(16)
        pdf.cell(0, 10, f"Traducao {origem} -> {destino}", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(5)

        pdf.set_font_size(13)
        pdf.cell(0, 8, "Original", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font_size(11)
        pdf.multi_cell(0, 6, original)
        pdf.ln(5)

        pdf.set_font_size(13)
        pdf.cell(0, 8, "Traducao", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font_size(11)
        pdf.multi_cell(0, 6, traduzido)

        pdf.output(caminho)
        return f"{nome_base}.pdf"

    # Fallback para txt se o formato pedido não está disponível
    if formato in ("docx", "pdf"):
        with open(caminho.replace(f".{formato}", ".txt"), "w", encoding="utf-8") as f:
            f.write(f"ORIGINAL:\n{original}\n\nTRADUÇÃO:\n{traduzido}")
        return f"{nome_base}.txt (formato {formato} não disponível — salvo como txt)"

    return ""


# =====================================================================
# FUNÇÕES DE GERENCIAMENTO (glossário, cache, config)
# =====================================================================

def gerenciar_glossario(acao, termo="", traducao="", idioma="*"):
    """Gerencia o glossário de tradução.

    Args:
        acao: 'adicionar', 'remover' ou 'listar'
        termo: Termo no idioma original
        traducao: Tradução desejada (só para 'adicionar')
        idioma: Código do idioma destino ou '*' para todos
    """
    acao = (acao or "listar").strip().lower()

    if acao == "listar":
        return _glossario.listar(idioma if idioma != "*" else None)
    elif acao == "adicionar":
        if not termo or not traducao:
            return "ERRO: 'adicionar' requer termo e traducao."
        return _glossario.adicionar(termo, traducao, idioma)
    elif acao == "remover":
        if not termo:
            return "ERRO: 'remover' requer termo."
        return _glossario.remover(termo, idioma)
    else:
        return "ERRO: ação deve ser 'adicionar', 'remover' ou 'listar'."


def memoria_traducao(acao="buscar", termo=""):
    """Consulta ou gerencia a memória de tradução (cache).

    Args:
        acao: 'buscar', 'estatisticas' ou 'limpar'
        termo: Termo para buscar (só para 'buscar')
    """
    acao = (acao or "buscar").strip().lower()

    if acao == "buscar":
        if not termo:
            return "ERRO: forneça um termo para buscar na memória."
        resultados = _cache.buscar(termo)
        if not resultados:
            return f"Nenhuma tradução encontrada na memória para '{termo}'."
        linhas = [f"🧠 Memória de tradução — {len(resultados)} resultado(s) para '{termo}':"]
        for r in resultados:
            linhas.append(
                f"\n  [{r.get('origem','?')} → {r.get('destino','?')}] "
                f"({r.get('motor','?')})\n"
                f"  Original: {r.get('original','')}\n"
                f"  Tradução: {r.get('traducao','')[:200]}"
            )
        return "\n".join(linhas)

    elif acao == "estatisticas":
        stats = _cache.estatisticas()
        return (
            f"🧠 Memória de tradução:\n"
            f"  Entradas: {stats['total_entradas']} / {stats['max_entradas']}\n"
            f"  Arquivo: {_CACHE_FILE}"
        )

    elif acao == "limpar":
        return _cache.limpar()

    return "ERRO: ação deve ser 'buscar', 'estatisticas' ou 'limpar'."


def listar_idiomas():
    """Lista todos os idiomas disponíveis para tradução."""
    linhas = ["🌍 Idiomas disponíveis para tradução:\n"]
    for codigo, info in IDIOMAS.items():
        if codigo == "auto":
            continue
        linhas.append(f"  {info['bandeira']} {codigo:5s} — {info['nome']}")
    linhas.append(f"\n  Total: {len(IDIOMAS) - 1} idiomas")
    linhas.append(f"  Use 'auto' como origem para detecção automática.")
    return "\n".join(linhas)


# =====================================================================
# REGISTRO DO PLUGIN (TOOLS + FERRAMENTAS)
# =====================================================================

TOOLS = [
    {"type": "function", "function": {
        "name": "traduzir_texto",
        "description": (
            "Traduz um texto usando motor dedicado. Dois motores: "
            "'google' (API gratuita, rapido, sem custo) ou 'local' "
            "(Ollama com prompts especializados por idioma, mais preciso). "
            "Aplica glossario personalizado e salva na memoria de traducao "
            "(cache). Use para traducoes rapidas de texto no chat. "
            "Diferente de traduzir_documento_em_blocos (que processa "
            "arquivos grandes em background), esta funcao traduz texto "
            "DIRETO e devolve o resultado na hora."
        ),
        "parameters": {"type": "object", "properties": {
            "texto": {"type": "string",
                      "description": "Texto a traduzir"},
            "origem": {"type": "string", "default": "auto",
                       "description": "Codigo do idioma de origem (ex: en, pt, es, ja) ou 'auto' para detectar"},
            "destino": {"type": "string", "default": "en",
                        "description": "Codigo do idioma de destino (ex: pt, en, es, fr, de, ja, zh)"},
            "motor": {"type": "string", "enum": ["google", "local"], "default": "google",
                      "description": "Motor: 'google' (API gratuita, rapido) ou 'local' (Ollama, privado, com prompts especializados)"},
            "formalidade": {"type": "string", "enum": ["formal", "informal", "neutro"],
                           "default": "formal",
                           "description": "Registro: formal, informal ou neutro"},
        }, "required": ["texto"]}}},

    {"type": "function", "function": {
        "name": "traduzir_captura_tela",
        "description": (
            "SCANNER+TRADUTOR: Deixa o usuario selecionar com o mouse "
            "qualquer area da tela, faz OCR (extrai texto da imagem), "
            "traduz automaticamente e mostra o resultado num overlay "
            "sobre a tela. Opcionalmente salva em arquivo (txt, docx, "
            "pdf ou md). Use quando o usuario pedir para 'traduzir o que "
            "esta na tela', 'scanear e traduzir', 'ler e traduzir isso', "
            "'traduzir essa imagem/pagina/documento na tela', 'OCR e traduzir' etc."
        ),
        "parameters": {"type": "object", "properties": {
            "origem": {"type": "string", "default": "auto",
                       "description": "Idioma do texto na tela (ou 'auto')"},
            "destino": {"type": "string", "default": "pt",
                        "description": "Idioma para traduzir"},
            "motor": {"type": "string", "enum": ["google", "local"], "default": "google",
                      "description": "'google' ou 'local' (Ollama)"},
            "formalidade": {"type": "string", "enum": ["formal", "informal", "neutro"],
                           "default": "formal"},
            "formato_saida": {"type": "string",
                              "enum": ["txt", "docx", "pdf", "md", "nenhum"],
                              "default": "txt",
                              "description": "Formato para salvar: txt, docx, pdf, md ou nenhum (so exibe na tela)"},
        }}}},

    {"type": "function", "function": {
        "name": "gerenciar_glossario",
        "description": (
            "Gerencia o glossario de traducao (termos protegidos que NAO "
            "devem ser traduzidos literalmente — nomes de produto, termos "
            "tecnicos, marcas). Acoes: 'adicionar' (novo termo+traducao), "
            "'remover' (apaga termo), 'listar' (mostra todos)."
        ),
        "parameters": {"type": "object", "properties": {
            "acao": {"type": "string", "enum": ["adicionar", "remover", "listar"],
                     "description": "O que fazer: adicionar, remover ou listar"},
            "termo": {"type": "string",
                      "description": "Termo original a proteger (obrigatorio para adicionar/remover)"},
            "traducao": {"type": "string",
                         "description": "Como o termo deve aparecer na traducao (obrigatorio para adicionar)"},
            "idioma": {"type": "string", "default": "*",
                       "description": "Codigo do idioma destino (ou '*' para aplicar a todos)"},
        }, "required": ["acao"]}}},

    {"type": "function", "function": {
        "name": "memoria_traducao",
        "description": (
            "Consulta a memoria de traducao (cache de traducoes anteriores). "
            "Acoes: 'buscar' (procura traducoes anteriores por termo), "
            "'estatisticas' (quantas traducoes salvas), 'limpar' (apaga tudo)."
        ),
        "parameters": {"type": "object", "properties": {
            "acao": {"type": "string", "enum": ["buscar", "estatisticas", "limpar"],
                     "description": "O que fazer"},
            "termo": {"type": "string",
                      "description": "Termo para buscar (so para acao 'buscar')"},
        }, "required": ["acao"]}}},

    {"type": "function", "function": {
        "name": "listar_idiomas",
        "description": "Lista todos os 24 idiomas disponiveis para traducao, com codigo, nome e bandeira.",
        "parameters": {"type": "object", "properties": {}}}},
]

FERRAMENTAS = {
    "traduzir_texto": traduzir_texto,
    "traduzir_captura_tela": traduzir_captura_tela,
    "gerenciar_glossario": gerenciar_glossario,
    "memoria_traducao": memoria_traducao,
    "listar_idiomas": listar_idiomas,
}
