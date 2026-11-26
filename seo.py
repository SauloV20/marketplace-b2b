"""Para aparecer no Google: sitemap.xml, robots.txt e o que não deve ser indexado.

O endereço do painel (ADMIN_PATH) nunca aparece aqui: ele continua secreto; as páginas do painel levam
"noindex" no cabeçalho da resposta."""
from xml.sax.saxutils import escape

from flask import current_app, request, url_for

from models import Category, Product

# páginas da loja que não devem ir para o Google (privadas ou sem conteúdo útil)
PRIVADAS_LOJA = {"shop.cart", "shop.checkout", "shop.pedido", "shop.pedido_pix", "shop.rapido", "shop.cart_frete",
                 "shop.cart_add", "shop.cart_update", "shop.cart_cep", "shop.atendente_responder"}
PUBLICAS_CONTA = {"account.cadastro"}


def base():
    return (current_app.config.get("SITE_URL") or request.url_root).rstrip("/")


def nao_indexar():
    """True nas páginas privadas (conta, carrinho, pedidos, painel) e nas buscas."""
    ep = request.endpoint or ""
    if request.blueprint == "admin":
        return True
    if request.blueprint == "account" and ep not in PUBLICAS_CONTA:
        return True
    if ep in PRIVADAS_LOJA:
        return True
    filtros = any(request.args.get(k) for k in ("q", "ordem", "disp", "min", "max"))
    return ep == "shop.index" and filtros  # resultados de busca e listas filtradas


def robots():
    return "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /carrinho",
        "Disallow: /checkout",
        "Disallow: /pedido/",
        "Disallow: /pedido-rapido",
        "Disallow: /conta",
        "Disallow: /atendente",
        "Disallow: /*?q=",
        "",
        f"Sitemap: {base()}/sitemap.xml",
        "",
    ])


def sitemap():
    from produtos import link
    b = base()
    urls = [(f"{b}/", None, "daily", "1.0")]
    urls += [(f"{b}/?cat={c.id}", None, "weekly", "0.8") for c in Category.query.order_by(Category.name).all()
             if any(p.active for p in c.products)]
    for p in Product.query.filter_by(active=True).order_by(Product.id).all():
        quando = p.created_at.date().isoformat() if p.created_at else None
        urls.append((b + link(p), quando, "weekly", "0.7"))
    urls += [(b + url_for(e), None, "yearly", "0.3") for e in ("account.cadastro", "shop.privacidade", "shop.termos")]
    linhas = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, quando, freq, prio in urls:
        linhas.append(f"<url><loc>{escape(loc)}</loc>" + (f"<lastmod>{quando}</lastmod>" if quando else "")
                      + f"<changefreq>{freq}</changefreq><priority>{prio}</priority></url>")
    linhas.append("</urlset>")
    return "\n".join(linhas)
