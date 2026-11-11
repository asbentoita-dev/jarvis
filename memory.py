import hashlib
import time

from config import CHROMA_HOST, CHROMA_PORT, FAXINA_DIAS, MAX_MEMORIAS

# 🧠 memória vetorial (ChromaDB) — a função que o "Claude" não tem nativamente.
# Import e conexão preguiçosos: se o ChromaDB estiver offline,
# o Jarvis continua funcionando (só sem memória de longo prazo).

_collection = None
_erro = None


def _conectar():
    global _collection, _erro

    if _collection is not None:
        return _collection

    try:
        import chromadb
        client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
        _collection = client.get_or_create_collection("jarvis")
        return _collection
    except Exception as e:
        _erro = str(e)
        return None


def _resetar_conexao():
    """Se a coleção foi apagada/recriada por fora (ex: zerar_memoria.py),
    derruba o cache para reconectar na próxima chamada."""
    global _collection
    _collection = None


def _com_reconexao(operacao):
    """Executa a operação; se a coleção sumiu, reconecta e tenta 1x de novo."""
    try:
        return operacao()
    except Exception as e:
        if "does not exist" in str(e):
            _resetar_conexao()
            if _conectar() is not None:
                return operacao()
        raise


def salvar_memoria(texto, categoria="general"):
    if _conectar() is None:
        return False

    def op():
        col = _conectar()
        # id estável (sha256), não muda entre execuções como o hash() nativo
        id_estavel = hashlib.sha256(texto.encode("utf-8")).hexdigest()[:32]
        col.add(
            ids=[id_estavel],
            documents=[texto],
            metadatas=[{"categoria": categoria, "data": time.time()}]
        )
        return True

    try:
        return _com_reconexao(op)
    except Exception as e:
        print(f"[MEMORIA] erro ao salvar: {e}")
        return False


def buscar_memoria(query, categoria=None):
    col = _conectar()
    if col is None:
        return []

    def op():
        result = _conectar().query(
            query_texts=[query],
            n_results=5,
            where={"categoria": categoria} if categoria else None
        )
        return result.get("documents", [[]])[0]

    try:
        return _com_reconexao(op)
    except Exception as e:
        print(f"[MEMORIA] erro ao buscar: {e}")
        return []


def apagar_memorias(consulta):
    """Apaga as memórias mais parecidas com a consulta. Retorna o que apagou."""
    col = _conectar()
    if col is None:
        return "ERRO: memoria offline."

    def op():
        col2 = _conectar()
        result = col2.query(query_texts=[consulta], n_results=10)
        ids = result.get("ids", [[]])[0]
        docs = result.get("documents", [[]])[0]

        if not ids:
            return "Nenhuma memoria parecida encontrada."

        col2.delete(ids=ids)

        lista = "\n".join(f"- {d[:120]}" for d in docs)
        return f"Apaguei {len(ids)} memorias:\n{lista}"

    try:
        return _com_reconexao(op)
    except Exception as e:
        return f"ERRO ao apagar: {e}"


def listar_todas():
    """Lista todas as memórias gravadas (para auditoria)."""
    col = _conectar()
    if col is None:
        return "ERRO: memoria offline."

    def op():
        dados = _conectar().get()
        docs = dados.get("documents", [])
        if not docs:
            return "(nenhuma memoria gravada)"
        return "\n".join(f"{i+1}. {d[:150]}" for i, d in enumerate(docs))

    try:
        return _com_reconexao(op)
    except Exception as e:
        return f"ERRO: {e}"


