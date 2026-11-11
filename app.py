import os
import subprocess
import threading
import time
import webbrowser

# 🧠 Jarvis como app de desktop:
# roda o Flask e abre uma janela de aplicativo (Edge/Chrome --app):
# sem abas nem barra de endereço, parece um programa nativo.

from web import app
import memory_backup

URL = "http://127.0.0.1:5000"

NAVEGADORES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def abrir_janela():
    time.sleep(1.5)  # espera o servidor subir

    for exe in NAVEGADORES:
        if os.path.exists(exe):
            subprocess.Popen([exe, f"--app={URL}", "--window-size=1000,740"])
            return

    # plano C: navegador comum
    webbrowser.open(URL)


if __name__ == "__main__":
    threading.Thread(target=abrir_janela, daemon=True).start()

    # 💾 Backup automático e periódico da memória (ChromaDB). Ver memory_backup.py.
    memory_backup.iniciar_agendador()

    # servidor em primeiro plano (feche esta janela preta para encerrar)
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
