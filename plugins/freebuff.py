# 🔌 Plugin: delegar_codigo — entrega de bastão para o Freebuff
# O Freebuff (agente de código gratuito) é só interativo, então o fluxo é:
# Jarvis cria a pasta do projeto + BRIEF.md com a tarefa detalhada,
# e abre o Freebuff já dentro da pasta, numa janela de terminal.
# O usuário só digita lá: "leia o BRIEF.md e execute a tarefa".
#
# Requisitos (uma vez): npm i -g freebuff   e   freebuff login

import os
import re
import subprocess

from config import WORKSPACE


def delegar_codigo(nome_projeto, tarefa):
    """Prepara um projeto e abre o Freebuff nele para construir o código."""

    # nome de pasta seguro
    nome_limpo = re.sub(r"[^a-zA-Z0-9_-]", "_", str(nome_projeto).strip())[:40]
    if not nome_limpo:
        return "ERRO: nome de projeto invalido."

    base = os.path.join(WORKSPACE, "projetos", nome_limpo)
    os.makedirs(base, exist_ok=True)

    brief = os.path.join(base, "BRIEF.md")
    with open(brief, "w", encoding="utf-8") as f:
        f.write(f"""# Projeto: {nome_limpo}

## Tarefa

{tarefa}

## Instruções

- Execute a tarefa acima criando os arquivos NESTA pasta.
- Ao final, crie um arquivo COMO_USAR.md explicando como rodar.
""")

    try:
        subprocess.Popen(
            f'start "Freebuff - {nome_limpo}" cmd /k freebuff --cwd "{base}"',
            shell=True
        )
    except Exception as e:
        return (f"BRIEF criado em {brief}, mas nao consegui abrir o Freebuff "
                f"({e}). Abra manualmente: freebuff --cwd \"{base}\"")

    return (f"Projeto preparado: {base}\\BRIEF.md\n"
            f"Abri o Freebuff nessa pasta em uma nova janela de terminal.\n"
            f"DIGA AO USUARIO: na janela do Freebuff, digite "
            f"'leia o arquivo BRIEF.md e execute a tarefa'. "
            f"Na primeira vez, pode pedir login (comando: freebuff login).")


TOOLS = [
    {"type": "function", "function": {
        "name": "delegar_codigo",
        "description": "Delega a construcao de um APP/SITE/SISTEMA COMPLETO ao Freebuff (agente de codigo externo): prepara a pasta do projeto com um briefing e abre o Freebuff nela. Use quando o usuario pedir para construir um projeto de software grande ou disser 'delega para o freebuff'. Para scripts simples, use criar_arquivo.",
        "parameters": {"type": "object", "properties": {
            "nome_projeto": {"type": "string", "description": "Nome curto do projeto, ex: site-receitas"},
            "tarefa": {"type": "string", "description": "Descricao COMPLETA e detalhada do que construir (o briefing)"}},
            "required": ["nome_projeto", "tarefa"]}}},
]

FERRAMENTAS = {
    "delegar_codigo": delegar_codigo,
}
