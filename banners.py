"""Banners da página inicial: produto em destaque ou imagem pronta, com a imagem guardada no banco.

No Render gratuito, arquivos enviados somem a cada deploy/reinício; por isso a imagem fica numa coluna do banco."""
from flask import url_for

import imagens
from models import Product, db

CORES = {"azul": "Azul Marketplace B2B", "escuro": "Azul-escuro", "laranja": "Laranja", "verde": "Verde", "claro": "Claro"}
TIPOS = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg", b"GIF87a": "image/gif",
         b"GIF89a": "image/gif"}
MAX_IMAGEM = 10_000_000  # 10 MB por imagem enviada (é comprimida antes de ir para o banco)


def tipo_da_imagem(dados):
    """Confere pelos primeiros bytes (não pela extensão) se é PNG, JPG, GIF ou WEBP."""
    if dados[:4] == b"RIFF" and dados[8:12] == b"WEBP":
        return "image/webp"
    for assinatura, tipo in TIPOS.items():
        if dados.startswith(assinatura):
            return tipo
    return None


def ler_formulario(f, arquivo, banner=None):
    """Valida o formulário do painel. Devolve (dados, imagem (bytes, tipo) ou None, erro)."""
    produto = None
    if f.get("product_id"):
        produto = db.session.get(Product, f.get("product_id", type=int) or 0)
        if not produto:
            return None, None, "Produto não encontrado."
    imagem = None
    if arquivo and arquivo.filename:
        dados = arquivo.read(MAX_IMAGEM + 1)
        if len(dados) > MAX_IMAGEM:
            return None, None, "A imagem passa de 10 MB. Diminua e envie de novo."
        tipo = tipo_da_imagem(dados)
        if not tipo:
            return None, None, "Envie uma imagem PNG, JPG, WEBP ou GIF."
        imagem = imagens.comprimir(dados, tipo, imagens.LADO_BANNER)
    imagem_url = f.get("imagem_url", "").strip()
    if imagem_url and (not imagem_url.startswith("https://") or len(imagem_url) > 500):
        return None, None, "O link da imagem precisa começar com https://"
    link = f.get("link", "").strip()
    # o navegador ignora tabulação e quebra de linha no link: "/<TAB>/site.com" viraria "//site.com" (outro site)
    if link and (link.startswith("//") or "\\" in link or any(ord(ch) < 33 for ch in link)
                 or not link.startswith(("/", "https://", "http://"))):
        return None, None, "O link do clique precisa começar com / (página da loja) ou https://"
    tem_imagem = imagem or imagem_url or (banner and banner.imagem and "remover_imagem" not in f)
    titulo = " ".join(f.get("titulo", "").split())[:120]
    if not produto and not tem_imagem and not titulo:
        return None, None, "Escolha um produto, envie uma imagem ou escreva um título."
    try:
        ordem = int(f.get("ordem") or 0)
    except ValueError:
        ordem = 0
    cor = f.get("cor") if f.get("cor") in CORES else "azul"
    dados = dict(titulo=titulo, subtitulo=" ".join(f.get("subtitulo", "").split())[:200],
                 texto_botao=" ".join(f.get("texto_botao", "").split())[:40], link=link[:500], imagem_url=imagem_url,
                 product_id=produto.id if produto else None, cor=cor, ordem=ordem, ativo="ativo" in f)
    return dados, imagem, None


def link_do_produto(p):
    from produtos import link
    return link(p)


def _foto(p):
    from produtos import foto_principal
    return foto_principal(p)


def montar(banners, cliente=None, preco=None):
    """Transforma os banners em dicionários prontos para a página (o preço só vai para quem entrou)."""
    slides = []
    for b in banners:
        p = b.product if b.product_id else None
        if p is not None and not p.active:
            continue  # produto desativado: o banner some sozinho
        img = url_for("shop.banner_imagem", bid=b.id, v=b.versao) if b.imagem else (b.imagem_url or None)
        slides.append({
            "id": b.id, "cor": b.cor or "azul", "imagem": img,
            "so_imagem": bool(img) and not p and not b.titulo,
            "titulo": b.titulo or (p.name if p else ""),
            "subtitulo": b.subtitulo or (p.description if p and p.description and p.description != "Produto de exemplo."
                                         else ("Preço de atacado para empresas" if p else "")),
            "texto_botao": b.texto_botao or ("Ver produto" if p else ("Confira" if b.link else "")),
            "link": b.link or (link_do_produto(p) if p else ""),
            "produto": p, "preco": preco(p) if (p and cliente and preco) else None,
            "produto_img": _foto(p) if p else None,
        })
    return slides
