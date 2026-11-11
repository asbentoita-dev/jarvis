# 🧠 Registro configurável de "cérebros" de nuvem (API / HTTPS).
#
# Em vez de cada provedor (Groq, 9router, ...) ser hardcoded no código,
# eles vivem em cerebros.json — editável sem tocar em Python. Cada
# entrada vira um prefixo <nome>/<modelo> no seletor de modelos,
# roteado pelo mesmo cliente compatível com a API da OpenAI.
#
# Cada campo pode ser um valor literal (ex: "url": "http://...") OU
# apontar para uma variável do .env (ex: "url_env": "ROUTER_URL"),
# para quem prefere manter chaves fora do JSON.

import json
import os
import re

import requests

import config

CEREBROS_PATH = os.path.join(config.BASE_DIR, "cerebros.json")

# entradas padrão: mantêm Groq e 9router funcionando via .env,
# igual já funcionava antes deste arquivo existir.
_PADRAO = {
    "groq": {
        "tipo": "api",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "chave_env": "GROQ_API_KEY",
        "modelos_env": "GROQ_MODELOS",
        "modelos_padrao": "llama-3.3-70b-versatile"
    },
    "router": {
        "tipo": "https",
        "url_env": "ROUTER_URL",
        "chave_env": "ROUTER_API_KEY",
        "modelos_env": "ROUTER_MODELOS"
    }
}


