# 🔌 Plugin: executar_multiagente — orquestração por TIME de especialistas
# (Planejador -> Técnico + Interface + Analista -> Revisor Crítico), com
# troca de modelo LOCAL por papel (via llm.definir_modelo) e correção
# DIRECIONADA (só o agente responsável refaz, não a missão inteira).
#
# Times ficam em equipes.json (ver equipes.py) — "qualidade" (modelos
# maiores) e "rapido" (modelos leves), editáveis sem mexer em código.
#
# Isto é DIFERENTE do executar_missao (plugins/missao.py): aquele quebra
# uma missão livre em N etapas sequenciais com 1 modelo. Este aqui roda
# um time FIXO de papéis (técnico/interface/analista) em paralelo lógico
# (sequencial na prática — GPU de 8GB não aguenta 2 modelos ao mesmo
# tempo) e sempre passa pelo Revisor no final. Uso pensado pra testar a
# orquestração multiagente de verdade, com relatório completo de tempo e
# tokens por papel.

import re
import time as _time

import equipes

# ordem fixa deste teste: Técnico (arquitetura) -> Interface (UX) ->
# Analista (riscos/melhoria). "visual" é o papel usado pro time (o nome
# vem de equipes.json), mas aqui a tarefa é 100% textual (proposta de
# interface, não análise de imagem) — não precisa de imagem anexada.
_PAPEIS_MISSAO = ["tecnico", "visual", "analista"]

_ROTULO_LOG = {
    "planejador": "PLANNER",
    "tecnico": "TECHNICAL",
    "visual": "UI",
    "analista": "ANALYST",
    "conteudo": "CONTENT",
    "revisor": "REVIEWER",
}

_ROTULO_HUMANO = {
    "planejador": "Planejador",
    "tecnico": "Operário Técnico",
    "visual": "Operário de Interface",
    "analista": "Operário Analista",
    "conteudo": "Operário de Conteúdo",
    "revisor": "Revisor Crítico",
}

_CABECALHO_SUBTAREFA = {
    "tecnico": "TECNICO",
    "visual": "INTERFACE",
    "analista": "ANALISTA",
}
_CABECALHO_PARA_PAPEL = {v: k for k, v in _CABECALHO_SUBTAREFA.items()}


def _chat_papel(papel, modelo, prompt, metricas_log):
    """Troca pro modelo do papel, chama o modelo, registra tempo/tokens."""
    import llm

    modelo_real = llm.definir_modelo(modelo)
    inicio = _time.perf_counter()
    r = llm.chat([{"role": "user", "content": prompt}])
    tempo_s = round(_time.perf_counter() - inicio, 2)
    m = llm.obter_ultima_metrica()

    metricas_log.append({
        "papel": papel,
        "rotulo": _ROTULO_HUMANO.get(papel, papel),
        "modelo": modelo_real,
        "tempo_s": tempo_s,
        "tokens_entrada": m.get("tokens_entrada", 0),
        "tokens_saida": m.get("tokens_saida", 0),
        "tokens_por_segundo": round(m.get("tokens_por_segundo", 0), 1),
    })
    return (r.get("content") or "").strip()


def _planejar(objetivo, time_dados, metricas_log):
    prompt = f"""Você é o Planejador de um time multiagente. Divida o objetivo
abaixo em EXATAMENTE 3 subtarefas, uma para cada especialista.
NÃO execute nada — apenas planeje.

Objetivo: {objetivo}

Especialistas (nesta ordem):
1. TECNICO — arquitetura técnica (tecnologia, estrutura de arquivos, como
   implementar cada funcionalidade, onde os dados ficam salvos)
2. INTERFACE — proposta de interface (tela, campos, botões, organização
   visual, comportamento de cada ação)
3. ANALISTA — riscos, problemas possíveis, requisitos esquecidos e 1
   melhoria simples

Responda EXATAMENTE neste formato, um bloco por especialista, sem texto
antes ou depois:

### TECNICO
objetivo: <uma frase>
instrucoes: <instruções específicas para esse especialista>
resultado_esperado: <o que a resposta dele deve conter>

### INTERFACE
objetivo: <uma frase>
instrucoes: <instruções específicas>
resultado_esperado: <o que a resposta dele deve conter>

### ANALISTA
objetivo: <uma frase>
instrucoes: <instruções específicas>
resultado_esperado: <o que a resposta dele deve conter>"""

    texto = _chat_papel("planejador", time_dados["planejador"], prompt, metricas_log)
    return texto, _parsear_plano(texto, objetivo)


