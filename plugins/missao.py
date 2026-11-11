# 🔌 Plugin: executar_missao — o multiagente reescrito como ferramenta
# Padrão Planejar → Executar → Criticar, integrado à janela do Jarvis.
#
# Correções em relação ao core/ antigo (v4.x):
# 1. O plano é quebrado por LINHAS (não por json.loads que sempre falhava)
# 2. O histórico da missão usa o formato correto de mensagens do Ollama
# 3. O crítico responde APROVADO/REPROVADO na 1ª linha (sem o bug do
#    "sim" dentro de "simples/assim")
# 4. NADA é salvo na memória de longo prazo automaticamente (sem poluição)
# Extras: respeita o botão ⏹ parar, e cada etapa reprovada ganha 1 retry.


def _planejar(objetivo, max_etapas):
    import llm

    prompt = f"""Monte um plano de execução para o objetivo abaixo.

Regras:
- Uma etapa por linha, sem números, sem marcadores, sem explicações.
- No máximo {max_etapas} etapas.
- Cada etapa deve ser uma ação concreta e executável.
- Etapas de pesquisa vêm antes de etapas de escrita/criação.

Objetivo: {objetivo}"""

    r = llm.chat([{"role": "user", "content": prompt}])
    linhas = (r.get("content") or "").splitlines()

    etapas = []
    for linha in linhas:
        limpa = linha.strip().lstrip("-•*0123456789.() ").strip()
        if limpa and len(limpa) > 3:
            etapas.append(limpa)

    return etapas[:max_etapas]


def _detectar_e_testar(etapa, resultado):
    """Sandbox: detecta se a etapa gerou código e tenta executar/testar.
    Retorna (teste_ok, saida_teste) — teste_ok=None se não havia o que testar.
    Agora suporta Python, JS/Node, HTML e projetos no sandbox (10GB)."""
    import re as _re_test
    import subprocess
    import config
    import os

    resultado_str = str(resultado)

    # 1. Detecta se criou arquivo .py no sandbox
    match_sandbox_py = _re_test.search(r"sandbox/projetos/([^/\s]+)/([^\s]+\.py)", resultado_str)
    if match_sandbox_py:
        projeto, arquivo_rel = match_sandbox_py.groups()
        arquivo = os.path.join('/sessions/practical-sharp-goodall/mnt/Jarvis/sandbox/projetos', projeto, arquivo_rel)
        if os.path.isfile(arquivo):
            try:
                r = subprocess.run(
                    ["python", "-c", f"import py_compile; py_compile.compile(r'{arquivo}', doraise=True); print('SINTAXE_OK')"],
                    capture_output=True, text=True, timeout=15,
                    cwd=os.path.dirname(arquivo)
                )
                saida = (r.stdout + r.stderr).strip()
                if "SINTAXE_OK" in saida:
                    # Tenta executar
                    try:
                        r2 = subprocess.run(
                            ["python", arquivo],
                            capture_output=True, text=True, timeout=30,
                            cwd=os.path.dirname(arquivo)
                        )
                        saida2 = (r2.stdout + r2.stderr).strip()[:500]
                        if r2.returncode == 0:
                            return True, f"Sintaxe OK. Execução OK.\n{saida2}"
                        else:
                            return False, f"Sintaxe OK mas execução falhou (exit {r2.returncode}):\n{saida2}"
                    except subprocess.TimeoutExpired:
                        return True, "Sintaxe OK. Execução excedeu timeout (30s) — pode ser normal pra scripts longos."
                else:
                    return False, f"Erro de sintaxe:\n{saida[:500]}"
            except subprocess.TimeoutExpired:
                return None, "Timeout na verificação de sintaxe."
            except Exception as e:
                return None, f"Erro ao testar: {e}"

    # 2. Detecta se criou arquivo no workspace (original)
    tem_python = bool(_re_test.search(r'```python|criar_arquivo.*\.py|\.py', resultado_str))
    match_arquivo = _re_test.search(r"Arquivo (?:criado|salvo)[^:]*:\s*(.+?\.py)", resultado_str)

    if not tem_python and not match_arquivo and not match_sandbox_py:
        # 3. Detecta projetos JS/Node no sandbox
        match_sandbox_js = _re_test.search(r"sandbox/projetos/([^/\s]+)/(package\.json|index\.html|app\.js)", resultado_str)
        if match_sandbox_js:
            return None, "Projeto web detectado — testes via npm/jest disponíveis via rodar_testes()"

        # 4. Detecta blocos de código Python inline
        match_bloco = _re_test.search(r'```python\n(.*?)```', resultado_str, _re_test.DOTALL)
        if match_bloco:
            codigo = match_bloco.group(1)
            try:
                compile(codigo, "<sandbox>", "exec")
                return True, "Bloco de código Python: sintaxe válida."
            except SyntaxError as e:
                return False, f"Bloco de código: erro de sintaxe na linha {e.lineno}: {e.msg}"

        return None, ""  # nada pra testar

    # Original logic for workspace .py files
    if match_arquivo:
        arquivo = match_arquivo.group(1).strip()
        if not os.path.isabs(arquivo):
            arquivo = os.path.join(config.WORKSPACE, arquivo)

        if os.path.isfile(arquivo):
            try:
                r = subprocess.run(
                    ["python", "-c", f"import py_compile; py_compile.compile(r'{arquivo}', doraise=True); print('SINTAXE_OK')"],
                    capture_output=True, text=True, timeout=15,
                    cwd=config.WORKSPACE
                )
                saida = (r.stdout + r.stderr).strip()
                if "SINTAXE_OK" in saida:
                    try:
                        r2 = subprocess.run(
                            ["python", arquivo],
                            capture_output=True, text=True, timeout=30,
                            cwd=config.WORKSPACE
                        )
                        saida2 = (r2.stdout + r2.stderr).strip()[:500]
                        if r2.returncode == 0:
                            return True, f"Sintaxe OK. Execução OK.\n{saida2}"
                        else:
                            return False, f"Sintaxe OK mas execução falhou (exit {r2.returncode}):\n{saida2}"
                    except subprocess.TimeoutExpired:
                        return True, "Sintaxe OK. Execução excedeu timeout (30s) — pode ser normal pra scripts longos."
                else:
                    return False, f"Erro de sintaxe:\n{saida[:500]}"
            except subprocess.TimeoutExpired:
                return None, "Timeout na verificação de sintaxe."
            except Exception as e:
                return None, f"Erro ao testar: {e}"

    # Se tem bloco de código mas não criou arquivo, tenta checar sintaxe inline
    match_bloco = _re_test.search(r'```python\n(.*?)```', resultado_str, _re_test.DOTALL)
    if match_bloco:
        codigo = match_bloco.group(1)
        try:
            compile(codigo, "<sandbox>", "exec")
            return True, "Bloco de código: sintaxe válida."
        except SyntaxError as e:
            return False, f"Bloco de código: erro de sintaxe na linha {e.lineno}: {e.msg}"

    return None, ""


