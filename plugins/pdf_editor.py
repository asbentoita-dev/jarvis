# 🔌 Plugin: pdf_editor — ferramentas reais de edição de PDF (não só
# leitura). Junta, divide, extrai páginas, gira e coloca marca d'água.
# Usa pypdf (já é dependência pra leitura de PDF) + reportlab (só pra
# gerar a camada de texto da marca d'água).
#
# Requisitos: pip install pypdf reportlab

import io
import os
import re

from config import WORKSPACE
import pastas_config

PASTA_SAIDA = os.path.join(WORKSPACE, "pdfs")


def _bases_permitidas():
    return [WORKSPACE] + pastas_config.listar()


def _resolver(caminho):
    """Mesma regra do resto do Jarvis: nome simples cai no workspace;
    caminho completo só é aceito se estiver numa pasta liberada."""
    caminho = str(caminho).strip().strip('"')

    if os.path.isabs(caminho):
        real = os.path.abspath(caminho)
        for base in _bases_permitidas():
            if real.lower().startswith(os.path.abspath(base).lower()):
                return real
        return None

    for base in _bases_permitidas():
        candidato = os.path.join(base, caminho)
        if os.path.exists(candidato):
            return candidato

    return os.path.join(WORKSPACE, caminho)


def _saida(nome):
    os.makedirs(PASTA_SAIDA, exist_ok=True)
    return os.path.join(PASTA_SAIDA, nome)


def _parsear_paginas(spec, total):
    """'1,3,5-7' -> [0,2,4,5,6] (0-indexado). '' ou 'todas' = tudo."""
    spec = (spec or "").strip().lower()
    if not spec or spec == "todas":
        return list(range(total))

    indices = []
    for parte in spec.split(","):
        parte = parte.strip()
        if not parte:
            continue
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", parte)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            indices.extend(range(a - 1, b))
        elif parte.isdigit():
            indices.append(int(parte) - 1)
    return [i for i in indices if 0 <= i < total]


def _importar_pypdf():
    try:
        from pypdf import PdfReader, PdfWriter
        return PdfReader, PdfWriter
    except ImportError:
        return None, None


def juntar_pdfs(caminhos, nome_saida="juntado.pdf"):
    """Junta vários PDFs (nessa ordem) em um único arquivo."""
    PdfReader, PdfWriter = _importar_pypdf()
    if not PdfReader:
        return "ERRO: instale pypdf (pip install pypdf)"

    if isinstance(caminhos, str):
        lista = [c.strip() for c in caminhos.split(",") if c.strip()]
    else:
        lista = list(caminhos)

    if len(lista) < 2:
        return "ERRO: informe pelo menos 2 arquivos (separados por vírgula)."

    escritor = PdfWriter()
    for nome in lista:
        caminho = _resolver(nome)
        if not caminho or not os.path.exists(caminho):
            return f"ERRO: arquivo nao encontrado ou fora das pastas permitidas: {nome}"
        try:
            leitor = PdfReader(caminho)
        except Exception as e:
            return f"ERRO ao abrir {nome}: {e}"
        for pagina in leitor.pages:
            escritor.add_page(pagina)

    destino = _saida(nome_saida if nome_saida.endswith(".pdf") else nome_saida + ".pdf")
    with open(destino, "wb") as f:
        escritor.write(f)

    return f"PDFs juntados ({len(lista)} arquivos) em: {destino}"


def dividir_pdf(caminho, paginas_por_arquivo=1):
    """Divide um PDF em vários arquivos menores, N páginas cada."""
    PdfReader, PdfWriter = _importar_pypdf()
    if not PdfReader:
        return "ERRO: instale pypdf (pip install pypdf)"

    origem = _resolver(caminho)
    if not origem or not os.path.exists(origem):
        return f"ERRO: arquivo nao encontrado: {caminho}"

    try:
        n = max(1, int(paginas_por_arquivo))
    except (TypeError, ValueError):
        n = 1

    leitor = PdfReader(origem)
    total = len(leitor.pages)
    base_nome = os.path.splitext(os.path.basename(origem))[0]
    pasta = _saida(f"{base_nome}_partes")
    os.makedirs(pasta, exist_ok=True)

    gerados = []
    for inicio in range(0, total, n):
        escritor = PdfWriter()
        for i in range(inicio, min(inicio + n, total)):
            escritor.add_page(leitor.pages[i])
        nome_parte = os.path.join(pasta, f"{base_nome}_p{inicio + 1}-{min(inicio + n, total)}.pdf")
        with open(nome_parte, "wb") as f:
            escritor.write(f)
        gerados.append(nome_parte)

    return f"PDF dividido em {len(gerados)} arquivo(s), pasta: {pasta}\n" + "\n".join(gerados)


