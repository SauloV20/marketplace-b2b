"""'Avise-me quando chegar': o cliente pede na página do produto esgotado e recebe um e-mail quando o estoque volta.

A loja confere os pedidos de aviso sempre que o estoque pode ter voltado: produto editado ou reativado no painel,
planilha importada, pedido cancelado ou excluído. Cada pedido de aviso dispara uma vez só.
"""
from flask import current_app, url_for

from b2b import disponivel
from email_utils import enviar_email, link_externo
from models import AvisoEstoque, Product, User, agora_utc, db

MAX_POR_VEZ = 300  # e-mails por conferência (o resto sai na próxima)


def pendente(cliente, produto):
    if not cliente:
        return None
    return AvisoEstoque.query.filter_by(user_id=cliente.id, product_id=produto.id, avisado_em=None).first()


def alternar(cliente, produto):
    """Liga ou desliga o aviso. Devolve True se ficou ligado."""
    a = pendente(cliente, produto)
    if a:
        db.session.delete(a)
        db.session.commit()
        return False
    db.session.add(AvisoEstoque(user_id=cliente.id, product_id=produto.id))
    db.session.commit()
    return True


def esperando(produto_id):
    return AvisoEstoque.query.filter_by(product_id=produto_id, avisado_em=None).count()


def do_cliente(cliente):
    return (AvisoEstoque.query.filter_by(user_id=cliente.id, avisado_em=None)
            .order_by(AvisoEstoque.criado_em.desc()).all())


def disparar():
    """Avisa quem está esperando produtos que voltaram a ter estoque. Devolve quantos e-mails saíram."""
    try:
        ids = [pid for (pid,) in db.session.query(AvisoEstoque.product_id)
               .filter(AvisoEstoque.avisado_em.is_(None)).distinct().all()]
        if not ids:
            return 0
        voltaram = [p for p in Product.query.filter(Product.id.in_(ids)).all() if disponivel(p)]
        enviados = 0
        for p in voltaram:
            link = link_externo(url_for("shop.produto", pid=p.id, nome=_slug(p)))
            for a in AvisoEstoque.query.filter_by(product_id=p.id, avisado_em=None).limit(MAX_POR_VEZ - enviados):
                u = db.session.get(User, a.user_id)
                if u and u.email:
                    enviar_email(u.email, f"Marketplace B2B: chegou {p.name}",
                                 f"Olá, {u.name.split()[0] if u.name else ''}!\n\nO produto que você pediu para "
                                 f"avisar voltou ao estoque no Marketplace B2B: {p.name}.\n\nVer produto: {link}\n\n"
                                 f"Garanta o seu antes que acabe de novo.\n\nEquipe Marketplace B2B")
                a.avisado_em = agora_utc()
                enviados += 1
            if enviados >= MAX_POR_VEZ:
                break
        db.session.commit()
        return enviados
    except Exception:  # um aviso que falha nunca atrapalha a operação que o chamou
        db.session.rollback()
        current_app.logger.exception("Falha ao enviar os avisos de estoque")
        return 0


def _slug(p):
    from produtos import slug
    return slug(p.name)
