"""🚀 Fase de EXECUÇÃO do Chat Livre.

O Chat Livre (botão de planejamento reaproveitado) deixa o modelo conversar
CRU: zero system prompt, zero ferramentas. Quando o usuário confirma
("faça", "bora"...), o Jarvis entra AQUI.

A ideia (decidida com o usuário): em vez de depender de function-calling
nativo (que o KoboldCpp não expõe e modelos pequenos erram), o Jarvis:
  1. injeta o catálogo de ferramentas como TEXTO e pede um PLANO em JSON
     de passos ({"passos":[{"ferramenta":...,"argumentos":{...}}]});
  2. executa os passos deterministicamente, na ordem;
  3. resume o resultado em português pro usuário.

Se o plano vier vazio/inválido (comum em modelo de 8B), cai no FALLBACK
AGÊNTICO: o modelo vai pedindo uma ferramenta por vez escrevendo o JSON no
texto (o mesmo resgate que o agent.py já faz no loop normal), o Jarvis
executa e devolve o resultado, até o modelo escrever a resposta final.
"""

import json
import re

from config import MAX_ITERACOES
import llm
from tools import TOOLS, executar_ferramenta, filtrar_tools

_RE_THINK = re.compile(r"<think\b[^>]*>[\s\S]*?</think>", re.IGNORECASE)
_RE_THINK_SO_FECHA = re.compile(r"[\s\S]*?</think>", re.IGNORECASE)
_RE_THINK_SO_ABRE = re.compile(r"<think\b[^>]*>[\s\S]*$", re.IGNORECASE)


def _sem_pensamento(texto):
    if not texto:
        return ""
    t = _RE_THINK.sub("", texto)
    if " response" in t.lower():
        t = _RE_THINK_SO_FECHA.sub("", t)
    if " thinking" in t.lower():
        t = _RE_THINK_SO_ABRE.sub("", t)
    return t.strip()


def _catalogo(tarefa=None):
    """Lista as ferramentas disponíveis como texto simples, uma por linha,
    com o nome, os parâmetros e a 1ª linha da descrição. Usa a prefiltragem
    do próprio Jarvis (filtrar_tools) pra não estufar o prompt."""
    tools = TOOLS
    try:
        filtradas = filtrar_tools(tarefa) if tarefa else TOOLS
        if filtradas:
            tools = filtradas
    except Exception:
        pass

    linhas = []
    for t in tools:
        f = t.get("function", {})
        nome = f.get("name")
        if not nome:
            continue
        desc = (f.get("description") or "").strip().splitlines()
        desc = desc[0].strip() if desc else ""
        props = (f.get("parameters", {}) or {}).get("properties", {}) or {}
        assinatura = ", ".join(props.keys())
        linhas.append(f"- {nome}({assinatura}): {desc}")
    return "\n".join(linhas)