def extrair_paginas_pdf(caminho, paginas, nome_saida=None):
    """Extrai páginas específicas (ex: '1,3,5-7') para um novo PDF."""
    PdfReader, PdfWriter = _importar_pypdf()
    if not PdfReader:
        return "ERRO: instale pypdf (pip install pypdf)"

    origem = _resolver(caminho)
    if not origem or not os.path.exists(origem):
        return f"ERRO: arquivo nao encontrado: {caminho}"

    leitor = PdfReader(origem)
    total = len(leitor.pages)
    indices = _parsear_paginas(paginas, total)
    if not indices:
        return f"ERRO: nenhuma pagina valida em '{paginas}' (o PDF tem {total} paginas)."

    escritor = PdfWriter()
    for i in indices:
        escritor.add_page(leitor.pages[i])

    base_nome = os.path.splitext(os.path.basename(origem))[0]
    nome_saida = nome_saida or f"{base_nome}_extraido.pdf"
    if not nome_saida.endswith(".pdf"):
        nome_saida += ".pdf"
    destino = _saida(nome_saida)
    with open(destino, "wb") as f:
        escritor.write(f)

    return f"Extraidas {len(indices)} pagina(s) para: {destino}"


def girar_pdf(caminho, graus=90, paginas="todas", nome_saida=None):
    """Gira páginas do PDF (90/180/270 graus, sentido horário)."""
    PdfReader, PdfWriter = _importar_pypdf()
    if not PdfReader:
        return "ERRO: instale pypdf (pip install pypdf)"

    origem = _resolver(caminho)
    if not origem or not os.path.exists(origem):
        return f"ERRO: arquivo nao encontrado: {caminho}"

    try:
        graus = int(graus)
    except (TypeError, ValueError):
        return "ERRO: graus deve ser um numero (90, 180 ou 270)."
    if graus % 90 != 0:
        return "ERRO: graus deve ser multiplo de 90."

    leitor = PdfReader(origem)
    total = len(leitor.pages)
    alvo = set(_parsear_paginas(paginas, total))

    escritor = PdfWriter()
    for i, pagina in enumerate(leitor.pages):
        if i in alvo:
            pagina.rotate(graus)
        escritor.add_page(pagina)

    base_nome = os.path.splitext(os.path.basename(origem))[0]
    nome_saida = nome_saida or f"{base_nome}_girado.pdf"
    if not nome_saida.endswith(".pdf"):
        nome_saida += ".pdf"
    destino = _saida(nome_saida)
    with open(destino, "wb") as f:
        escritor.write(f)

    return f"PDF girado ({graus} graus, paginas: {paginas}) salvo em: {destino}"


def adicionar_marca_dagua_pdf(caminho, texto, nome_saida=None):
    """Aplica uma marca d'água diagonal de texto em todas as páginas."""
    PdfReader, PdfWriter = _importar_pypdf()
    if not PdfReader:
        return "ERRO: instale pypdf (pip install pypdf)"
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.colors import Color
    except ImportError:
        return "ERRO: instale reportlab (pip install reportlab)"

    origem = _resolver(caminho)
    if not origem or not os.path.exists(origem):
        return f"ERRO: arquivo nao encontrado: {caminho}"

    leitor = PdfReader(origem)
    primeira = leitor.pages[0]
    largura = float(primeira.mediabox.width)
    altura = float(primeira.mediabox.height)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(largura, altura))
    c.saveState()
    c.setFillColor(Color(0.5, 0.5, 0.5, alpha=0.35))
    c.setFont("Helvetica-Bold", 40)
    c.translate(largura / 2, altura / 2)
    c.rotate(45)
    c.drawCentredString(0, 0, str(texto)[:60])
    c.restoreState()
    c.save()
    buf.seek(0)

    marca = PdfReader(buf).pages[0]
    escritor = PdfWriter()
    for pagina in leitor.pages:
        pagina.merge_page(marca)
        escritor.add_page(pagina)

    base_nome = os.path.splitext(os.path.basename(origem))[0]
    nome_saida = nome_saida or f"{base_nome}_marcado.pdf"
    if not nome_saida.endswith(".pdf"):
        nome_saida += ".pdf"
    destino = _saida(nome_saida)
    with open(destino, "wb") as f:
        escritor.write(f)

    return f"Marca d'agua aplicada, salvo em: {destino}"