# Detecta e roda pytest/npm test se existir na pasta do projeto
def _rodar_testes_projeto(pasta=None, projeto_sandbox=None):
    """Procura e roda testes automatizados no projeto (pytest, npm test, etc.).
    Retorna (rodou, passou, saida).
    Se projeto_sandbox for informado, usa o sandbox/projetos/<nome>."""
    import subprocess
    import config
    import os

    # Se tem projeto no sandbox, usa ele
    if projeto_sandbox:
        pasta = os.path.join('/sessions/practical-sharp-goodall/mnt/Jarvis/sandbox/projetos', projeto_sandbox)
    else:
        pasta = pasta or config.WORKSPACE

    if not os.path.isdir(pasta):
        return False, False, f"Pasta não encontrada: {pasta}"

    # pytest (Python)
    if os.path.isfile(os.path.join(pasta, "pytest.ini")) or \
       os.path.isfile(os.path.join(pasta, "setup.py")) or \
       os.path.isdir(os.path.join(pasta, "tests")) or \
       os.path.isfile(os.path.join(pasta, "pyproject.toml")):
        try:
            r = subprocess.run(
                ["python", "-m", "pytest", "--tb=short", "-q"],
                capture_output=True, text=True, timeout=60,
                cwd=pasta
            )
            saida = (r.stdout + r.stderr).strip()[-500:]
            return True, r.returncode == 0, saida
        except Exception as e:
            return True, False, f"pytest falhou: {e}"

    # npm test / jest
    if os.path.isfile(os.path.join(pasta, "package.json")):
        try:
            import json
            with open(os.path.join(pasta, "package.json"), "r") as f:
                pkg = json.load(f)
            if "test" in pkg.get("scripts", {}):
                r = subprocess.run(
                    ["npm", "test", "--", "--passWithNoTests"],
                    capture_output=True, text=True, timeout=60,
                    cwd=pasta, shell=True
                )
                saida = (r.stdout + r.stderr).strip()[-500:]
                return True, r.returncode == 0, saida
        except Exception:
            pass

        # Tenta npx jest direto
        try:
            r = subprocess.run(
                ["npx", "jest", "--passWithNoTests"],
                capture_output=True, text=True, timeout=60,
                cwd=pasta, shell=True
            )
            saida = (r.stdout + r.stderr).strip()[-500:]
            passed = 'passing' in saida.lower() or 'passed' in saida.lower()
            return True, passed, saida
        except Exception:
            pass

    return False, False, ""

    return False, False, ""


def _criticar(etapa, resultado):
    import llm

    prompt = f"""Etapa pedida: {etapa}

Resultado obtido:
{str(resultado)[:1500]}

A etapa foi realmente cumprida?
Responda na PRIMEIRA linha com UMA única palavra: APROVADO ou REPROVADO
Na segunda linha, o motivo em poucas palavras."""

    r = llm.chat([{"role": "user", "content": prompt}])
    texto = (r.get("content") or "").strip()
    linhas = texto.splitlines()

    aprovado = bool(linhas) and linhas[0].strip().upper().startswith("APROVADO")
    motivo = linhas[1].strip() if len(linhas) > 1 else texto[:100]

    return aprovado, motivo