def _parse_plano(texto):
    """Extrai a lista de passos do texto do modelo, tolerando cerca de
    markdown e texto ao redor. Devolve [] se não achar um plano válido."""
    texto = _sem_pensamento(texto or "")
    if not texto:
        return []
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", texto)
    if m:
        texto = m.group(1).strip()
    i = texto.find("{")
    if i < 0:
        return []
    try:
        d, _ = json.JSONDecoder().raw_decode(texto, i)
    except Exception:
        return []
    if not isinstance(d, dict):
        return []
    passos = d.get("passos") or d.get("steps") or []
    if not isinstance(passos, list):
        return []
    saida = []
    for p in passos:
        if not isinstance(p, dict):
            continue
        nome = p.get("ferramenta") or p.get("name") or p.get("tool")
        args = (p.get("argumentos") or p.get("parameters")
                or p.get("arguments") or {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if nome and isinstance(args, dict):
            saida.append({"ferramenta": nome, "argumentos": args})
    return saida


def _pedir_plano(tarefa):
    cat = _catalogo(tarefa)
    prompt = (
        "Você é um planejador. Sua tarefa é montar um PLANO de execução "
        "para o pedido do usuário usando as ferramentas disponíveis.\n\n"
        "Responda SOMENTE com um JSON, sem explicação e sem markdown, no "
        'formato exato:\n{"passos":[{"ferramenta":"nome","argumentos":{...}}]}\n'
        'Se não houver nada a executar, responda {"passos":[]}.\n\n'
        "Use somente ferramentas que existem nesta lista:\n"
        f"{cat}\n\n"
        f"PEDIDO DO USUÁRIO: {tarefa}"
    )
    try:
        r = llm.chat([{"role": "user", "content": prompt}], tools=None)
    except Exception:
        return []
    return _parse_plano(r.get("content") or "")


def _executar_passos(passos, on_evento):
    resultados = []
    for p in passos:
        nome = p["ferramenta"]
        args = p["argumentos"]
        try:
            res = executar_ferramenta(nome, args)
        except Exception as e:
            res = f"ERRO ao executar {nome}: {e}"
        if on_evento:
            try:
                on_evento(nome, args, res)
            except Exception:
                pass
        resultados.append({"ferramenta": nome, "argumentos": args,
                           "resultado": res or ""})
    return resultados


def _resumir(tarefa, resultados, on_token=None):
    bloco = "\n\n".join(
        f"[{r['ferramenta']}] {json.dumps(r['argumentos'], ensure_ascii=False)}\n"
        f"{r['resultado'][:3000]}"
        for r in resultados
    )
    prompt = (
        "Abaixo estão os resultados REAIS das ferramentas executadas para o "
        "pedido. Responda ao usuário em português, de forma clara e "
        "objetiva, usando SOMENTE o que os resultados mostram. Não invente "
        "nada que não esteja ali.\n\n"
        f"PEDIDO: {tarefa}\n\nRESULTADOS:\n{bloco}"
    )
    try:
        r = llm.chat([{"role": "user", "content": prompt}], tools=None,
                     on_token=on_token)
    except Exception as e:
        return f"(falha ao resumir os resultados: {e})"
    return _sem_pensamento(r.get("content") or "")


def _loop_agentico(tarefa, historico, on_evento, cat):
    """Fallback: o modelo pede uma ferramenta por vez escrevendo o JSON no
    texto; o Jarvis executa e devolve o resultado, até o modelo terminar
    com uma resposta em prosa. Sem streaming (senão o JSON cru apareceria
    na tela enquanto o modelo 'pensa' a chamada)."""
    try:
        from agent import _extrair_tool_call_do_texto, _remover_pensamento
    except Exception:
        return "(fallback agêntico indisponível)"

    sistema = (
        "Você é Jarvis em modo EXECUÇÃO. Não peça permissão nem explique o "
        "que vai fazer: execute. Para usar uma ferramenta, escreva SOMENTE "
        'um JSON no formato {"name":"ferramenta","parameters":{...}} e nada '
        "mais. O resultado volta pra você e você decide o próximo passo. "
        "Quando a tarefa estiver concluída, responda em português com o "
        "resultado final, de forma clara e objetiva.\n\n"
        f"FERRAMENTAS DISPONÍVEIS:\n{cat}"
    )
    msgs = ([{"role": "system", "content": sistema}]
            + (historico or [])[-6:]
            + [{"role": "user", "content": tarefa or "(continuar)"}])

    for _ in range(MAX_ITERACOES):
        try:
            r = llm.chat(msgs, tools=None)
        except Exception as e:
            return f"(falha no fallback agêntico: {e})"
        msgs.append(r)
        tc = _extrair_tool_call_do_texto(r.get("content") or "")
        if not tc:
            return _remover_pensamento(r.get("content") or "")

        nome = tc["function"]["name"]
        args = tc["function"]["arguments"]
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        res = executar_ferramenta(nome, args)
        if on_evento:
            try:
                on_evento(nome, args, res)
            except Exception:
                pass
        msgs.append({"role": "tool", "name": nome, "content": res or ""})

    return "(limite de iterações atingido sem resposta final)"


def executar_livre(tarefa, historico=None, on_evento=None, on_token=None):
    """Ponto de entrada da fase de execução. Devolve o texto final pro
    usuário."""
    cat = _catalogo(tarefa)
    passos = _pedir_plano(tarefa)
    if passos:
        print(f"[EXECUCAO] plano com {len(passos)} passo(s): "
              f"{[p['ferramenta'] for p in passos]}")
        resultados = _executar_passos(passos, on_evento)
        if resultados:
            return _resumir(tarefa, resultados, on_token=on_token)
    # plano vazio/inválido (ou nada executado): fallback agêntico por texto
    print("[EXECUCAO] sem plano válido, usando fallback agêntico")
    return _loop_agentico(tarefa, historico, on_evento, cat)
