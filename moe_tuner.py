"""
🧪 Auto-ajuste de modelos MoE (mixture-of-experts) grandes — Qwen3.6-35B-A3B,
Mixtral, DeepSeek-MoE e parecidos: modelos com um total de parâmetros bem
maior do que a VRAM disponível, mas que só "ativam" uma fatia pequena por
token (ex: 35B no total, ~3B ativos).

Em vez de catar manualmente o número de camadas na GPU testando um por um
(como fizemos na mão com o Qwen3.5-9B em 25/08/2026 — ver o histórico da
conversa), essa rotina faz isso sozinha: testa alguns valores de "camadas
na GPU", de cima pra baixo, com um prompt curto e fixo, e guarda o primeiro
que funcionar sem travar nem estourar VRAM — sem você precisar decorar
nada. Vira um "processo que deu certo" reaproveitável pra qualquer modelo
grande novo que você baixar (ver preferência do usuário: linha de
produção).

Resultado fica salvo em moe_presets.json, por nome de modelo — na próxima
vez que você for usar esse mesmo modelo, o Jarvis já mostra a configuração
que funcionou, em vez de você ter que testar tudo de novo.

⚠️ Limitação honesta: isso NÃO é a técnica de "expert fica no SSD e é
streamado sob demanda a cada token" que apareceu no texto que você leu —
essa técnica é implementada dentro do motor de inferência (llama.cpp/
Ollama), não dá pra fazer de fora via HTTP. O que ESSA rotina faz é achar,
por tentativa e erro automatizado, o melhor equilíbrio de camadas
GPU/CPU que o Ollama já suporta hoje pro seu hardware — o mesmo ajuste
manual de sempre, só que automatizado e memorizado por modelo.
"""
import json
import os
import re
import threading
import time
import uuid

import llm

_PASTA = os.path.dirname(os.path.abspath(__file__))
ARQUIVO_PRESETS = os.path.join(_PASTA, "moe_presets.json")

# fallback por nome, só usado quando o /api/show não trouxe expert_count
# (modelo antigo ou o Ollama dessa versão não expõe esse metadado)
_PADRAO_MOE_NOME = re.compile(r"-a\d+b\b|moe|mixtral", re.IGNORECASE)

PROMPT_TESTE = "Responda em uma frase curta: qual a capital do Brasil?"

_JOBS = {}
_JOBS_LOCK = threading.Lock()


def eh_moe(nome_modelo):
    """Confere primeiro pela ficha técnica real do modelo (expert_count no
    GGUF — confiável); só cai pro nome se o Ollama não expôs esse campo."""
    try:
        info = llm.informacoes_modelo(nome_modelo)
        if (info or {}).get("expert_count"):
            return True
    except Exception:
        pass
    return bool(_PADRAO_MOE_NOME.search(nome_modelo or ""))


def _ler_presets():
    if not os.path.exists(ARQUIVO_PRESETS):
        return {}
    try:
        with open(ARQUIVO_PRESETS, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _salvar_presets(presets):
    try:
        with open(ARQUIVO_PRESETS, "w", encoding="utf-8") as f:
            json.dump(presets, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def obter_preset(nome_modelo):
    return _ler_presets().get(nome_modelo)


def _testar_camadas(nome_modelo, num_gpu, num_ctx, timeout_s=90):
    """1 chamada curta com esse nº de camadas — inclui o tempo de
    recarregar o modelo do zero com essa config (mudar num_gpu força o
    Ollama a descarregar e recarregar o modelo, então isso NÃO é rápido
    pra modelo grande — timeout generoso de propósito)."""
    modelo_antes = llm.modelo_atual()
    try:
        llm.definir_modelo(nome_modelo)
        inicio = time.perf_counter()
        llm.chat(
            [{"role": "user", "content": PROMPT_TESTE}],
            opcoes_extra={"num_gpu": num_gpu, "num_ctx": num_ctx},
        )
        tempo = time.perf_counter() - inicio
        metrica = llm.obter_ultima_metrica()
        if tempo > timeout_s:
            return False, 0, f"demorou {tempo:.0f}s (> {timeout_s}s) — provavelmente travando"
        return True, metrica.get("tokens_por_segundo", 0), None
    except Exception as e:
        return False, 0, str(e)
    finally:
        try:
            llm.definir_modelo(modelo_antes)
        except Exception:
            pass


def _rodar_auto_tune(job_id, nome_modelo, camadas_totais, num_ctx):
    job = _JOBS[job_id]

    def log(msg):
        job["mensagens"].append(msg)

    # de cima pra baixo: tenta usar o máximo de GPU possível primeiro,
    # só desce se travar. Não precisa achar o ÓTIMO absoluto, só o
    # primeiro que funciona sem travar — é o suficiente pra sair do
    # zero-a-zero manual.
    candidatos = sorted({
        camadas_totais,
        max(1, round(camadas_totais * 0.8)),
        max(1, round(camadas_totais * 0.6)),
        max(1, round(camadas_totais * 0.4)),
        max(1, round(camadas_totais * 0.25)),
        max(1, round(camadas_totais * 0.10)),
    }, reverse=True)

    tentativas = []
    for candidato in candidatos:
        log(f"Testando {candidato}/{camadas_totais} camadas na GPU…")
        ok, tok_s, erro = _testar_camadas(nome_modelo, candidato, num_ctx)
        tentativas.append({"camadas": candidato, "ok": ok,
                            "tok_s": round(tok_s, 1), "erro": erro})
        if ok:
            log(f"✅ {candidato} camadas funcionou ({tok_s:.1f} tok/s) — salvando como padrão desse modelo.")
            preset = {
                "num_gpu": candidato,
                "num_ctx": num_ctx,
                "tokens_por_segundo": round(tok_s, 1),
                "testado_em": time.strftime("%Y-%m-%d %H:%M"),
                "camadas_totais": camadas_totais,
            }
            presets = _ler_presets()
            presets[nome_modelo] = preset
            _salvar_presets(presets)
            job["status"] = "ok"
            job["resultado"] = {"sucesso": True, "preset": preset, "tentativas": tentativas}
            return
        log(f"❌ {candidato} camadas falhou ({erro or 'sem resposta'}) — tentando menos.")

    log("Nenhuma quantidade de camadas testada funcionou — pode ser VRAM insuficiente mesmo com quase tudo na CPU, ou timeout curto demais pra esse hardware.")
    job["status"] = "ok"
    job["resultado"] = {"sucesso": False, "preset": None, "tentativas": tentativas}


def iniciar_auto_tune(nome_modelo, camadas_totais, num_ctx=8192):
    """Dispara o auto-ajuste em background e devolve um job_id pra
    acompanhar o progresso via status_auto_tune()."""
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "rodando", "mensagens": [], "resultado": None}
    thread = threading.Thread(
        target=_rodar_auto_tune,
        args=(job_id, nome_modelo, camadas_totais, num_ctx),
        daemon=True,
    )
    thread.start()
    return job_id


def status_auto_tune(job_id):
    return _JOBS.get(job_id)