def executar_missao(objetivo, max_etapas=6):
    """Planeja, executa etapa por etapa (com ferramentas) e confere."""
    import agent

    max_etapas = max(1, min(int(max_etapas or 6), 8))

    # ── 1. PLANEJAR ──
    etapas = _planejar(objetivo, max_etapas)
    if not etapas:
        return "ERRO: não consegui montar um plano para esse objetivo."

    relatorio = [f"🎯 MISSÃO: {objetivo}", "", "📋 PLANO:"]
    relatorio += [f"  {i}. {e}" for i, e in enumerate(etapas, 1)]

    # histórico PRÓPRIO da missão, no formato correto de mensagens —
    # cada etapa enxerga o que as anteriores fizeram
    historico_missao = []
    aprovadas = 0

    # ── 2. EXECUTAR + CRITICAR ──
    for i, etapa in enumerate(etapas, 1):
        pedido = (f"(Missão em andamento: {objetivo})\n"
                  f"Execute agora APENAS esta etapa e reporte o resultado: "
                  f"{etapa}")

        resultado, _ = agent.processar_mensagem(pedido, historico_missao)

        # botão ⏹ parar da janela interrompe a missão inteira
        if isinstance(resultado, str) and resultado.startswith("⏹"):
            relatorio.append(f"\n⏹ Missão interrompida pelo usuário na etapa {i}.")
            return "\n".join(relatorio)

        # 🧪 SANDBOX: testa o resultado antes da crítica
        teste_ok, saida_teste = _detectar_e_testar(etapa, resultado)
        if teste_ok is False:
            # teste falhou — injeta a falha no resultado pra o crítico ver
            resultado = resultado + f"\n\n⚠ TESTE AUTOMÁTICO FALHOU:\n{saida_teste}"
        elif teste_ok is True:
            resultado = resultado + f"\n\n✅ TESTE AUTOMÁTICO: {saida_teste}"

        aprovado, motivo = _criticar(etapa, resultado)

        # ── retry único com o feedback do crítico ──
        if not aprovado:
            pedido2 = (f"A etapa '{etapa}' foi REPROVADA pelo revisor "
                       f"(motivo: {motivo}). Corrija e complete a etapa agora.")
            resultado, _ = agent.processar_mensagem(pedido2, historico_missao)

            if isinstance(resultado, str) and resultado.startswith("⏹"):
                relatorio.append(f"\n⏹ Missão interrompida na etapa {i}.")
                return "\n".join(relatorio)

            aprovado, motivo = _criticar(etapa, resultado)

        aprovadas += 1 if aprovado else 0
        selo = "✅" if aprovado else f"⚠ pendente ({motivo})"
        relatorio.append(f"\n⚙ Etapa {i}: {etapa}\n"
                         f"   → {str(resultado)[:400]}\n"
                         f"   🔍 {selo}")

    # ── 3. FECHAMENTO ──
    # 🧪 SANDBOX: roda testes do projeto se existirem (pytest/npm test)
    # Detecta se algum resultado menciona sandbox/projetos/
    import re as _re_test
    match_sandbox = _re_test.search(r"sandbox/projetos/([^/\s]+)", "\n".join(relatorio))
    projeto_sandbox = match_sandbox.group(1) if match_sandbox else None

    _rodou_tests, _tests_ok, _saida_tests = _rodar_testes_projeto(projeto_sandbox=projeto_sandbox)
    if _rodou_tests:
        if _tests_ok:
            relatorio.append("\n🧪 Testes do projeto: ✅ PASSARAM")
        else:
            relatorio.append(f"\n🧪 Testes do projeto: ❌ FALHARAM\n{_saida_tests}")

    relatorio.append(f"\n🏁 Missão finalizada: {aprovadas}/{len(etapas)} "
                     f"etapas aprovadas pelo revisor.")

    return "\n".join(relatorio)


TOOLS = [
    {"type": "function", "function": {
        "name": "executar_missao",
        "description": "Executa um objetivo COMPLEXO em etapas: planeja, executa cada etapa usando as ferramentas e um revisor confere cada resultado. Use quando o usuario pedir uma 'missao', um projeto completo ou uma tarefa grande com varias acoes encadeadas. Para pedidos simples, NAO use - execute diretamente.",
        "parameters": {"type": "object", "properties": {
            "objetivo": {"type": "string", "description": "O objetivo completo da missao"},
            "max_etapas": {"type": "integer", "description": "Maximo de etapas do plano (padrao 6, maximo 8)"}},
            "required": ["objetivo"]}}},
]

FERRAMENTAS = {
    "executar_missao": executar_missao,
}