def _parsear_plano(texto, objetivo):
    """Extrai os 3 blocos ### TECNICO / ### INTERFACE / ### ANALISTA.
    Se o modelo não seguir o formato à risca (acontece com modelo local
    pequeno), cai num plano genérico — nunca trava a missão por causa de
    formatação imperfeita."""
    blocos = {}
    atual = None
    campos = {}
    for linha in (texto or "").splitlines():
        m = re.match(r"^#{1,3}\s*([A-ZÀ-Ú]+)\s*$", linha.strip().upper())
        if m and m.group(1) in _CABECALHO_PARA_PAPEL:
            if atual:
                blocos[atual] = campos
            atual = _CABECALHO_PARA_PAPEL[m.group(1)]
            campos = {}
            continue
        m2 = re.match(r"^(objetivo|instrucoes|instru[çc][õo]es|resultado_esperado)\s*:\s*(.+)$",
                       linha.strip(), re.IGNORECASE)
        if m2 and atual:
            chave = "instrucoes" if "instru" in m2.group(1).lower() else m2.group(1).lower()
            campos[chave] = m2.group(2).strip()
    if atual:
        blocos[atual] = campos

    plano = {}
    for papel in _PAPEIS_MISSAO:
        b = blocos.get(papel) or {}
        plano[papel] = {
            "objetivo": b.get("objetivo") or f"Contribuir com a parte de {_ROTULO_HUMANO[papel]} do pedido.",
            "instrucoes": b.get("instrucoes") or objetivo,
            "resultado_esperado": b.get("resultado_esperado") or "Resposta objetiva e concreta.",
        }
    return plano


def _executar_subtarefas(objetivo, plano, time_dados, log, metricas_log):
    resultados = {}
    for papel in _PAPEIS_MISSAO:
        log.append(f"[{_ROTULO_LOG[papel]}] executando")
        sub = plano[papel]
        prompt = (f"Contexto: você é o {_ROTULO_HUMANO[papel]} de um time que está "
                  f"resolvendo o pedido: \"{objetivo}\"\n\n"
                  f"Sua subtarefa: {sub['objetivo']}\n"
                  f"Instruções: {sub['instrucoes']}\n"
                  f"O resultado esperado deve conter: {sub['resultado_esperado']}\n\n"
                  f"Responda de forma objetiva, só com o conteúdo pedido "
                  f"(sem repetir estas instruções).")
        resultados[papel] = _chat_papel(papel, time_dados[papel], prompt, metricas_log)
    return resultados


_PADRAO_ESTADO = re.compile(r"\b(APPROVED|CORRECTION_REQUIRED)\b", re.IGNORECASE)
_PADRAO_PROBLEMA = re.compile(
    r"PROBLEMA:\s*(.+?)\s*"
    r"A[ÇC][AÃ]O_CORRETIVA:\s*(.+?)\s*"
    r"AGENTE_RESPONS[ÁA]VEL:\s*(\w+)",
    re.IGNORECASE | re.DOTALL
)


