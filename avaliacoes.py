"""Avaliações de produto: nota de 1 a 5 e comentário de quem comprou.

- Só avalia quem tem pedido pago ou enviado com o produto (o titular ou alguém da equipe da empresa).
- Uma avaliação por pessoa e produto; editar manda de volta para a moderação.
- Aparece na loja só depois de aprovada no painel (Marketing > Avaliações). O Marketplace B2B pode responder em público.
- A média e o total aprovado vão para a página do produto e para os dados do Google (estrelas na busca).
"""
from flask import url_for
from sqlalchemy import func

from email_utils import link_externo
from models import Avaliacao, Order, OrderItem, agora_utc, db

STATUS = {"pendente": "Aguardando aprovação", "aprovada": "Publicada", "recusada": "Não publicada"}
MAX_COMENTARIO = 1000


class ErroAvaliacao(Exception):
    pass


def nome_publico(user):
    """Primeiro nome e inicial do sobrenome ("Ana S."). Empresas aparecem pelo nome de quem avaliou."""
    partes = (user.name or "Cliente").split()
    return (partes[0] + (f" {partes[-1][0]}." if len(partes) > 1 else ""))[:60]


def comprou(titular, produto_id):
    """A empresa (ou pessoa) tem pedido pago ou enviado com este produto?"""
    if not titular:
        return False
    return db.session.query(Order.id).join(OrderItem).filter(
        Order.user_id == titular.id, OrderItem.product_id == produto_id,
        db.or_(Order.status == "pago", Order.envio != "")).first() is not None


def da_pessoa(pessoa, produto_id):
    return Avaliacao.query.filter_by(user_id=pessoa.id, product_id=produto_id).first() if pessoa else None


def salvar(titular, pessoa, produto, nota, comentario):
    """Cria ou edita a avaliação de `pessoa`. Volta para a moderação. Devolve a avaliação."""
    if not comprou(titular, produto.id):
        raise ErroAvaliacao("Só quem comprou este produto pode avaliar.")
    try:
        nota = int(nota)
    except (TypeError, ValueError):
        nota = 0
    if not 1 <= nota <= 5:
        raise ErroAvaliacao("Escolha uma nota de 1 a 5 estrelas.")
    comentario = "\n".join(" ".join(l.split()) for l in (comentario or "").strip().splitlines()).strip()
    if len(comentario) > MAX_COMENTARIO:
        raise ErroAvaliacao(f"O comentário pode ter até {MAX_COMENTARIO} caracteres.")
    a = da_pessoa(pessoa, produto.id)
    nova = a is None
    avisar = nova or a.status != "pendente"  # editar de novo antes da moderação não manda outro e-mail à loja
    if nova:
        a = Avaliacao(product_id=produto.id, user_id=pessoa.id)
        db.session.add(a)
    a.nota, a.comentario, a.nome = nota, comentario, nome_publico(pessoa)
    a.status, a.resposta, a.criado_em = "pendente", a.resposta if not nova else "", agora_utc()
    db.session.commit()
    if not avisar:
        return a
    import avisos
    avisos._admins(f"Marketplace B2B: nova avaliação de {produto.name}"[:150],
                   f"{pessoa.name} deu {nota} estrela(s) para {produto.name}." +
                   (f"\n\n\"{comentario}\"" if comentario else "") +
                   f"\n\nPublicar ou não: {link_externo(url_for('admin.avaliacoes_lista'))}", area="marketing")
    return a


def publicadas(produto_id, limite=30):
    return (Avaliacao.query.filter_by(product_id=produto_id, status="aprovada")
            .order_by(Avaliacao.criado_em.desc()).limit(limite).all())


def media(produto_id):
    """(média com uma casa, quantidade) das avaliações publicadas; (None, 0) sem nenhuma."""
    m, n = db.session.query(func.avg(Avaliacao.nota), func.count(Avaliacao.id)).filter(
        Avaliacao.product_id == produto_id, Avaliacao.status == "aprovada").one()
    return (round(float(m), 1) if n else None), n


def medias(produto_ids):
    """{id do produto: (média, quantidade)} para as listas de produtos."""
    if not produto_ids:
        return {}
    linhas = db.session.query(Avaliacao.product_id, func.avg(Avaliacao.nota), func.count(Avaliacao.id)).filter(
        Avaliacao.product_id.in_(list(produto_ids)), Avaliacao.status == "aprovada").group_by(Avaliacao.product_id)
    return {pid: (round(float(m), 1), n) for pid, m, n in linhas}


def moderar(a, acao, resposta=""):
    """Painel: aprovar, recusar ou só responder. Devolve a mensagem."""
    a.resposta = " ".join((resposta or "").split())[:500]
    if acao == "aprovar":
        a.status = "aprovada"
    elif acao == "recusar":
        a.status = "recusada"
    db.session.commit()
    return {"aprovar": "Avaliação publicada.", "recusar": "Avaliação não publicada."}.get(acao, "Resposta salva.")


def lista(status="pendente"):
    q = Avaliacao.query
    if status in STATUS:
        q = q.filter_by(status=status)
    return q.order_by(Avaliacao.criado_em.desc()).limit(300).all()


def pendentes():
    return Avaliacao.query.filter_by(status="pendente").count()
