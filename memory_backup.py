import gzip
import json
import os
import threading
import time
import traceback
from datetime import datetime

from config import (
    CHROMA_HOST,
    CHROMA_PORT,
    BACKUP_MEMORIA_DIR,
    BACKUP_MEMORIA_INTERVALO_HORAS,
    BACKUP_MEMORIA_MAX_ARQUIVOS,
)
import backup_config

# 💾 Backup automático da memória de longo prazo (ChromaDB) do Jarvis.
#
# O Jarvis não guarda a memória em arquivo próprio — ele fala com um
# servidor ChromaDB separado, via API HTTP (ver memory.py). Por isso
# esse backup não copia uma pasta do disco: ele pede pra API TODAS as
# coleções e todos os itens (documentos, metadados e embeddings) e
# salva isso num arquivo .json.gz com carimbo de data/hora.
#
# A pasta de destino é configurável pelo usuário na interface (aba
# ⚙️ Configurações > 💾 Backup, atrás de senha — ver backup_config.py):
# se ele nunca mexeu, usa BACKUP_MEMORIA_DIR (padrão: "backups_memoria"
# na pasta do Jarvis, sobrevive a reinstalação/rebuild do .exe); se ele
# apontou pra outra pasta (ex: uma pasta do Google Drive/OneDrive, pra
# sincronizar entre PCs), usa essa.
#
# Roda sozinho em segundo plano (thread daemon), a cada
# BACKUP_MEMORIA_INTERVALO_HORAS, e mantém só os
# BACKUP_MEMORIA_MAX_ARQUIVOS mais recentes (rotação automática, pra
# não lotar o HD). Se o Chroma estiver offline na hora do backup, só
# registra o erro e tenta de novo no próximo ciclo — nunca derruba o
# Jarvis.

_ultimo_ok = None
_ultimo_erro = None
_ultimo_resultado = None
_thread_iniciada = False
_lock = threading.Lock()


def pasta_atual():
    """Pasta de backup em uso agora: a que o usuário configurou pela
    interface (aba Backup, com senha), ou a padrão do config.py se ele
    nunca mexeu nisso."""
    personalizada = backup_config.obter_pasta_destino()
    return personalizada or BACKUP_MEMORIA_DIR


def _coletar_dados_chroma():
    """Puxa todas as coleções e todos os itens via API HTTP do Chroma."""
    import chromadb

    client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    saida = {}

    for info in client.list_collections():
        nome = getattr(info, "name", None) or str(info)
        colecao = client.get_collection(nome)
        dados = colecao.get(include=["documents", "metadatas", "embeddings"])

        embeddings = dados.get("embeddings")
        if embeddings is None:
            embeddings = []
        embeddings_serializaveis = [
            (e.tolist() if hasattr(e, "tolist") else list(e))
            for e in embeddings
        ]

        saida[nome] = {
            "ids": dados.get("ids", []),
            "documents": dados.get("documents", []),
            "metadatas": dados.get("metadatas", []),
            "embeddings": embeddings_serializaveis,
        }

    return saida


def _rotacionar():
    """Mantém só os BACKUP_MEMORIA_MAX_ARQUIVOS backups mais recentes
    (na pasta em uso agora — ver pasta_atual())."""
    pasta = pasta_atual()
    try:
        arquivos = sorted(
            f for f in os.listdir(pasta)
            if f.startswith("memoria_") and f.endswith(".json.gz")
        )
        excesso = len(arquivos) - BACKUP_MEMORIA_MAX_ARQUIVOS
        for nome in arquivos[: max(excesso, 0)]:
            try:
                os.remove(os.path.join(pasta, nome))
            except OSError:
                pass
    except FileNotFoundError:
        pass


