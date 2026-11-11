#!/usr/bin/env python3
# 🔌 Plugin: navegador avançado v2 (Playwright)
# Sessões persistentes, múltiplas abas, JavaScript, screenshots

import os
import queue
import threading
import time

from config import WORKSPACE

_sessoes = {}


def _criar_worker(sessao_id, fila):
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    storage_path = os.path.join(WORKSPACE, f".browser_sessao_{sessao_id}")
    context = pw.chromium.launch_persistent_context(
        storage_path, headless=True,
        viewport={"width": 1280, "height": 900},
        ignore_https_errors=True,
    )
    page = context.pages[0] if context.pages else context.new_page()

    while True:
        item = fila.get()
        if item is None:
            try:
                context.close()
                pw.stop()
            except Exception:
                pass
            break
        func, args, retorno = item
        try:
            retorno.put(("ok", func(page, *args)))
        except Exception as e:
            retorno.put(("erro", f"{type(e).__name__}: {e}"))


def _executar(sessao_id, func, *args):
    global _sessoes
    if sessao_id not in _sessoes:
        _sessoes[sessao_id] = fila = queue.Queue()
        threading.Thread(target=_criar_worker, args=(sessao_id, fila), daemon=True).start()
        time.sleep(2.0)
    fila = _sessoes[sessao_id]
    retorno = queue.Queue()
    fila.put((func, args, retorno))
    try:
        status, valor = retorno.get(timeout=120)
    except queue.Empty:
        return "ERRO no navegador: tempo esgotado."
    return valor if status == "ok" else f"ERRO no navegador: {valor}"


