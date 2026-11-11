# 🔌 Plugin: ponte MCP (Model Context Protocol)
#
# Conecta o Jarvis a servidores MCP externos (o mesmo tipo de servidor
# usado pelo Claude Desktop/Code) e expõe as ferramentas deles como
# ferramentas normais do Jarvis — o modelo nem percebe a diferença.
#
# Configuração: mcp.json na pasta do Jarvis Claude, MESMO FORMATO do
# Claude Desktop:
#   {
#     "mcpServers": {
#       "nome-do-servidor": {
#         "command": "npx",
#         "args": ["-y", "@algum/pacote-mcp"],
#         "env": {"CHAVE": "valor"}
#       }
#     }
#   }
#
# Instalar (uma vez): pip install mcp
#
# 💡 Cada chamada de ferramenta MCP abre uma conexão nova e fecha ao
# terminar (não fica processo/subprocesso ocioso rodando) — de propósito,
# para não competir por CPU/RAM com um modelo local em uso.

import asyncio
import os
import re

import mcp_config

TOOLS = []
FERRAMENTAS = {}


def _ler_config():
    # leitura/escrita de mcp.json fica centralizada em mcp_config.py,
    # compartilhada com a interface web (aba Configurações)
    return mcp_config.ler()


def _params(cfg):
    from mcp import StdioServerParameters
    env = os.environ.copy()
    env.update(cfg.get("env") or {})
    return StdioServerParameters(
        command=cfg["command"],
        args=cfg.get("args", []),
        env=env,
        cwd=cfg.get("cwd"),
    )


async def _listar_tools_async(cfg):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    async with stdio_client(_params(cfg)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            resultado = await session.list_tools()
            # SDKs mais novos usam input_schema (snake_case) em vez de
            # inputSchema — aceita os dois para não depender da versão exata.
            return [{"name": t.name, "description": t.description or "",
                    "inputSchema": (getattr(t, "input_schema", None)
                                     or getattr(t, "inputSchema", None)
                                     or {"type": "object", "properties": {}})}
                    for t in resultado.tools]


async def _chamar_tool_async(cfg, nome_tool, args):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    async with stdio_client(_params(cfg)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            resultado = await session.call_tool(nome_tool, args or {})
            partes = [getattr(c, "text", "") for c in resultado.content
                      if getattr(c, "text", "")]
            return "\n".join(partes) if partes else str(resultado)


def _nome_seguro(texto):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", texto)[:64]


def _fabrica_executor(cfg, nome_tool):
    def _executar(**kwargs):
        try:
            return asyncio.run(
                asyncio.wait_for(_chamar_tool_async(cfg, nome_tool, kwargs),
                                  timeout=60))
        except Exception as e:
            return f"ERRO MCP ({nome_tool}): {type(e).__name__}: {e}"
    return _executar


def _descobrir_e_registrar():
    servidores = _ler_config()
    if not servidores:
        return

    try:
        import mcp  # noqa: F401 — só para checar se o pacote está instalado
    except ImportError:
        print("[MCP] pacote 'mcp' nao instalado (pip install mcp) — "
              "servidores configurados em mcp.json nao serao carregados.")
        return

    for nome_servidor, cfg in servidores.items():
        if not isinstance(cfg, dict) or cfg.get("ativo") is False:
            continue
        if not cfg.get("command"):
            continue

        try:
            tools_remotas = asyncio.run(
                asyncio.wait_for(_listar_tools_async(cfg), timeout=15))
        except Exception as e:
            print(f"[MCP] {nome_servidor}: falha ao conectar "
                  f"({type(e).__name__}: {e})")
            continue

        for t in tools_remotas:
            nome_local = _nome_seguro(f"mcp_{nome_servidor}_{t['name']}")
            TOOLS.append({"type": "function", "function": {
                "name": nome_local,
                "description": f"[MCP:{nome_servidor}] {t['description']}"[:1000],
                "parameters": t["inputSchema"],
            }})
            FERRAMENTAS[nome_local] = _fabrica_executor(cfg, t["name"])

        print(f"[MCP] {nome_servidor}: {len(tools_remotas)} "
              f"ferramenta(s) carregada(s)")


_descobrir_e_registrar()