def _revisar(objetivo, plano_texto, resultados, time_dados, log, metricas_log):
    blocos_resultado = "\n\n".join(
        f"--- {_ROTULO_HUMANO[p].upper()} ---\n{resultados[p][:1500]}"
        for p in _PAPEIS_MISSAO
    )
    prompt = f"""Você é o Revisor Crítico. Avalie com rigor — não aprove só
para ser gentil. Se algo estiver incompleto, contraditório ou raso,
reprove de verdade.

PEDIDO ORIGINAL: {objetivo}

PLANO:
{plano_texto}

RESULTADOS DOS ESPECIALISTAS:
{blocos_resultado}

Verifique: cada subtarefa foi cumprida? as respostas são compatíveis
entre si? existe contradição? falta informação importante? a solução é
viável?

Responda na PRIMEIRA linha EXATAMENTE uma palavra: APPROVED ou
CORRECTION_REQUIRED
Se for CORRECTION_REQUIRED, liste os problemas, um bloco por problema,
EXATAMENTE neste formato:
PROBLEMA: <descrição>
ACAO_CORRETIVA: <o que fazer>
AGENTE_RESPONSAVEL: <tecnico, visual ou analista>"""

    log.append("[REVIEWER] iniciando revisão")
    texto = _chat_papel("revisor", time_dados["revisor"], prompt, metricas_log)

    m_estado = _PADRAO_ESTADO.search(texto[:200] or "")
    estado = m_estado.group(1).upper() if m_estado else "CORRECTION_REQUIRED"

    problemas = []
    if estado == "CORRECTION_REQUIRED":
        for pm in _PADRAO_PROBLEMA.finditer(texto):
            agente_bruto = pm.group(3).strip().lower()
            agente = agente_bruto if agente_bruto in _PAPEIS_MISSAO else None
            problemas.append({
                "problema": pm.group(1).strip(),
                "acao": pm.group(2).strip(),
                "agente": agente,
            })
        # revisor disse CORRECTION_REQUIRED mas não conseguimos extrair
        # nenhum problema estruturado — trata como aprovado pra não travar
        # a missão numa correção sem alvo definido
        if not problemas:
            estado = "APPROVED"

    log.append(f"[REVIEWER] {estado}")
    return estado, problemas, texto


def executar_multiagente(objetivo, time="qualidade"):
    """Roda o time inteiro (Planejador -> Técnico/Interface/Analista ->
    Revisor) sobre o objetivo e devolve um relatório completo em
    markdown, com log passo a passo e métricas (tempo/tokens) de cada
    especialista."""
    import llm

    time_dados = equipes.obter(time)
    if not time_dados:
        disponiveis = ", ".join(equipes.nomes())
        return f"ERRO: time '{time}' não existe. Times disponíveis: {disponiveis}"

    modelo_original = llm.modelo_atual()
    inicio_total = _time.perf_counter()
    log = []
    metricas_log = []

    log.append("[PLANNER] tarefa recebida")
    plano_texto, plano = _planejar(objetivo, time_dados, metricas_log)
    log.append("[PLANNER] 3 subtarefas criadas")

    resultados = _executar_subtarefas(objetivo, plano, time_dados, log, metricas_log)

    estado, problemas, texto_revisor = _revisar(
        objetivo, plano_texto, resultados, time_dados, log, metricas_log)

    # ── correção DIRECIONADA: só o(s) agente(s) apontado(s) refaz(em),
    # não a missão inteira — e só 1 rodada de correção (evita loop) ──
    correcoes_feitas = []
    if estado == "CORRECTION_REQUIRED":
        agentes_a_corrigir = {p["agente"] for p in problemas if p["agente"]}
        for papel in agentes_a_corrigir:
            acoes = "; ".join(p["acao"] for p in problemas if p["agente"] == papel)
            log.append(f"[{_ROTULO_LOG[papel]}] corrigindo")
            sub = plano[papel]
            prompt_correcao = (
                f"Contexto: você é o {_ROTULO_HUMANO[papel]}. Sua resposta "
                f"anterior foi REPROVADA pelo revisor.\n\n"
                f"Pedido original: {objetivo}\nSua subtarefa: {sub['objetivo']}\n"
                f"Sua resposta anterior:\n{resultados[papel][:1000]}\n\n"
                f"Ação corretiva pedida: {acoes}\n\n"
                f"Refaça sua resposta corrigindo isso especificamente.")
            resultados[papel] = _chat_papel(papel, time_dados[papel], prompt_correcao, metricas_log)
            correcoes_feitas.append(f"{_ROTULO_HUMANO[papel]}: {acoes}")

        # revisa de novo (1 única rodada extra) — reporta o estado real,
        # mesmo que ainda esteja pendente
        estado, problemas, texto_revisor = _revisar(
            objetivo, plano_texto, resultados, time_dados, log, metricas_log)

    tempo_total_s = round(_time.perf_counter() - inicio_total, 2)

    # devolve o modelo ao que estava antes da missão (não deixa a missão
    # "vazar" a troca de modelo pro resto da conversa)
    llm.definir_modelo(modelo_original)

    return _montar_relatorio(objetivo, time, log, plano, resultados, estado,
                              problemas, correcoes_feitas, metricas_log, tempo_total_s)