def _abrir(page, url):
    page.goto(url, timeout=30000, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    return f"[Página: {page.title()}]\n{page.url}\n\n{page.inner_text('body')[:3000]}"


def _ir_para(page, url):
    page.goto(url, timeout=30000, wait_until="domcontentloaded")
    page.wait_for_timeout(1000)
    return f"[Página: {page.title()}]"


def _nova_aba(page, url="about:blank"):
    nova = page.context.new_page()
    if url and url != "about:blank":
        nova.goto(url, timeout=30000, wait_until="domcontentloaded")
        nova.wait_for_timeout(800)
    return f"Nova aba: {nova.title() or url}"


def _listar_abas(page):
    return "Abas:\n" + "\n".join(
        f"  [{i}] {p.title() or '(sem titulo)'} — {p.url}"
        for i, p in enumerate(page.context.pages)
    )


def _trocar_aba(page, indice):
    abas = page.context.pages
    if 0 <= indice < len(abas):
        abas[indice].bring_to_front()
        return f"Aba [{indice}]: {abas[indice].title()}"
    return f"Indice {indice} invalido. Total: {len(abas)}"


def _fechar_aba(page, indice=None):
    abas = page.context.pages
    if indice is None:
        indice = len(abas) - 1
    if 0 <= indice < len(abas):
        abas[indice].close()
        return f"Fechou aba [{indice}]"
    return "Indice invalido"


def _clicar(page, alvo):
    try:
        page.get_by_text(alvo, exact=False).first.click(timeout=5000)
    except Exception:
        page.click(alvo, timeout=5000)
    page.wait_for_timeout(1500)
    return f"Cliquei em '{alvo}'"


def _preencher(page, campo, texto):
    try:
        page.fill(campo, texto, timeout=5000)
    except Exception:
        page.get_by_placeholder(campo).first.fill(texto, timeout=5000)
    return f"Preenchi '{campo}' com '{texto}'."


def _digitando(page, texto):
    page.keyboard.type(texto, delay=30)
    return f"Digitou: {texto[:50]}"


def _rolar(page, direcao="down", quantidade=500):
    if direcao == "down":
        page.mouse.wheel(0, quantidade)
    elif direcao == "up":
        page.mouse.wheel(0, -quantidade)
    page.wait_for_timeout(800)
    return f"Rolou {direcao}."


def _selecionar(page, seletor, valor):
    page.select_option(seletor, valor, timeout=5000)
    return f"Selecionou '{valor}' em '{seletor}'."


def _avaliar(page, expressao_js):
    return str(page.evaluate(expressao_js))


def _html(page):
    return page.content()[:10000]


def _texto(page):
    return page.inner_text("body")[:5000]


def _titulo(page):
    return page.title()


def _url(page):
    return page.url


def _capturar_screenshot(page, nome="screenshot.png"):
    caminho = os.path.join(WORKSPACE, nome)
    page.screenshot(path=caminho, full_page=True)
    return f"IMAGEM_GERADA: {nome} ({caminho})"


def _fechar_pagina(page):
    page.close()
    return "Aba fechada."


def _fechar_navegador(page):
    page.context.close()
    return "Navegador fechado."


TOOLS = [
    {"type": "function", "function": {
        "name": "abrir_pagina",
        "description": "Abre um site e retorna o texto da pagina.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "URL do site"},
            "sessao": {"type": "string", "description": "Nome da sessao"}},
        "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "navegar_para",
        "description": "Navega para uma URL diferente.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "URL"},
            "sessao": {"type": "string", "description": "Nome da sessao"}},
        "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "nova_aba",
        "description": "Abre uma nova aba.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "URL (opcional)"},
            "sessao": {"type": "string", "description": "Nome da sessao"}}}}},
    {"type": "function", "function": {
        "name": "listar_abas",
        "description": "Lista todas as abas abertas.",
        "parameters": {"type": "object", "properties": {
            "sessao": {"type": "string", "description": "Nome da sessao"}}}}},
    {"type": "function", "function": {
        "name": "trocar_aba",
        "description": "Foca numa aba pelo indice.",
        "parameters": {"type": "object", "properties": {
            "indice": {"type": "integer", "description": "Indice da aba"},
            "sessao": {"type": "string", "description": "Nome da sessao"}},
        "required": ["indice"]}}},
    {"type": "function", "function": {
        "name": "fechar_aba",
        "description": "Fecha uma aba.",
        "parameters": {"type": "object", "properties": {
            "indice": {"type": "integer", "description": "Indice"},
            "sessao": {"type": "string", "description": "Nome da sessao"}}}}},
    {"type": "function", "function": {
        "name": "clicar",
        "description": "Clica num link ou botao.",
        "parameters": {"type": "object", "properties": {
            "alvo": {"type": "string", "description": "Texto ou seletor CSS"},
            "sessao": {"type": "string", "description": "Nome da sessao"}},
        "required": ["alvo"]}}},
    {"type": "function", "function": {
        "name": "preencher",
        "description": "Preenche um campo de formulario.",
        "parameters": {"type": "object", "properties": {
            "campo": {"type": "string", "description": "Seletor ou placeholder"},
            "texto": {"type": "string", "description": "Texto a digitar"},
            "sessao": {"type": "string", "description": "Nome da sessao"}},
        "required": ["campo", "texto"]}}},
    {"type": "function", "function": {
        "name": "digitando",
        "description": "Digita texto no campo focado.",
        "parameters": {"type": "object", "properties": {
            "texto": {"type": "string", "description": "Texto para digitar"},
            "sessao": {"type": "string", "description": "Nome da sessao"}},
        "required": ["texto"]}}},
    {"type": "function", "function": {
        "name": "rolar",
        "description": "Rola a página.",
        "parameters": {"type": "object", "properties": {
            "direcao": {"type": "string", "enum": ["down", "up", "top", "bottom"]},
            "quantidade": {"type": "integer", "description": "Pixels"},
            "sessao": {"type": "string", "description": "Nome da sessao"}}}}},
    {"type": "function", "function": {
        "name": "selecionar",
        "description": "Seleciona opcao de select.",
        "parameters": {"type": "object", "properties": {
            "seletor": {"type": "string", "description": "CSS selector"},
            "valor": {"type": "string", "description": "Valor ou label"},
            "sessao": {"type": "string", "description": "Nome da sessao"}},
        "required": ["seletor", "valor"]}}},
    {"type": "function", "function": {
        "name": "avaliar",
        "description": "Executa JavaScript na pagina.",
        "parameters": {"type": "object", "properties": {
            "expressao_js": {"type": "string", "description": "JavaScript para avaliar"},
            "sessao": {"type": "string", "description": "Nome da sessao"}},
        "required": ["expressao_js"]}}},
    {"type": "function", "function": {
        "name": "capturar_screenshot",
        "description": "Tira screenshot da pagina e salva no workspace.",
        "parameters": {"type": "object", "properties": {
            "nome": {"type": "string", "description": "Nome do arquivo (padrao: screenshot.png)"},
            "sessao": {"type": "string", "description": "Nome da sessao"}}}}},
    {"type": "function", "function": {
        "name": "html_da_pagina",
        "description": "Retorna o HTML da pagina.",
        "parameters": {"type": "object", "properties": {
            "sessao": {"type": "string", "description": "Nome da sessao"}}}}},
    {"type": "function", "function": {
        "name": "texto_da_pagina",
        "description": "Retorna o texto visivel da pagina.",
        "parameters": {"type": "object", "properties": {
            "sessao": {"type": "string", "description": "Nome da sessao"}}}}},
    {"type": "function", "function": {
        "name": "titulo_da_pagina",
        "description": "Retorna o titulo da pagina.",
        "parameters": {"type": "object", "properties": {
            "sessao": {"type": "string", "description": "Nome da sessao"}}}}},
    {"type": "function", "function": {
        "name": "url_atual",
        "description": "Retorna a URL atual.",
        "parameters": {"type": "object", "properties": {
            "sessao": {"type": "string", "description": "Nome da sessao"}}}}},
    {"type": "function", "function": {
        "name": "fechar_pagina",
        "description": "Fecha a aba atual.",
        "parameters": {"type": "object", "properties": {
            "sessao": {"type": "string", "description": "Nome da sessao"}}}}},
    {"type": "function", "function": {
        "name": "fechar_navegador",
        "description": "Fecha o navegador da sessao.",
        "parameters": {"type": "object", "properties": {
            "sessao": {"type": "string", "description": "Nome da sessao"}}}}},
]

