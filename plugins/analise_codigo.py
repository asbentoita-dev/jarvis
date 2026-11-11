# 🔌 Plugin: análise de código + identificação real de arquivos
# Portado do Jarvis v1 (pasta v1_legado): PythonAnalyzer e FileIdentifier.
# Melhorias na portagem: qualquer cérebro analisa (não só llama3),
# código grande é cortado para caber no contexto, e a identificação
# funciona mesmo sem a biblioteca libmagic (assinaturas de bytes).

import os


# ── identificação por assinatura de bytes (fallback sem libmagic) ──
ASSINATURAS = [
    (b"%PDF", "Documento PDF"),
    (b"PK\x03\x04", "Pacote ZIP (pode ser docx/xlsx/pptx/zip)"),
    (b"\x89PNG", "Imagem PNG"),
    (b"\xff\xd8\xff", "Imagem JPEG"),
    (b"GIF8", "Imagem GIF"),
    (b"RIFF", "Mídia RIFF (wav/avi/webp)"),
    (b"ID3", "Áudio MP3"),
    (b"OggS", "Áudio Ogg"),
    (b"MZ", "Executável do Windows (exe/dll)"),
    (b"\x1f\x8b", "Compactado gzip"),
    (b"GGUF", "Modelo de IA (GGUF)"),
    (b"SQLite format 3", "Banco de dados SQLite"),
]


def identificar_arquivo(caminho):
    """Identifica o tipo REAL de um arquivo (pelo conteúdo, não só extensão)."""
    import tools

    real = tools._resolver_caminho(caminho)
    if real is None:
        return ("ERRO: fora das pastas permitidas. "
                "Se o usuario pediu, chame liberar_pasta primeiro.")
    if not os.path.exists(real):
        return f"ERRO: arquivo nao existe: {caminho}"

    ext = os.path.splitext(real)[1].lower() or "(sem extensao)"
    kb = os.path.getsize(real) // 1024

    tipo = None

    # 1) libmagic, se instalada (pip install python-magic-bin)
    try:
        import magic
        tipo = magic.Magic(mime=True).from_file(real)
    except Exception:
        pass

    # 2) fallback: assinatura dos primeiros bytes
    if not tipo:
        try:
            with open(real, "rb") as f:
                inicio = f.read(16)
            tipo = next((nome for ass, nome in ASSINATURAS
                         if inicio.startswith(ass)), None)
            if tipo is None:
                # heurística: é texto?
                tipo = ("Texto" if all(b in range(9, 127) or b >= 128
                                       for b in inicio[:12]) else "Desconhecido")
        except Exception as e:
            return f"ERRO ao identificar: {e}"

    aviso = ""
    if tipo and "ZIP" in str(tipo) and ext in (".docx", ".xlsx", ".pptx"):
        aviso = " (normal: documentos Office sao pacotes ZIP)"
    elif ext != "(sem extensao)" and tipo and ext[1:] not in str(tipo).lower() \
            and "Texto" not in str(tipo):
        aviso = " ⚠ ATENCAO: a extensao pode nao bater com o conteudo real"

    return (f"{os.path.basename(real)} | extensao: {ext} | "
            f"tipo real: {tipo}{aviso} | {kb} KB")


# ── análise de código com roteiro (ideia do PythonAnalyzer do v1) ──

def analisar_codigo(caminho):
    """Lê um arquivo de código e prepara a análise técnica com roteiro."""
    import tools

    conteudo = tools.ler_arquivo(caminho)
    if str(conteudo).startswith("ERRO"):
        return conteudo

    codigo = str(conteudo)[:6000]
    linhas = codigo.count("\n") + 1

    return f"""[CODIGO DE: {caminho} | ~{linhas} linhas na amostra]

{codigo}

[INSTRUCAO PARA VOCE, JARVIS — responda agora ao usuario:]
1. O que esse código faz?
2. Existe algum problema ou bug?
3. Existe risco de segurança?
4. Melhorias recomendadas (as 3 mais importantes)?
5. Qualidade geral: nota de 0 a 10, com justificativa curta.
Seja direto e técnico. Baseie-se APENAS no código acima."""


TOOLS = [
    {"type": "function", "function": {
        "name": "identificar_arquivo",
        "description": "Identifica o tipo REAL de um arquivo pelo conteudo (nao so pela extensao). Detecta extensoes mentirosas. Use quando o tipo de um arquivo for duvidoso.",
        "parameters": {"type": "object", "properties": {
            "caminho": {"type": "string", "description": "Caminho do arquivo"}},
            "required": ["caminho"]}}},
    {"type": "function", "function": {
        "name": "analisar_codigo",
        "description": "Analise tecnica de um arquivo de codigo (py, js, etc): o que faz, bugs, riscos de seguranca, melhorias e nota 0-10. Use quando pedirem para revisar/avaliar/melhorar codigo.",
        "parameters": {"type": "object", "properties": {
            "caminho": {"type": "string", "description": "Caminho do arquivo de codigo"}},
            "required": ["caminho"]}}},
]

FERRAMENTAS = {
    "identificar_arquivo": identificar_arquivo,
    "analisar_codigo": analisar_codigo,
}
