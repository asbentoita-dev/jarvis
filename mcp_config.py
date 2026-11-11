# 📖 Leitura/escrita de mcp.json — compartilhado entre o plugin que
# conecta os servidores (plugins/mcp_bridge.py) e a interface web, que
# permite adicionar/remover servidores sem editar o arquivo na mão.

import json
import os

import config

MCP_CONFIG_PATH = os.path.join(config.BASE_DIR, "mcp.json")

_EXEMPLO = {
    "mcpServers": {
        "_exemplo_desativado": {
            "ativo": False,
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem",
                      "C:\\caminho\\para\\liberar"]
        }
    }
}


def _ler_bruto():
    if not os.path.exists(MCP_CONFIG_PATH):
        try:
            with open(MCP_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(_EXEMPLO, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return dict(_EXEMPLO)
    try:
        with open(MCP_CONFIG_PATH, "r", encoding="utf-8") as f:
            dados = json.load(f)
        if isinstance(dados, dict):
            return dados
    except Exception:
        pass
    return {"mcpServers": {}}


def ler():
    """{"nome": {"command":..., "args":[...], "env":{...}, "ativo": bool}}"""
    return _ler_bruto().get("mcpServers", {}) or {}


def salvar_servidor(nome, command, args=None, env=None, ativo=True):
    nome = (nome or "").strip()
    if not nome:
        raise ValueError("nome vazio")
    if not command:
        raise ValueError("command vazio")

    dados = _ler_bruto()
    dados.setdefault("mcpServers", {})
    dados["mcpServers"][nome] = {
        "command": command,
        "args": args or [],
        "env": env or {},
        "ativo": bool(ativo),
    }
    with open(MCP_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    return dados["mcpServers"][nome]


def remover_servidor(nome):
    dados = _ler_bruto()
    servidores = dados.get("mcpServers", {})
    if nome in servidores:
        del servidores[nome]
        with open(MCP_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
        return True
    return False
