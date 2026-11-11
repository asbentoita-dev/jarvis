# 🔌 Plugin: n8n — dispara suas automações
# Este arquivo também serve de MODELO para criar novos plugins:
# defina TOOLS (descrição JSON) e FERRAMENTAS (nome -> função) e pronto.

import requests

N8N_URL = "http://localhost:5679"  # porta do seu container n8n


def chamar_n8n(fluxo, dados=""):
    """Dispara um fluxo do n8n pelo webhook. 'fluxo' é o nome do webhook."""
    try:
        import json as _json
        corpo = {}
        if dados:
            try:
                corpo = _json.loads(dados)
            except Exception:
                corpo = {"mensagem": dados}

        r = requests.post(
            f"{N8N_URL}/webhook/{fluxo}",
            json=corpo,
            timeout=30
        )

        if r.status_code == 404:
            return (f"ERRO: webhook '{fluxo}' nao existe no n8n. "
                    "Crie um fluxo com gatilho Webhook e use o nome dele.")

        return f"n8n respondeu (HTTP {r.status_code}): {r.text[:500]}"

    except requests.exceptions.ConnectionError:
        return "ERRO: n8n offline. Verifique o container no Docker (porta 5679)."
    except Exception as e:
        return f"ERRO no n8n: {e}"


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "chamar_n8n",
            "description": "Dispara uma automacao (fluxo) do n8n do usuario, como enviar notificacao, e-mail ou registrar dados. Informe o nome do webhook do fluxo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fluxo": {"type": "string", "description": "Nome do webhook do fluxo no n8n, ex: lembrete"},
                    "dados": {"type": "string", "description": "Dados em JSON ou texto para enviar ao fluxo (opcional)"}
                },
                "required": ["fluxo"]
            }
        }
    }
]

FERRAMENTAS = {
    "chamar_n8n": chamar_n8n,
}
