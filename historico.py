import json
import os
import hashlib
from datetime import datetime
from config import WORKSPACE

# 📚 Persistência de histórico de conversas
# Salva cada sessão de conversa em arquivo JSON no workspace

HISTORICO_DIR = os.path.join(WORKSPACE, "historico")
os.makedirs(HISTORICO_DIR, exist_ok=True)


def _gerar_id_sessao():
    """Gera um ID único para a sessão baseado no timestamp."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _arquivo_sessao(id_sessao):
    """Retorna o caminho do arquivo de uma sessão."""
    return os.path.join(HISTORICO_DIR, f"sessao_{id_sessao}.json")


def criar_sessao(titulo=None):
    """Cria uma nova sessão de conversa.

    Titulo fica vazio de propósito quando não informado: assim
    salvar_sessao() gera o título de verdade a partir da primeira
    mensagem (tipo Claude/ChatGPT). Antes tinha um "Conversa {id}" aqui
    que nunca deixava esse título automático acontecer — a barra lateral
    ficava presa em "Conversa 20260814_195000" pra sempre."""
    id_sessao = _gerar_id_sessao()
    sessao = {
        "id": id_sessao,
        "titulo": titulo or "",
        "criada_em": datetime.now().isoformat(),
        "atualizada_em": datetime.now().isoformat(),
        "mensagens": []
    }
    return sessao


def salvar_sessao(sessao):
    """Salva uma sessão no disco."""
    if not sessao.get("id"):
        sessao["id"] = _gerar_id_sessao()
    
    sessao["atualizada_em"] = datetime.now().isoformat()
    
    # Gera título automático a partir da 1ª mensagem do usuário (igual
    # Claude/ChatGPT), se ainda não tiver um título de verdade
    if not sessao.get("titulo") and sessao.get("mensagens"):
        primeira = next(
            (m for m in sessao["mensagens"] if m.get("role") == "user"),
            sessao["mensagens"][0])
        bruta = (primeira.get("content") or "").strip().replace("\n", " ")
        if bruta:
            cortada = bruta[:50]
            sessao["titulo"] = cortada + ("..." if len(bruta) > 50 else "")
        else:
            sessao["titulo"] = "Conversa"
    
    arquivo = _arquivo_sessao(sessao["id"])
    with open(arquivo, "w", encoding="utf-8") as f:
        json.dump(sessao, f, ensure_ascii=False, indent=2)

    # poda automática: nunca deixa acumular mais que MAX_SESSOES_PADRAO
    # sessões salvas (evita crescer sem necessidade, tipo as 58 de antes)
    limitar_sessoes()

    return arquivo


def carregar_sessao(id_sessao):
    """Carrega uma sessão do disco."""
    arquivo = _arquivo_sessao(id_sessao)
    if not os.path.exists(arquivo):
        return None
    
    with open(arquivo, "r", encoding="utf-8") as f:
        return json.load(f)


def listar_sessoes(limite=50):
    """Lista todas as sessões salvas, ordenadas por data (mais recentes primeiro)."""
    if not os.path.exists(HISTORICO_DIR):
        return []
    
    sessoes = []
    for arquivo in sorted(os.listdir(HISTORICO_DIR), reverse=True):
        if not arquivo.startswith("sessao_") or not arquivo.endswith(".json"):
            continue
        
        caminho = os.path.join(HISTORICO_DIR, arquivo)
        try:
            with open(caminho, "r", encoding="utf-8") as f:
                sessao = json.load(f)
                sessoes.append({
                    "id": sessao.get("id", arquivo.replace("sessao_", "").replace(".json", "")),
                    "titulo": sessao.get("titulo", "Sem título"),
                    "criada_em": sessao.get("criada_em", ""),
                    "atualizada_em": sessao.get("atualizada_em", ""),
                    "mensagens_count": len(sessao.get("mensagens", []))
                })
        except Exception:
            continue
        
        if len(sessoes) >= limite:
            break
    
    return sessoes


def adicionar_mensagem(sessao, role, content, tool_calls=None):
    """Adiciona uma mensagem à sessão. O "id" é a posição de inserção no
    momento da criação (não a posição atual na lista) — fica estável pra
    sempre mesmo que outras mensagens sejam apagadas depois, o que é
    justamente o que excluir_mensagem() precisa pra apagar a certa."""
    if "mensagens" not in sessao:
        sessao["mensagens"] = []

    msg = {
        "id": len(sessao["mensagens"]),
        "role": role,
        "content": content,
        "timestamp": datetime.now().isoformat()
    }

    if tool_calls:
        msg["tool_calls"] = tool_calls

    sessao["mensagens"].append(msg)
    sessao["atualizada_em"] = datetime.now().isoformat()

    return msg


def excluir_mensagem(id_sessao, msg_id):
    """Apaga uma única mensagem (um balão) de uma sessão salva, pelo "id"
    atribuído na criação — não pela posição atual, que muda conforme
    outras mensagens vão sendo removidas. Sessões salvas antes desse campo
    existir (sem "id" em nenhuma mensagem) caem num fallback por índice
    posicional. Devolve True se removeu alguma coisa, False se não achou."""
    sessao = carregar_sessao(id_sessao)
    if not sessao:
        return False

    mensagens = sessao.get("mensagens", [])
    try:
        msg_id_int = int(msg_id)
    except (TypeError, ValueError):
        return False

    tem_ids = any("id" in m for m in mensagens)
    if tem_ids:
        novas = [m for m in mensagens if m.get("id") != msg_id_int]
    else:
        if not (0 <= msg_id_int < len(mensagens)):
            return False
        novas = mensagens[:msg_id_int] + mensagens[msg_id_int + 1:]

    if len(novas) == len(mensagens):
        return False  # id não encontrado, nada foi removido

    sessao["mensagens"] = novas
    salvar_sessao(sessao)
    return True


def excluir_sessao(id_sessao):
    """Exclui uma sessão do disco."""
    arquivo = _arquivo_sessao(id_sessao)
    if os.path.exists(arquivo):
        os.remove(arquivo)
        return True
    return False


MAX_SESSOES_PADRAO = 30


def limitar_sessoes(max_sessoes=MAX_SESSOES_PADRAO):
    """Poda o histórico automaticamente, mantendo só as `max_sessoes` mais
    recentes (por atualizada_em, igual listar_sessoes ordena). Chamada a
    cada salvar_sessao() pra evitar que o histórico cresça sem parar (58
    sessões acumuladas sem necessidade nenhuma, a maioria só ruído de
    teste). Devolve quantas sessões foram removidas."""
    todas = listar_sessoes(limite=999999)
    excedentes = todas[max_sessoes:]
    removidas = 0
    for s in excedentes:
        if excluir_sessao(s["id"]):
            removidas += 1
    return removidas


def apagar_todas_sessoes(manter_id=None):
    """Apaga TODO o histórico salvo em disco (botão 🗑️ Apagar histórico da
    aba Sistema). `manter_id`, se informado, preserva essa sessão (ex: a
    que está aberta na hora) em vez de zerar tudo. Devolve quantas
    sessões foram removidas."""
    if not os.path.exists(HISTORICO_DIR):
        return 0
    removidas = 0
    for arquivo in os.listdir(HISTORICO_DIR):
        if not arquivo.startswith("sessao_") or not arquivo.endswith(".json"):
            continue
        id_sessao = arquivo.replace("sessao_", "").replace(".json", "")
        if manter_id and id_sessao == manter_id:
            continue
        if excluir_sessao(id_sessao):
            removidas += 1
    return removidas


def exportar_sessao(id_sessao, formato="json"):
    """Exporta uma sessão em diferentes formatos."""
    sessao = carregar_sessao(id_sessao)
    if not sessao:
        return None
    
    if formato == "json":
        return json.dumps(sessao, ensure_ascii=False, indent=2)
    
    elif formato == "txt":
        linhas = [f"=== {sessao.get('titulo', 'Conversa')} ===\n"]
        linhas.append(f"Criada em: {sessao.get('criada_em', 'N/A')}\n\n")
        
        for msg in sessao.get("mensagens", []):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            timestamp = msg.get("timestamp", "")
            
            if role == "user":
                linhas.append(f"[Você] ({timestamp}): {content}\n")
            elif role == "assistant":
                linhas.append(f"[Jarvis] ({timestamp}): {content}\n")
            elif role == "tool":
                linhas.append(f"[Ferramenta] ({timestamp}): {content}\n")
        
        return "\n".join(linhas)
    
    elif formato == "md":
        linhas = [f"# {sessao.get('titulo', 'Conversa')}\n\n"]
        linhas.append(f"**Criada em:** {sessao.get('criada_em', 'N/A')}\n\n")
        linhas.append("---\n\n")
        
        for msg in sessao.get("mensagens", []):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            timestamp = msg.get("timestamp", "")
            
            if role == "user":
                linhas.append(f"### 🧑 Você\n\n{content}\n\n")
            elif role == "assistant":
                linhas.append(f"### 🤖 Jarvis\n\n{content}\n\n")
            elif role == "tool":
                linhas.append(f"### 🔧 Ferramenta\n\n```\n{content}\n```\n\n")
        
        return "\n".join(linhas)
    
    return None


def buscar_sessoes(query, limite=10):
    """Busca sessões por conteúdo das mensagens."""
    sessoes = []
    query_lower = query.lower()
    
    for arquivo in os.listdir(HISTORICO_DIR):
        if not arquivo.endswith(".json"):
            continue
        
        caminho = os.path.join(HISTORICO_DIR, arquivo)
        try:
            with open(caminho, "r", encoding="utf-8") as f:
                sessao = json.load(f)
                
                # Busca no título
                if query_lower in sessao.get("titulo", "").lower():
                    sessoes.append({
                        "id": sessao.get("id"),
                        "titulo": sessao.get("titulo"),
                        "criada_em": sessao.get("criada_em"),
                        "match": "título"
                    })
                    continue
                
                # Busca nas mensagens
                for msg in sessao.get("mensagens", []):
                    if query_lower in msg.get("content", "").lower():
                        sessoes.append({
                            "id": sessao.get("id"),
                            "titulo": sessao.get("titulo"),
                            "criada_em": sessao.get("criada_em"),
                            "match": "mensagem"
                        })
                        break
        except Exception:
            continue
        
        if len(sessoes) >= limite:
            break
    
    return sessoes
