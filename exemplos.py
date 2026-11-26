"""Dados de exemplo que o `flask init-db` cria (80 pedidos de "Cliente demo" e o catálogo de amostra).

Antes de abrir a loja, o dono tira esses dados do caminho pelo painel (Backup e espaço):
- os pedidos de exemplo são apagados (eles nunca mexeram no estoque, então o estoque não volta);
- os produtos de amostra que ninguém editou são só desativados (somem da loja e podem ser reativados).
Pedidos e produtos de verdade nunca entram nestas listas."""
from datetime import datetime, timedelta

from sqlalchemy import or_

from models import Order, OrderItem, Product, agora_utc, db

NOME_DEMO = "Cliente demo"
DESCRICAO_DEMO = "Produto de exemplo."
BACKUP_RECENTE = timedelta(hours=24)


def pedidos():
    """Pedidos criados pelo catálogo de exemplo: sem conta, sem e-mail e sem documento."""
    return Order.query.filter(Order.customer_name == NOME_DEMO, Order.user_id.is_(None),
                              or_(Order.customer_email.is_(None), Order.customer_email == ""),
                              or_(Order.documento.is_(None), Order.documento == ""))


def produtos():
    """Produtos de amostra que continuam como vieram: descrição padrão, sem foto e sem venda de verdade."""
    reais = db.session.query(OrderItem.product_id).join(Order).filter(Order.id.notin_(pedidos().with_entities(Order.id)))
    return [p for p in Product.query.filter(Product.active.is_(True), Product.description == DESCRICAO_DEMO,
                                            Product.id.notin_(reais)).order_by(Product.name).all()
            if not p.fotos and not (p.image_url or "").strip()]


def backup_recente():
    from b2b import config
    try:
        quando = datetime.fromisoformat(config("ultimo_backup_em", ""))
    except ValueError:
        return False
    return agora_utc() - quando < BACKUP_RECENTE


def resumo():
    return {"pedidos": pedidos().count(), "produtos": produtos(), "backup_recente": backup_recente()}


def limpar(desativar_produtos):
    """Apaga os pedidos de exemplo e (se pedido) desativa os produtos de amostra. Devolve (pedidos, produtos)."""
    lista = produtos() if desativar_produtos else []
    n = 0
    for o in pedidos().all():
        for nota in list(o.notas):  # nota de teste fica no histórico fiscal, sem o pedido
            nota.order_id = None
        db.session.delete(o)  # sem cancelar antes: o pedido de exemplo nunca baixou o estoque
        n += 1
    for p in lista:
        p.active = False
    db.session.commit()
    return n, len(lista)
