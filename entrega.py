"""Pós-venda: envio com rastreio e documentos do pedido (nota fiscal, XML, boleto).

A entrega é separada do pagamento: um pedido no boleto faturado é enviado antes de ser pago.
Os documentos ficam no banco (o disco do Render gratuito é apagado a cada deploy) e são baixados sempre como
anexo, nunca abertos no navegador como página.
"""
import re
import unicodedata

from flask import Response

from banners import tipo_da_imagem

TIPOS = {"nota": "Nota fiscal (DANFE)", "xml": "XML da nota fiscal", "boleto": "Boleto", "outro": "Outro documento"}
ENVIO = {"": "Aguardando envio", "enviado": "Enviado", "entregue": "Entregue"}
MAX_ARQUIVO = 4_500_000  # o envio inteiro (formulário + arquivo) tem limite de 5 MB
MAX_POR_PEDIDO = 20


def _mime(dados):
    if dados.startswith(b"%PDF-"):
        return "application/pdf", "pdf"
    inicio = dados[:200].lstrip(b"\xef\xbb\xbf \t\r\n")
    if inicio.startswith(b"<?xml") or inicio.startswith(b"<nfeProc") or inicio.startswith(b"<NFe"):
        return "application/xml", "xml"
    imagem = tipo_da_imagem(dados)
    if imagem:
        return imagem, imagem.split("/")[1].replace("jpeg", "jpg")
    return None, None


def nome_seguro(nome, extensao):
    """Nome do arquivo sem acentos, espaços estranhos ou caminhos, com a extensão do tipo real."""
    base = unicodedata.normalize("NFKD", (nome or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
    base = base.encode("ascii", "ignore").decode()
    base = base.rsplit(".", 1)[0] if "." in base else base
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-.")[:100] or "documento"
    return f"{base}.{extensao}"


def ler_arquivo(arquivo, tipo, ja_tem):
    """Valida o documento enviado no painel. Devolve (dict com os campos, erro)."""
    if tipo not in TIPOS:
        return None, "Escolha o tipo do documento."
    if not arquivo or not arquivo.filename:
        return None, "Escolha o arquivo."
    if ja_tem >= MAX_POR_PEDIDO:
        return None, f"Cada pedido aceita até {MAX_POR_PEDIDO} documentos. Remova algum antes."
    dados = arquivo.read(MAX_ARQUIVO + 1)
    if len(dados) > MAX_ARQUIVO:
        return None, "O arquivo passa de 4,5 MB. Comprima o PDF e envie de novo."
    mime, ext = _mime(dados)
    if not mime:
        return None, "Envie um PDF, o XML da nota ou uma imagem (PNG, JPG)."
    if tipo == "xml" and ext != "xml":
        return None, "Para “XML da nota fiscal”, envie o arquivo .xml que o emissor da nota gera."
    if tipo != "xml" and ext == "xml":
        tipo = "xml"
    if mime.startswith("image/"):  # foto de nota/boleto: menor, mas ainda legível
        import imagens
        dados, mime = imagens.comprimir(dados, mime, 2400)
        ext = {"image/jpeg": "jpg", "image/webp": "webp"}.get(mime, ext)
    return dict(tipo=tipo, nome=nome_seguro(arquivo.filename, ext), mime=mime, tamanho=len(dados), dados=dados), None


def baixar(arquivo):
    return Response(arquivo.dados, mimetype=arquivo.mime, headers={
        "Content-Disposition": f'attachment; filename="{arquivo.nome}"',
        "Cache-Control": "private, no-store",
    })


def ler_envio(form):
    """Transportadora, código e link de rastreio. Devolve (dados, erro)."""
    transportadora = " ".join(form.get("transportadora", "").split())[:80]
    rastreio = " ".join(form.get("rastreio", "").split())[:80]
    link = form.get("rastreio_link", "").strip()
    if not transportadora:
        return None, "Informe a transportadora (ou “Entrega própria”, “Retirada no balcão”)."
    if link and (not link.startswith(("https://", "http://")) or len(link) > 300 or " " in link):
        return None, "O link de rastreio precisa começar com https:// (ou deixe vazio)."
    return dict(transportadora=transportadora, rastreio=rastreio, rastreio_link=link), None