# 💬 RESPOSTAS PRONTAS (cache de mensagem trivial) — diferente de
# salvar_memoria/buscar_memoria acima (que são lembranças de texto livre,
# buscadas por similaridade semântica pro RAG do agente), isto aqui é um
# cache CHAVE→RESPOSTA: pra "oi", "bom dia", "obrigado" etc (o conjunto
# fechado que agent.PADRAO_TRIVIAL reconhece), guarda a resposta que o
# modelo já deu uma vez e devolve ela direto da próxima vez que a MESMA
# frase (normalizada) aparecer — zero chamada ao modelo. Isso é o "Jarvis
# aprende todo dia": na primeira vez que alguém manda "boa noite" ainda
# precisa perguntar pro modelo, mas dali em diante a resposta já está
# pronta na memória, sem gastar GPU/tempo/tokens só pra cumprimento.
#
# Usa o mesmo ChromaDB (categoria "resposta_pronta"), mas por ID exato
# (col.get, não col.query) — não é busca por similaridade/embeddings
# aqui, é uma chave determinística (a frase já vem normalizada por quem
# chama, ver agent._chave_normalizada_trivial). Evita risco de "resposta
# de uma pergunta parecida mas diferente" vazar pra outra.

def buscar_resposta_pronta(chave):
    """chave: frase trivial já normalizada (ex: "oi", "bom dia").
    Devolve a resposta salva, ou None se nunca foi salva/memória offline."""
    col = _conectar()
    if col is None:
        return None

    def op():
        id_estavel = "trivial:" + hashlib.sha256(chave.encode("utf-8")).hexdigest()[:32]
        dados = _conectar().get(ids=[id_estavel], include=["metadatas"])
        metas = dados.get("metadatas") or []
        if not metas:
            return None
        return (metas[0] or {}).get("resposta") or None

    try:
        return _com_reconexao(op)
    except Exception as e:
        print(f"[MEMORIA] erro ao buscar resposta pronta: {e}")
        return None


def salvar_resposta_pronta(chave, resposta):
    """Grava/atualiza a resposta pronta pra essa chave (upsert — sobrescreve
    se já existia, pra sempre ficar com a versão mais recente que o
    modelo gerou)."""
    col = _conectar()
    if col is None:
        return False

    def op():
        c = _conectar()
        id_estavel = "trivial:" + hashlib.sha256(chave.encode("utf-8")).hexdigest()[:32]
        meta = {"categoria": "resposta_pronta", "resposta": resposta, "data": time.time()}
        try:
            c.upsert(ids=[id_estavel], documents=[chave], metadatas=[meta])
        except AttributeError:
            # chromadb antigo sem upsert() — apaga e recria manualmente
            try:
                c.delete(ids=[id_estavel])
            except Exception:
                pass
            c.add(ids=[id_estavel], documents=[chave], metadatas=[meta])
        return True

    try:
        return _com_reconexao(op)
    except Exception as e:
        print(f"[MEMORIA] erro ao salvar resposta pronta: {e}")
        return False


def status():
    if _conectar() is not None:
        return "online"
    return f"offline ({_erro})"


# 🧹 faxina: mantém a memória enxuta (roda na inicialização)
def faxina():
    col = _conectar()
    if col is None:
        return "memoria offline, faxina pulada"

    try:
        agora = time.time()

        # 1) regra de idade (desligada se FAXINA_DIAS = 0)
        velhas = []
        if FAXINA_DIAS > 0:
            limite = agora - (FAXINA_DIAS * 86400)

            dados = col.get(include=["metadatas"])
            ids = dados.get("ids", [])
            metas = dados.get("metadatas", []) or []

            # memórias antigas sem campo "data" são mantidas
            velhas = [
                i for i, m in zip(ids, metas)
                if (m or {}).get("data", agora) < limite
            ]
            if velhas:
                col.delete(ids=velhas)

        # 2) teto de quantidade: remove as mais antigas primeiro
        dados = col.get(include=["metadatas"])
        ids = dados.get("ids", [])
        metas = dados.get("metadatas", []) or []

        excesso = []
        if len(ids) > MAX_MEMORIAS:
            ordenadas = sorted(
                zip(ids, metas),
                key=lambda x: (x[1] or {}).get("data", 0)
            )
            excesso = [i for i, _ in ordenadas[: len(ids) - MAX_MEMORIAS]]
            col.delete(ids=excesso)

        total = col.count()
        return (f"{len(velhas)} antigas + {len(excesso)} excedentes removidas, "
                f"{total} memorias ativas")

    except Exception as e:
        return f"erro na faxina: {e}"