def _garantir_arquivo():
    if not os.path.exists(CEREBROS_PATH):
        try:
            with open(CEREBROS_PATH, "w", encoding="utf-8") as f:
                json.dump(_PADRAO, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def _migrar_campos_padrao(dados):
    """Preenche campos novos que faltem nas entradas padrão (groq/router)
    sem sobrescrever nada que o usuário já tenha customizado — evita que
    um cerebros.json gerado por uma versão antiga fique incompleto."""
    mudou = False
    for nome, padrao_cfg in _PADRAO.items():
        if nome in dados and isinstance(dados[nome], dict):
            for campo, valor in padrao_cfg.items():
                if campo not in dados[nome]:
                    dados[nome][campo] = valor
                    mudou = True
    if mudou:
        try:
            with open(CEREBROS_PATH, "w", encoding="utf-8") as f:
                json.dump(dados, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return dados


def _carregar_arquivo():
    _garantir_arquivo()
    try:
        with open(CEREBROS_PATH, "r", encoding="utf-8") as f:
            dados = json.load(f)
        if isinstance(dados, dict) and dados:
            return _migrar_campos_padrao(dados)
    except Exception:
        pass
    return dict(_PADRAO)


def _resolver(cfg, campo):
    """Valor literal tem prioridade; senão busca do .env pelo *_env."""
    if cfg.get(campo):
        return cfg[campo]
    env_var = cfg.get(f"{campo}_env")
    if env_var:
        return os.environ.get(env_var, "")
    return ""


def _resolver_modelos(cfg):
    """Ordem: lista literal > variável do .env (se preenchida) >
    modelos_padrao (fallback embutido, ex: Groq sem GROQ_MODELOS no .env)."""
    bruto = cfg.get("modelos")
    if bruto:
        if isinstance(bruto, list):
            return [str(m).strip() for m in bruto if str(m).strip()]
        return [m.strip() for m in str(bruto).split(",") if m.strip()]

    env_var = cfg.get("modelos_env")
    if env_var:
        val = os.environ.get(env_var, "")
        if val:
            return [m.strip() for m in val.split(",") if m.strip()]

    padrao = cfg.get("modelos_padrao")
    if padrao:
        return [m.strip() for m in str(padrao).split(",") if m.strip()]

    return []


def _limpar_url_colada(bruto):
    """Muita documentação de API (inclusive a do Google/Gemini) mostra um
    comando curl inteiro pra copiar, e é fácil colar o bloco todo no campo
    de URL sem querer — aí o requests quebra com um erro ilegível tipo
    'No connection adapters were found for curl "https://...'. Detecta
    esse caso e extrai só a URL de dentro do texto colado."""
    bruto = (bruto or "").strip()
    if not bruto or bruto.lower().startswith(("http://", "https://")):
        return bruto
    m = re.search(r'https?://[^\s"\'\\]+', bruto)
    return m.group(0) if m else bruto


def _normalizar_url_chat(url):
    """Muita gente cola só a URL base (ex: http://host:porta/v1) em vez
    do endpoint completo de chat — sem isso o POST cai num caminho que
    não aceita POST e dá 405. Completa com /chat/completions se faltar."""
    url = _limpar_url_colada(url)
    if not url:
        return url
    sem_barra = url.rstrip("/")
    if sem_barra.endswith("/chat/completions"):
        return sem_barra
    return sem_barra + "/chat/completions"


def listar():
    """Só devolve cérebros com URL e ao menos 1 modelo configurados."""
    dados = _carregar_arquivo()
    saida = []
    for nome, cfg in dados.items():
        if not isinstance(cfg, dict) or cfg.get("ativo") is False:
            continue
        url = _normalizar_url_chat(_resolver(cfg, "url"))
        modelos = _resolver_modelos(cfg)
        if not url or not modelos:
            continue
        saida.append({
            "nome": nome,
            "tipo": cfg.get("tipo", "https"),
            "url": url,
            "chave": _resolver(cfg, "chave"),
            "modelos": modelos,
            # timeout configurável por cérebro. Padrão = sem limite (espera
            # indefinidamente), porque roteadores/providers novos podem
            # demorar sem aviso e não dá pra saber de antemão o que vai
            # ser instalado. Se algum cérebro específico precisar de um
            # limite, basta pôr "timeout": <segundos> nele no
            # cerebros.json.
            "timeout": (lambda t: None if t in (None, 0) else int(t))(
                cfg.get("timeout")
            ),
        })
    return saida


def buscar(nome):
    for c in listar():
        if c["nome"] == nome:
            return c
    return None


def nomes_configurados():
    return {c["nome"] for c in listar()}


def descobrir_modelos(url, chave=""):
    """Pergunta ao próprio endpoint quais modelos ele tem (GET .../models,
    convenção da API da OpenAI) — evita ter que adivinhar/digitar os
    nomes na mão. Aceita a URL de chat completions ou já a de /models."""
    url = _limpar_url_colada(url)
    if not url:
        raise ValueError("URL vazia")

    base = url
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    alvo = base.rstrip("/") + "/models"

    headers = {}
    if chave:
        headers["Authorization"] = f"Bearer {chave}"

    r = requests.get(alvo, headers=headers, timeout=10)
    r.raise_for_status()
    dados = r.json()

    itens = dados.get("data") if isinstance(dados, dict) else dados
    if not isinstance(itens, list):
        raise ValueError("resposta inesperada do endpoint /models")

    nomes = []
    for item in itens:
        if isinstance(item, dict) and item.get("id"):
            nomes.append(item["id"])
        elif isinstance(item, str):
            nomes.append(item)
    return nomes


def listar_todos_raw():
    """Todas as entradas do arquivo, mesmo inativas/incompletas — para
    telas de edição (diferente de listar(), que só mostra as prontas)."""
    return _carregar_arquivo()


def salvar(nome, tipo, url, chave, modelos, ativo=True):
    """Cria ou atualiza uma entrada (valores literais, sem *_env) —
    usado pela interface web para adicionar/editar cérebros na mão."""
    nome = (nome or "").strip()
    if not nome:
        raise ValueError("nome vazio")

    dados = _carregar_arquivo()
    if isinstance(modelos, str):
        modelos = [m.strip() for m in modelos.split(",") if m.strip()]

    dados[nome] = {
        "tipo": tipo or "https",
        "url": _limpar_url_colada(url),
        "chave": chave or "",
        "modelos": modelos or [],
        "ativo": bool(ativo),
    }
    with open(CEREBROS_PATH, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    return dados[nome]


def remover(nome):
    dados = _carregar_arquivo()
    if nome in dados:
        del dados[nome]
        with open(CEREBROS_PATH, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
        return True
    return False
