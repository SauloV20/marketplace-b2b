"""Histórico do pedido: quem fez o quê e quando (cliente, loja ou o próprio sistema).

Cada evento é gravado depois que a ação deu certo; se o registro falhar, a ação não é desfeita (só fica no log).
"""
from flask import current_app, session

from models import PedidoEvento, User, db


def _nome_do_admin():
    u = db.session.get(User, session.get("admin_id") or 0)
    return u.name if u else "Loja"


def registrar(pedido_id, texto, autor="loja", nome=None):
    """autor: "loja" (administrador logado), "cliente" ou "sistema"."""
    try:
        if autor == "loja" and nome is None:
            nome = _nome_do_admin()
        db.session.add(PedidoEvento(order_id=pedido_id, autor=autor, autor_nome=(nome or "")[:120],
                                    texto=" ".join(texto.split())[:500]))
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Não foi possível registrar o histórico do pedido %s", pedido_id)

