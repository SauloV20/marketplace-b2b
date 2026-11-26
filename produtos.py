"""Página do produto: link amigável, fotos (guardadas no banco) e produtos relacionados."""
import re
import unicodedata

from flask import url_for

import imagens
from banners import MAX_IMAGEM, tipo_da_imagem
from models import Product, ProdutoFoto

MAX_FOTOS = 8


def slug(texto):
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:80] or "produto"


def sem_acento(texto):
    """'Máscara de Solda' -> 'mascara de solda' (para a busca achar com ou sem acento)."""
    return unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()


def link(p, externo=False):
    caminho = url_for("shop.produto", pid=p.id, nome=slug(p.name))
    if externo:  # endereço oficial do site (SITE_URL), não o que o visitante usou para chegar
        from seo import base
        return base() + caminho
    return caminho


def fotos(p):
    """URLs das fotos: as enviadas pelo painel (na ordem) e, por último, o link de imagem antigo."""
    lista = [url_for("shop.produto_foto", fid=f.id, v=f.versao) for f in p.fotos]
    if p.image_url:
        lista.append(p.image_url)
    return lista


def foto_principal(p):
    lista = fotos(p)
    return lista[0] if lista else None


def relacionados(p, limite=6):
    return (Product.query.filter(Product.category_id == p.category_id, Product.id != p.id, Product.active.is_(True))
            .order_by(Product.created_at.desc()).limit(limite).all())


def ler_fotos(arquivos, ja_tem, obrigatorio=True):
    """Valida as fotos enviadas. Devolve ([(bytes, tipo)], erro)."""
    novas = []
    for a in arquivos:
        if not a or not a.filename:
            continue
        dados = a.read(MAX_IMAGEM + 1)
        if len(dados) > MAX_IMAGEM:
            return None, f"A foto {a.filename} passa de 10 MB. Diminua e envie de novo."
        tipo = tipo_da_imagem(dados)
        if not tipo:
            return None, f"{a.filename} não é uma imagem PNG, JPG, WEBP ou GIF."
        novas.append(imagens.comprimir(dados, tipo, imagens.LADO_FOTO))  # 3 MB de celular viram ~200 KB
    if not novas:
        return ([], None) if not obrigatorio else (None, "Escolha pelo menos uma foto.")
    if ja_tem + len(novas) > MAX_FOTOS:
        return None, f"Cada produto aceita até {MAX_FOTOS} fotos (já tem {ja_tem})."
    return novas, None


def nova_foto(p, dados, tipo, ordem):
    return ProdutoFoto(product_id=p.id, dados=dados, tipo=tipo, ordem=ordem)
