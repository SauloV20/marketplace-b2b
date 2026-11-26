"""Compressão das imagens enviadas pelo painel (fotos de produto, banners, documentos em imagem).

As imagens ficam no banco (o disco do Render gratuito é apagado a cada deploy) e o banco gratuito tem espaço
limitado. Uma foto de celular (3 a 6 MB) vira uns 100 a 250 KB sem diferença visível na loja: o lado maior é
reduzido e a imagem é salva de novo em JPEG (ou WEBP, quando tem transparência).

Se o Pillow não estiver instalado ou a imagem não abrir, a original é usada como veio.
"""
import io

try:
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 60_000_000  # recusa "bombas" de imagem (milhões de pixels em poucos KB)
except ImportError:  # pragma: no cover
    Image = None

LADO_FOTO = 1600  # foto de produto: nítida mesmo com zoom na página
LADO_BANNER = 1920  # banner ocupa a largura da tela
QUALIDADE = 82
FORMATOS = ("JPEG", "PNG", "WEBP", "GIF")  # os mesmos de banners.tipo_da_imagem


def disponivel():
    return Image is not None


def comprimir(dados, tipo, lado_maximo=LADO_FOTO):
    """Devolve (bytes, tipo) menores. GIF fica como está (pode ser animado); se não ficar menor, mantém o original."""
    if Image is None or tipo == "image/gif" or not dados:
        return dados, tipo
    try:
        # só os formatos aceitos no upload: a Pillow nunca chama o Ghostscript (EPS/PS) nem outro leitor externo
        with Image.open(io.BytesIO(dados), formats=FORMATOS) as img:
            img = ImageOps.exif_transpose(img)  # foto de celular "deitada" fica de pé
            if max(img.size) > lado_maximo:
                img.thumbnail((lado_maximo, lado_maximo), Image.LANCZOS)
            saida = io.BytesIO()
            transparente = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            if transparente:
                img.convert("RGBA").save(saida, "WEBP", quality=QUALIDADE, method=6)
                novo_tipo = "image/webp"
            else:
                img.convert("RGB").save(saida, "JPEG", quality=QUALIDADE, optimize=True, progressive=True)
                novo_tipo = "image/jpeg"
    except Exception:  # imagem estranha ou grande demais: guarda como veio (já foi validada pelo tipo)
        return dados, tipo
    novo = saida.getvalue()
    return (novo, novo_tipo) if len(novo) < len(dados) else (dados, tipo)
