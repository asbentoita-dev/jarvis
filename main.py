from agent import processar_mensagem
from llm import verificar_ollama
import memory
import memory_backup

# 🧠 modo terminal do Jarvis (a janela usa app.py / Jarvis_bandeja.bat)

print("🧠 Jarvis iniciado (modo terminal).")

aviso = verificar_ollama()
if aviso:
    print(aviso)

print(f"Memória (ChromaDB): {memory.status()}")
print(f"Faxina da memória: {memory.faxina()}")

# 💾 Backup automático e periódico da memória (ChromaDB). Ver memory_backup.py.
memory_backup.iniciar_agendador()
print('Digite "sair" para encerrar.\n')

historico = []

while True:
    msg = input("Você: ")

    if msg.lower() in ["sair", "exit"]:
        break

    try:
        resposta, metricas = processar_mensagem(msg, historico)
    except Exception as e:
        resposta, metricas = f"Erro: {e}", {}
    else:
        # 🩹 CORREÇÃO 25/08/2026: processar_mensagem não grava mais sozinho
        # no histórico do chamador (ver agent.py) — cada chamador agora
        # cuida do próprio histórico, exatamente 1 vez por turno.
        historico.append({"role": "user", "content": msg})
        historico.append({"role": "assistant", "content": resposta})

    print("\nJarvis:", resposta)
    if metricas:
        print(f"  ({metricas.get('tempo_total_s', 0)}s total · "
              f"{metricas.get('tokens_entrada', 0)} in / "
              f"{metricas.get('tokens_saida', 0)} out · "
              f"{metricas.get('tokens_por_segundo', 0)} tok/s)")
    print()