def extrair_texto_pdf(caminho):
    """Extrai o texto de TODAS as páginas (sem o limite de leitura rápida
    de ler_arquivo, que só olha as 20 primeiras páginas)."""
    PdfReader, _ = _importar_pypdf()
    if not PdfReader:
        return "ERRO: instale pypdf (pip install pypdf)"

    origem = _resolver(caminho)
    if not origem or not os.path.exists(origem):
        return f"ERRO: arquivo nao encontrado: {caminho}"

    try:
        leitor = PdfReader(origem)
        partes = []
        for i, pagina in enumerate(leitor.pages):
            texto = pagina.extract_text() or ""
            partes.append(f"--- pagina {i + 1} ---\n{texto}")
        completo = "\n".join(partes)
    except Exception as e:
        return f"ERRO ao extrair texto: {e}"

    if len(completo) > 12000:
        return completo[:12000] + f"\n...(cortado, {len(completo)} caracteres no total — peça uma faixa de paginas especifica se precisar do resto)"
    return completo or "(PDF sem texto extraivel — pode ser so imagem escaneada, sem OCR)"


TOOLS = [
    {"type": "function", "function": {
        "name": "juntar_pdfs",
        "description": "Junta varios arquivos PDF (na ordem dada) em um unico PDF.",
        "parameters": {"type": "object", "properties": {
            "caminhos": {"type": "string", "description": "Nomes/caminhos dos PDFs separados por virgula, na ordem desejada"},
            "nome_saida": {"type": "string", "description": "Nome do arquivo final (opcional, padrao 'juntado.pdf')"}},
            "required": ["caminhos"]}}},
    {"type": "function", "function": {
        "name": "dividir_pdf",
        "description": "Divide um PDF em varios arquivos menores, N paginas cada.",
        "parameters": {"type": "object", "properties": {
            "caminho": {"type": "string", "description": "Nome/caminho do PDF"},
            "paginas_por_arquivo": {"type": "integer", "description": "Quantas paginas por arquivo gerado (padrao 1)"}},
            "required": ["caminho"]}}},
    {"type": "function", "function": {
        "name": "extrair_paginas_pdf",
        "description": "Extrai paginas especificas de um PDF para um novo arquivo (ex: '1,3,5-7').",
        "parameters": {"type": "object", "properties": {
            "caminho": {"type": "string", "description": "Nome/caminho do PDF"},
            "paginas": {"type": "string", "description": "Paginas a extrair, ex: '1,3,5-7'"},
            "nome_saida": {"type": "string", "description": "Nome do arquivo final (opcional)"}},
            "required": ["caminho", "paginas"]}}},
    {"type": "function", "function": {
        "name": "girar_pdf",
        "description": "Gira paginas de um PDF em 90/180/270 graus.",
        "parameters": {"type": "object", "properties": {
            "caminho": {"type": "string", "description": "Nome/caminho do PDF"},
            "graus": {"type": "integer", "description": "90, 180 ou 270"},
            "paginas": {"type": "string", "description": "Quais paginas girar, ex: 'todas' ou '1,3,5-7' (padrao todas)"},
            "nome_saida": {"type": "string", "description": "Nome do arquivo final (opcional)"}},
            "required": ["caminho", "graus"]}}},
    {"type": "function", "function": {
        "name": "adicionar_marca_dagua_pdf",
        "description": "Aplica uma marca d'agua de texto (diagonal, translucida) em todas as paginas de um PDF.",
        "parameters": {"type": "object", "properties": {
            "caminho": {"type": "string", "description": "Nome/caminho do PDF"},
            "texto": {"type": "string", "description": "Texto da marca d'agua, ex: 'CONFIDENCIAL'"},
            "nome_saida": {"type": "string", "description": "Nome do arquivo final (opcional)"}},
            "required": ["caminho", "texto"]}}},
    {"type": "function", "function": {
        "name": "extrair_texto_pdf",
        "description": "Extrai o texto de TODAS as paginas de um PDF (ler_arquivo so pega as 20 primeiras) — use para PDFs grandes ou quando precisar do documento completo.",
        "parameters": {"type": "object", "properties": {
            "caminho": {"type": "string", "description": "Nome/caminho do PDF"}},
            "required": ["caminho"]}}},
]

FERRAMENTAS = {
    "juntar_pdfs": juntar_pdfs,
    "dividir_pdf": dividir_pdf,
    "extrair_paginas_pdf": extrair_paginas_pdf,
    "girar_pdf": girar_pdf,
    "adicionar_marca_dagua_pdf": adicionar_marca_dagua_pdf,
    "extrair_texto_pdf": extrair_texto_pdf,
}
