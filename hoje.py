"""Painel do dia: o que precisa de atenção agora, em cada área que o administrador pode ver."""
from datetime import timedelta
from decimal import Decimal

from b2b import hoje as dia_de_hoje
from formato import brl
from models import Compra, Lancamento, NotaFiscal, NotaRecebida, Order, Product, agora_utc, db

LIMITE = 6  # itens mostrados em cada quadro


def _soma(q):
    return sum((Decimal(l.valor) for l in q), Decimal(0))


def montar(pode, total=False):
    """Quadros do dia. `pode(area)` diz o que mostrar. Cada quadro: {titulo, n, valor, itens, link, nivel}."""
    d = dia_de_hoje()
    quadros = []

    def quadro(area, titulo, itens, link, nivel="atencao", valor=None, n=None):
        n = len(itens) if n is None else n
        if n:
            quadros.append({"area": area, "titulo": titulo, "n": n, "itens": itens[:LIMITE], "link": link,
                            "nivel": nivel, "valor": valor})

    if pode("pedidos"):
        orcamentos = Order.query.filter_by(status="orcamento").order_by(Order.id).all()
        quadro("pedidos", "Orçamentos esperando resposta", [(f"#{o.id} · {o.razao_social or o.customer_name}",
                                                             ("admin.orcamento", {"oid": o.id})) for o in orcamentos],
               ("admin.orders", {"status": "orcamento"}))
        enviar = (Order.query.filter(Order.envio == "", db.or_(Order.status == "pago", db.and_(
            Order.status == "pendente", Order.payment_method == "faturado"))).order_by(Order.id).all())
        quadro("pedidos", "Pedidos para separar e enviar", [(f"#{o.id} · {o.razao_social or o.customer_name}",
                                                              ("admin.pedido", {"oid": o.id})) for o in enviar],
               ("admin.orders", {}))
    if pode("financeiro"):
        abertas = Lancamento.query.filter(Lancamento.pago_em.is_(None))
        vencidas = abertas.filter(Lancamento.tipo == "receber", Lancamento.vencimento < d).order_by(Lancamento.vencimento).all()
        quadro("financeiro", "A receber vencido", [(f"{l.vencimento:%d/%m} · {brl(l.valor)}" +
                                                    (f" · pedido #{l.order_id}" if l.order_id else ""),
                                                    ("admin.financeiro_pagina", {"tipo": "receber", "situacao": "vencidas"}))
                                                   for l in vencidas],
               ("admin.financeiro_pagina", {"tipo": "receber", "situacao": "vencidas"}), "alerta", _soma(vencidas))
        pagar = abertas.filter(Lancamento.tipo == "pagar", Lancamento.vencimento <= d + timedelta(days=7)) \
            .order_by(Lancamento.vencimento).all()
        quadro("financeiro", "A pagar até daqui a 7 dias", [(f"{l.vencimento:%d/%m} · {brl(l.valor)} · "
                                                             + (l.descricao or l.categoria or "conta")[:40],
                                                             ("admin.financeiro_pagina", {"tipo": "pagar"})) for l in pagar],
               ("admin.financeiro_pagina", {"tipo": "pagar"}),
               "alerta" if any(l.vencimento < d for l in pagar) else "atencao", _soma(pagar))
    if pode("produtos"):
        baixos = [p for p in Product.query.filter(Product.active.is_(True), Product.estoque_alerta > 0).all()
                  if p.stock <= p.estoque_alerta]
        zerados = Product.query.filter(Product.active.is_(True), Product.stock <= 0).all()
        quadro("produtos", "Estoque zerado", [(p.name, ("admin.estoque_produto", {"pid": p.id})) for p in zerados],
               ("admin.estoque_lista", {"filtro": "zerado"}), "alerta")
        quadro("produtos", "Estoque baixo (no alerta)", [(f"{p.name} · {p.stock} un", ("admin.estoque_produto", {"pid": p.id}))
                                                        for p in baixos if p.stock > 0],
               ("admin.estoque_lista", {"filtro": "baixo"}))
    if pode("custos"):
        notas = NotaRecebida.query.filter(NotaRecebida.compra_id.is_(None), NotaRecebida.situacao == "autorizada") \
            .order_by(NotaRecebida.id).all()
        quadro("custos", "Notas de compra para lançar", [(f"{n.nome_emitente or n.cnpj_emitente} · NF {n.numero or ''}",
                                                          ("admin.nota_compra", {"rid": n.id})) for n in notas],
               ("admin.notas_compra", {}))
        atrasadas = Compra.query.filter(Compra.status == "enviado", Compra.previsao < d).all()
        quadro("custos", "Compras com entrega atrasada", [(f"#{c.id} · {c.fornecedor.nome} · previsão {c.previsao:%d/%m}",
                                                           ("admin.compra", {"cid": c.id})) for c in atrasadas],
               ("admin.compras_lista", {}))
    if pode("fiscal"):
        import fiscal
        if fiscal.emitente().get("ambiente") == "1":
            desde = agora_utc() - timedelta(days=30)
            com_nota = {n.order_id for n in NotaFiscal.query.filter(NotaFiscal.ambiente == 1,
                                                                    NotaFiscal.status == "autorizada").all()}
            sem = [o for o in Order.query.filter(Order.created_at >= desde, db.or_(
                Order.status == "pago", Order.envio != "")).order_by(Order.id).all() if o.id not in com_nota]
            quadro("fiscal", "Pedidos sem nota fiscal (30 dias)", [(f"#{o.id} · {o.razao_social or o.customer_name}",
                                                                    ("admin.pedido_nfe", {"oid": o.id})) for o in sem],
                   ("admin.notas_lista", {}))
        problemas = NotaFiscal.query.filter(NotaFiscal.status.in_(("rejeitada", "erro", "processando"))).all()
        quadro("fiscal", "Notas rejeitadas ou sem resposta", [(f"NF-e {n.numero} · {n.cstat} {n.motivo[:50]}",
                                                               ("admin.nota", {"nid": n.id})) for n in problemas],
               ("admin.notas_lista", {}), "alerta")
    if pode("financeiro") or pode("custos"):
        import margem
        if margem.modo() != "desligado":
            desde = agora_utc() - timedelta(days=30)
            baixos = []
            for o in Order.query.filter(Order.created_at >= desde, Order.status.in_(("orcamento", "aprovacao", "pendente",
                                                                                      "pago"))).order_by(Order.id.desc()).limit(300):
                avisos = margem.do_pedido(o)
                if avisos:
                    baixos.append((f"#{o.id} · {avisos[0][:70]}", ("admin.pedido", {"oid": o.id})))
            quadro("margem", "Vendas com margem abaixo do mínimo (30 dias)", baixos, ("admin.formacao_preco", {}), "alerta")
    if total:
        import backup_auto
        s = backup_auto.situacao()
        if not s["ultimo"] or agora_utc() - s["ultimo"] > timedelta(days=8):
            quadros.append({"area": "total", "titulo": "Backup semanal atrasado", "n": 1, "valor": None, "nivel": "alerta",
                            "itens": [(s["erro"] or "Nenhum backup automático na última semana.", ("admin.backup_pagina", {}))],
                            "link": ("admin.backup_pagina", {})})
    return quadros
