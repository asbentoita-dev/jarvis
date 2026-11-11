"""Times do modo multiagente: mapeia PAPEL -> modelo local, por time.

equipes.json (raiz do projeto) guarda os times editáveis pela interface
(ou na mão). Se o arquivo não existir ainda, é criado com os dois times
que o Bento definiu (Qualidade e Rápido) na primeira leitura.

Papéis reconhecidos: planejador, tecnico, analista, conteudo, visual,
revisor. Um time não precisa preencher todos — o orquestrador (ver
plugins/multiagente.py) usa só os papéis que a missão realmente precisa.
"""

import json
import os

import config

_ARQUIVO = os.path.join(config.BASE_DIR, "equipes.json")

# times de teste definidos pelo Bento (12/08/2026) — "time 1" = qualidade
# (modelos maiores, revisor mais rigoroso em teoria), "time 2" = rápido
# (modelos leves, pro dia a dia). Ajustável em equipes.json sem mexer em código.
_PADRAO = {
    "qualidade": {
        "planejador": "qwen3:8b",
        "tecnico": "qwen2.5-coder:7b",
        "analista": "deepseek-r1:7b",
        "conteudo": "fauxpaslife/gemma-4-E4B-it-textonly-sysprmpt-Q4_K_M:latest",
        "visual": "qwen2.5vl:7b",
        "revisor": "pdurlej/qwythos-9b-claude-mythos-5-1m:latest",
    },
    "rapido": {
        "planejador": "Ministral-3:3b",
        "tecnico": "qwen2.5-coder:1.5b",
        "analista": "llama3.1:latest",
        "conteudo": "hf.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF:Q4_K_M",
        "visual": "qwen2.5vl:7b",
        "revisor": "fauxpaslife/gemma-4-E4B-it-textonly-sysprmpt-Q4_K_M:latest",
    },
}


def _carregar():
    if not os.path.isfile(_ARQUIVO):
        salvar(_PADRAO)
        return dict(_PADRAO)
    try:
        with open(_ARQUIVO, "r", encoding="utf-8") as f:
            dados = json.load(f)
        return dados if isinstance(dados, dict) and dados else dict(_PADRAO)
    except Exception:
        return dict(_PADRAO)


def salvar(dados):
    with open(_ARQUIVO, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)


def listar():
    """Todos os times cadastrados: {nome_time: {papel: modelo}}."""
    return _carregar()


def obter(nome_time):
    """Devolve o mapa papel->modelo de UM time, ou None se não existir."""
    return _carregar().get(nome_time)


def nomes():
    return list(_carregar().keys())