def fazer_backup():
    """Faz um backup completo agora, na pasta configurada (ver
    pasta_atual()). Pode ser chamada manualmente ou pelo agendador
    automático."""
    global _ultimo_ok, _ultimo_erro, _ultimo_resultado

    with _lock:
        try:
            pasta = pasta_atual()
            os.makedirs(pasta, exist_ok=True)
            colecoes = _coletar_dados_chroma()

            total_itens = sum(len(c.get("ids", [])) for c in colecoes.values())
            if total_itens == 0:
                _ultimo_ok = time.time()
                _ultimo_erro = None
                _ultimo_resultado = "memória vazia, nada para salvar (backup pulado)"
                return _ultimo_resultado

            carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
            caminho = os.path.join(pasta, f"memoria_{carimbo}.json.gz")

            payload = {
                "gerado_em": datetime.now().isoformat(),
                "host": CHROMA_HOST,
                "port": CHROMA_PORT,
                "colecoes": colecoes,
            }

            caminho_tmp = caminho + ".tmp"
            with gzip.open(caminho_tmp, "wt", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(caminho_tmp, caminho)  # escrita atômica

            _rotacionar()

            _ultimo_ok = time.time()
            _ultimo_erro = None
            _ultimo_resultado = f"backup salvo: {os.path.basename(caminho)} ({total_itens} itens)"
            return _ultimo_resultado

        except Exception as e:
            _ultimo_erro = str(e)
            _ultimo_resultado = f"erro no backup: {e}"
            print(f"[BACKUP MEMORIA] {_ultimo_resultado}")
            traceback.print_exc()
            return _ultimo_resultado


def status():
    """Resumo curto do estado do backup (pra log/depuração)."""
    partes = []
    if _ultimo_ok:
        partes.append("último OK: " + datetime.fromtimestamp(_ultimo_ok).strftime("%d/%m %H:%M"))
    if _ultimo_erro:
        partes.append("último erro: " + _ultimo_erro)
    if _ultimo_resultado:
        partes.append(_ultimo_resultado)
    if not partes:
        partes.append("backup ainda não rodou nesta sessão")
    return " | ".join(partes)


def _loop_agendador():
    # espera o servidor Chroma "acordar" de vez antes do 1º backup
    time.sleep(30)
    while True:
        resultado = fazer_backup()
        print(f"[BACKUP MEMORIA] {resultado}")
        time.sleep(BACKUP_MEMORIA_INTERVALO_HORAS * 3600)


def iniciar_agendador():
    """Chame uma vez na inicialização do Jarvis (bandeja.py/app.py/main.py).
    Idempotente — chamar mais de uma vez não cria threads duplicadas."""
    global _thread_iniciada
    with _lock:
        if _thread_iniciada:
            return
        _thread_iniciada = True
    threading.Thread(target=_loop_agendador, daemon=True, name="backup-memoria").start()


# ─────────────────────────────────────────
# RESTAURAR
# Todo backup precisa de um jeito de voltar — sem isso a cópia salva
# é só decoração. Usado pelo script restaurar_memoria.py (linha de
# comando, com confirmação) e reaproveitável por uma tela/rota futura
# se um dia quiser um botão "Restaurar" na interface.
# ─────────────────────────────────────────

def listar_backups():
    """Lista os backups disponíveis (na pasta em uso agora, ver
    pasta_atual()), mais recente primeiro."""
    pasta = pasta_atual()
    if not os.path.isdir(pasta):
        return []

    arquivos = sorted(
        (f for f in os.listdir(pasta)
         if f.startswith("memoria_") and f.endswith(".json.gz")),
        reverse=True,
    )

    info = []
    for nome in arquivos:
        caminho = os.path.join(pasta, nome)
        try:
            tamanho_kb = round(os.path.getsize(caminho) / 1024, 1)
        except OSError:
            tamanho_kb = 0
        info.append({"nome": nome, "caminho": caminho, "tamanho_kb": tamanho_kb})

    return info


def _carregar_backup(caminho):
    with gzip.open(caminho, "rt", encoding="utf-8") as f:
        return json.load(f)


def restaurar_backup(nome_arquivo=None, substituir=False):
    """Restaura um backup de volta pro ChromaDB.

    nome_arquivo: nome do arquivo dentro de BACKUP_MEMORIA_DIR (ver
      listar_backups()). Se None, usa o mais recente.
    substituir: False (padrão) = MESCLA — faz upsert por cima do que já
      existe na memória agora; itens com o mesmo id são sobrescritos
      pela versão do backup, o resto continua intacto. True = SUBSTITUI
      — apaga cada coleção inteira antes de restaurar, voltando
      exatamente pro estado exato do backup (perde tudo que foi gravado
      depois dele).

    Retorna uma string-resumo do que foi feito (ou do erro).
    """
    try:
        import chromadb

        backups = listar_backups()
        if not backups:
            return f"nenhum backup encontrado em {pasta_atual()}"

        if nome_arquivo is None:
            alvo = backups[0]
        else:
            alvo = next((b for b in backups if b["nome"] == nome_arquivo), None)
            if alvo is None:
                return f"backup '{nome_arquivo}' não encontrado"

        payload = _carregar_backup(alvo["caminho"])
        colecoes = payload.get("colecoes", {})
        if not colecoes:
            return "backup vazio, nada para restaurar"

        client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)

        resumo = []
        for nome_colecao, dados in colecoes.items():
            ids = dados.get("ids", [])
            if not ids:
                continue

            if substituir:
                try:
                    client.delete_collection(nome_colecao)
                except Exception:
                    pass  # coleção pode não existir ainda — segue o jogo

            colecao = client.get_or_create_collection(nome_colecao)
            colecao.upsert(
                ids=ids,
                documents=dados.get("documents", []),
                metadatas=dados.get("metadatas", []),
                embeddings=dados.get("embeddings", []),
            )
            resumo.append(f"{nome_colecao}: {len(ids)} itens")

        modo = "substituindo" if substituir else "mesclando"
        return f"restaurado de {alvo['nome']} ({modo}) — " + ", ".join(resumo)

    except Exception as e:
        traceback.print_exc()
        return f"erro ao restaurar: {e}"