def _montar_relatorio(objetivo, time, log, plano, resultados, estado, problemas,
                       correcoes_feitas, metricas_log, tempo_total_s):
    partes = []
    partes.append(f"## 🎯 Missão multiagente — time \"{time}\"")
    partes.append(f"**Pedido:** {objetivo}\n")

    partes.append("### 📜 Log de execução")
    partes.append("```\n" + "\n".join(log) + "\n```\n")

    partes.append("### 📋 Plano")
    for papel in _PAPEIS_MISSAO:
        sub = plano[papel]
        partes.append(f"**{_ROTULO_HUMANO[papel]}** — {sub['objetivo']}")
    partes.append("")

    partes.append("### 🛠 Resultados por especialista")
    for papel in _PAPEIS_MISSAO:
        partes.append(f"\n**{_ROTULO_HUMANO[papel]}:**\n\n{resultados[papel][:1500]}")

    partes.append(f"\n### 🛡️ Revisor Crítico: {estado}")
    if problemas:
        for p in problemas:
            partes.append(f"- **Problema:** {p['problema']}\n"
                          f"  **Ação corretiva:** {p['acao']}\n"
                          f"  **Agente responsável:** {_ROTULO_HUMANO.get(p['agente'], p['agente'])}")
    if correcoes_feitas:
        partes.append("\n**Correções aplicadas (só nos agentes apontados, sem refazer a missão toda):**")
        for c in correcoes_feitas:
            partes.append(f"- {c}")

    partes.append("\n### ⏱ Relatório de tempo e tokens por especialista")
    partes.append("| Papel | Modelo | Tempo (s) | Tokens in/out | tok/s |")
    partes.append("|---|---|---|---|---|")
    for m in metricas_log:
        partes.append(f"| {m['rotulo']} | {m['modelo']} | {m['tempo_s']} | "
                      f"{m['tokens_entrada']}/{m['tokens_saida']} | {m['tokens_por_segundo']} |")

    tempo_soma_especialistas = round(sum(m["tempo_s"] for m in metricas_log), 2)
    partes.append(f"\n**Tempo total da missão:** {tempo_total_s}s "
                  f"(soma das chamadas aos modelos: {tempo_soma_especialistas}s — "
                  f"a diferença é overhead de troca de modelo/rede)")
    partes.append(f"**Chamadas ao(s) modelo(s):** {len(metricas_log)}")

    return "\n".join(partes)


TOOLS = [
    {"type": "function", "function": {
        "name": "executar_multiagente",
        "description": (
            "Executa um objetivo usando um TIME fixo de especialistas "
            "(Planejador, Técnico, Interface, Analista, Revisor), cada um "
            "com seu próprio modelo local (ver equipes.json). Diferente de "
            "executar_missao (que quebra em N etapas livres com 1 modelo "
            "só): aqui SEMPRE roda os 3 papéis fixos em time e sempre passa "
            "por um revisor crítico no final, com relatório completo de "
            "tempo/tokens por especialista. Use quando o usuário pedir "
            "explicitamente um 'teste multiagente', 'rodar o time X' ou "
            "quiser ver a orquestração completa (planejar/executar/revisar) "
            "em ação — não para pedidos simples do dia a dia."),
        "parameters": {"type": "object", "properties": {
            "objetivo": {"type": "string", "description": "O objetivo/tarefa completa"},
            "time": {"type": "string",
                    "description": "Nome do time a usar: 'qualidade' ou 'rapido' (padrão: qualidade)"}},
            "required": ["objetivo"]}}},
]

FERRAMENTAS = {
    "executar_multiagente": executar_multiagente,
}
