"""📖 Leitura de metadados REAIS de arquivos GGUF.

Antes disso o Jarvis adivinhava arquitetura/camadas pelo NOME do arquivo
(heurística frágil: um modelo 27B virava "7B / 32 camadas"). Aqui lemos os
metadados de verdade do GGUF com o pacote `gguf` (puro Python, sem
compilador) — arquitetura, número de camadas, contexto de treino, tamanho
do embedding, MoE (nº de experts e experts ativos) e os tipos de tensor
(quantização).

Usado para: ficha técnica na aba Model, cálculo de camadas de GPU
(--gpulayers) e exibição no seletor. Se o pacote `gguf` não estiver
disponível, degrada graciosamente (retorna só o tamanho do arquivo).
"""

import os

try:
    from gguf import GGUFReader
    _GGUF_OK = True
except Exception:
    _GGUF_OK = False


def _campo(reader, sufixo, padrao=None):
    """Acha um campo pelo sufixo (ex: '.block_count') em qualquer
    arquitetura, sem precisar saber o nome dela de antemão."""
    for k, f in reader.fields.items():
        if k.endswith(sufixo):
            try:
                return f.contents()
            except Exception:
                return padrao
    return padrao


def _int(v, padrao=None):
    try:
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        return int(v) if v is not None else padrao
    except Exception:
        return padrao


def ler_metadados(caminho):
    """Devolve um dict com os dados do GGUF (ou o mínimo, se falhar).

    Chaves: arquivo, nome, tamanho_bytes, tamanho_gb, arquitetura,
    n_camadas, ctx_treino, embedding, moe, n_experts, experts_ativos,
    quant, tipos_tensor, erro.
    """
    info = {
        "arquivo": caminho,
        "nome": os.path.basename(caminho),
        "tamanho_bytes": 0,
        "tamanho_gb": 0.0,
        "arquitetura": None,
        "n_camadas": None,
        "ctx_treino": None,
        "embedding": None,
        "moe": False,
        "n_experts": None,
        "experts_ativos": None,
        "quant": None,
        "tipos_tensor": None,
        "erro": None,
    }

    try:
        info["tamanho_bytes"] = os.path.getsize(caminho)
        info["tamanho_gb"] = round(info["tamanho_bytes"] / (1024 ** 3), 2)
    except Exception as e:
        info["erro"] = f"não consegui ler o tamanho: {e}"
        return info

    # quant pelo nome do arquivo (o rótulo mais visível pro usuário)
    nome = info["nome"].upper()
    for marca in ("MXFP4_MOE", "MXFP4", "IQ4_NL", "IQ3_XXS", "IQ2_XXS",
                  "Q8_0", "Q6_K", "Q5_K_M", "Q5_K_S", "Q5_1", "Q5_0",
                  "Q4_K_M", "Q4_K_S", "Q4_0", "Q3_K_L", "Q3_K_M", "Q3_K_S",
                  "Q2_K", "BF16", "F16"):
        if marca in nome:
            info["quant"] = marca
            break

    if not _GGUF_OK:
        info["erro"] = "pacote 'gguf' não instalado (pip install gguf)"
        return info

    try:
        reader = GGUFReader(caminho)
        info["arquitetura"] = _campo(reader, ".architecture")
        info["n_camadas"] = _int(_campo(reader, ".block_count"))
        info["ctx_treino"] = _int(_campo(reader, ".context_length"))
        info["embedding"] = _int(_campo(reader, ".embedding_length"))

        n_exp = _int(_campo(reader, ".expert_count"))
        usados = _int(_campo(reader, ".expert_used_count"))
        if n_exp and n_exp > 0:
            info["moe"] = True
            info["n_experts"] = n_exp
            info["experts_ativos"] = usados

        from collections import Counter
        tipos = Counter()
        for t in reader.tensors:
            try:
                tipos[t.tensor_type.name] += 1
            except Exception:
                pass
        info["tipos_tensor"] = dict(tipos)
    except Exception as e:
        info["erro"] = f"falha ao ler metadados: {e}"

    return info


def quant_principal(info):
    """Rótulo de quantização mais representativo (nome do arquivo tem
    prioridade; senão o tipo de tensor mais comum que não seja F32)."""
    if not info:
        return None
    if info.get("quant"):
        return info["quant"]
    tipos = info.get("tipos_tensor") or {}
    uteis = {k: v for k, v in tipos.items() if k not in ("F32", "F16", "BF16")}
    if uteis:
        return max(uteis, key=uteis.get)
    return None