FERRAMENTAS = {
    "abrir_pagina": lambda url, sessao="default": _executar(sessao, _abrir, url),
    "navegar_para": lambda url, sessao="default": _executar(sessao, _ir_para, url),
    "nova_aba": lambda url="", sessao="default": _executar(sessao, _nova_aba, url),
    "listar_abas": lambda sessao="default": _executar(sessao, _listar_abas),
    "trocar_aba": lambda indice, sessao="default": _executar(sessao, _trocar_aba, indice),
    "fechar_aba": lambda indice=None, sessao="default": _executar(sessao, _fechar_aba, indice),
    "clicar": lambda alvo, sessao="default": _executar(sessao, _clicar, alvo),
    "preencher": lambda campo, texto, sessao="default": _executar(sessao, _preencher, campo, texto),
    "digitando": lambda texto, sessao="default": _executar(sessao, _digitando, texto),
    "rolar": lambda direcao="down", quantidade=500, sessao="default": _executar(sessao, _rolar, direcao, quantidade),
    "selecionar": lambda seletor, valor, sessao="default": _executar(sessao, _selecionar, seletor, valor),
    "avaliar": lambda expressao_js, sessao="default": _executar(sessao, _avaliar, expressao_js),
    "capturar_screenshot": lambda nome="screenshot.png", sessao="default": _executar(sessao, _capturar_screenshot, nome),
    "html_da_pagina": lambda sessao="default": _executar(sessao, _html),
    "texto_da_pagina": lambda sessao="default": _executar(sessao, _texto),
    "titulo_da_pagina": lambda sessao="default": _executar(sessao, _titulo),
    "url_atual": lambda sessao="default": _executar(sessao, _url),
    "fechar_pagina": lambda sessao="default": _executar(sessao, _fechar_pagina),
    "fechar_navegador": lambda sessao="default": _executar(sessao, _fechar_navegador),
}