#!/usr/bin/env python3
# 🔌 Plugin: selenium_avancado
# Usando Selenium + Edge para captura de páginas web
# Integrado com o sistema de plugins do Jarvis

import os
import queue
import threading
import time

from config import WORKSPACE

# Worker do Selenium
_fila = queue.Queue()
_browser = None
_page = None


def _worker():
    global _browser, _page
    from selenium import webdriver
    from selenium.webdriver.edge.service import Service
    from selenium.webdriver.edge.options import Options
    from selenium.webdriver.common.by import By

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    _browser = webdriver.Edge(options=options)
    _page = _browser.page_source

    while True:
        item = _fila.get()
        if item is None:  # parar
            try:
                _browser.quit()
            except Exception:
                pass
            break

        func, args, retorno = item
        try:
            retorno.put(("ok", func(_browser, _page, *args)))
        except Exception as e:
            retorno.put(("erro", f"{type(e).__name__}: {e}"))


def _executar(func, *args):
    global _browser
    if _browser is None:
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        time.sleep(2.0)

    retorno = queue.Queue()
    _fila.put((func, args, retorno))
    try:
        status, valor = retorno.get(timeout=90)
    except queue.Empty:
        return "ERRO no selenium: tempo esgotado."
    return valor if status == "ok" else f"ERRO no selenium: {valor}"


# Funções de captura
def _abrir_page(browser, source, url):
    browser.get(url)
    browser.implicitly_wait(5)
    title = browser.title
    texto = browser.find_element("tag name", "body").text[:3000]
    return f"[Página: {title}]\n{url}\n\n{texto}"


def _capturar_tela(browser, source, nome="selenium.png"):
    caminho = os.path.join(WORKSPACE, nome)
    browser.save_screenshot(caminho)
    return f"IMAGEM_GERADA: {nome} ({caminho})"


def _extraind_texto(browser, source):
    return browser.find_element("tag name", "body").text[:5000]


def _extraind_html(browser, source):
    return browser.page_source[:10000]


# Interface pública
def abrir_pagina(url, sessao="selenium"):
    return _executar(_abrir_page, url)


def capturar_tela(nome_arquivo="selenium.png", sessao="selenium"):
    return _executar(_capturar_tela, nome_arquivo)


def extrair_texto(sessao="selenium"):
    return _executar(_extraind_texto)


def extrair_html(sessao="selenium"):
    return _executar(_extraind_html)


def fechar_navegador(sessao="selenium"):
    global _browser
    if _browser:
        _browser.quit()
        _browser = None
    return "Navegador fechado."


# Descrição para o sistema de ferramentas
TOOLS = [
    {"type": "function", "function": {
        "name": "abrir_pagina_selenium",
        "description": "Abre um site com Selenium (Edge) e retorna o texto da página.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Endereço do site"},
            "sessao": {"type": "string", "description": "Nome da sessão (padrão: 'selenium')"}},
        "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "capturar_tela_selenium",
        "description": "Tira screenshot da página atual.",
        "parameters": {"type": "object", "properties": {
            "nome_arquivo": {"type": "string", "description": "Nome do arquivo (padrão: selenium.png)"},
            "sessao": {"type": "string", "description": "Nome da sessão"}}}}},
    {"type": "function", "function": {
        "name": "extrair_texto_selenium",
        "description": "Extrai todo o texto visível da página.",
        "parameters": {"type": "object", "properties": {
            "sessao": {"type": "string", "description": "Nome da sessão"}}}}},
    {"type": "function", "function": {
        "name": "extrair_html_selenium",
        "description": "Extrai o HTML completo da página.",
        "parameters": {"type": "object", "properties": {
            "sessao": {"type": "string", "description": "Nome da sessão"}}}}},
    {"type": "function", "function": {
        "name": "fechar_selenium",
        "description": "Fecha o navegador Selenium.",
        "parameters": {"type": "object", "properties": {
            "sessao": {"type": "string", "description": "Nome da sessão"}}}}},
]

FERRAMENTAS = {
    "abrir_pagina_selenium": abrir_pagina,
    "capturar_tela_selenium": capturar_tela,
    "extrair_texto_selenium": extrair_texto,
    "extrair_html_selenium": extrair_html,
    "fechar_selenium": fechar_navegador,
}