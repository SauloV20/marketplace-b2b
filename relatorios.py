"""Relatório de vendas do painel: resumo do período, produtos (curva ABC), clientes, formas de pagamento e
clientes que pararam de comprar. Também exporta tudo para Excel.

Conta como venda o pedido aguardando pagamento ou pago (cancelados e orçamentos ficam de fora); dá para olhar só os
pagos. As datas são as do pedido, no horário de Brasília.
"""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func

from formato import dia_br, hoje, inicio_do_dia
from models import Order, OrderItem, Product, User, agora_utc, db

PERIODOS = {"mes": "Este mês", "mes_passado": "Mês passado", "30": "Últimos 30 dias", "90": "Últimos 90 dias",
            "ano": "Este ano"}
SITUACOES = {"vendas": "Aguardando pagamento e pagos", "pagos": "Só pagos"}
DIAS_INATIVO = 60


def periodo(args):
    """Lê o período escolhido. Devolve (de, até, chave do atalho ou "")."""
    d = hoje()
    atalho = args.get("periodo", "")
    try:
        de = date.fromisoformat(args["de"]) if args.get("de") else None
        ate = date.fromisoformat(args["ate"]) if args.get("ate") else None
    except ValueError:
        de = ate = None
    if de or ate:
        de, ate = de or date(2000, 1, 1), ate or d
        return (ate, de, "") if de > ate else (de, ate, "")
    if atalho == "mes_passado":
        fim = d.replace(day=1) - timedelta(days=1)
        return fim.replace(day=1), fim, atalho
    if atalho in ("30", "90"):
        return d - timedelta(days=int(atalho) - 1), d, atalho
    if atalho == "ano":
        return d.replace(month=1, day=1), d, atalho
    return d.replace(day=1), d, "mes"


def _utc(dia):
    return inicio_do_dia(dia)


def _filtro(de, ate, situacao):
    status = ("pago",) if situacao == "pagos" else ("pendente", "pago")
    return [Order.status.in_(status), Order.created_at >= _utc(de), Order.created_at < _utc(ate + timedelta(days=1))]


def montar(de, ate, situacao="vendas"):
    filtro = _filtro(de, ate, situacao)
    qtd, total, descontos, fretes = db.session.query(
        func.count(Order.id), func.coalesce(func.sum(Order.total), 0), func.coalesce(func.sum(Order.desconto), 0),
        func.coalesce(func.sum(Order.frete), 0)).filter(*filtro).one()
    total, descontos, fretes = Decimal(total), Decimal(descontos), Decimal(fretes)

    linhas = (db.session.query(OrderItem.product_id, func.sum(OrderItem.quantity),
                               func.sum(OrderItem.quantity * OrderItem.unit_price),
                               func.count(func.distinct(OrderItem.order_id)))
              .join(Order, Order.id == OrderItem.order_id).filter(*filtro)
              .group_by(OrderItem.product_id).all())
    nomes = {p.id: p for p in Product.query.filter(Product.id.in_([l[0] for l in linhas]))} if linhas else {}
    produtos = sorted(({"p": nomes.get(pid), "qtd": int(q or 0), "receita": Decimal(r or 0), "pedidos": int(n)}
                       for pid, q, r, n in linhas), key=lambda x: x["receita"], reverse=True)
    receita_produtos = sum((x["receita"] for x in produtos), Decimal(0))
    acumulado = Decimal(0)
    for x in produtos:  # curva ABC: A = até 80% da receita, B = até 95%, C = o resto
        x["pct"] = (x["receita"] * 100 / receita_produtos) if receita_produtos else Decimal(0)
        x["classe"] = "A" if acumulado < receita_produtos * Decimal("0.8") else (
            "B" if acumulado < receita_produtos * Decimal("0.95") else "C")
        acumulado += x["receita"]

    por_cliente = (db.session.query(Order.user_id, func.count(Order.id), func.sum(Order.total), func.max(Order.created_at))
                   .filter(*filtro).group_by(Order.user_id).all())
    usuarios = {u.id: u for u in User.query.filter(User.id.in_([c[0] for c in por_cliente if c[0]]))} if por_cliente else {}
    clientes = sorted(({"u": usuarios.get(uid), "pedidos": int(n), "total": Decimal(t or 0), "ultimo": ult}
                       for uid, n, t, ult in por_cliente), key=lambda x: x["total"], reverse=True)

    pagamentos = sorted(((m or "-", int(n), Decimal(t or 0)) for m, n, t in db.session.query(
        Order.payment_method, func.count(Order.id), func.sum(Order.total)).filter(*filtro)
        .group_by(Order.payment_method).all()), key=lambda x: x[2], reverse=True)

    return {"de": de, "ate": ate, "situacao": situacao, "pedidos": int(qtd), "total": total, "descontos": descontos,
            "fretes": fretes, "ticket": (total / qtd).quantize(Decimal("0.01")) if qtd else Decimal(0),
            "itens": sum(x["qtd"] for x in produtos), "produtos": produtos, "clientes": clientes,
            "pagamentos": pagamentos, "inativos": inativos()}


def inativos(dias=DIAS_INATIVO, limite=30):
    """Clientes que já compraram, mas não fazem pedido há mais de `dias` dias (maiores compradores primeiro)."""
    corte = agora_utc() - timedelta(days=dias)
    q = (db.session.query(Order.user_id, func.max(Order.created_at), func.count(Order.id), func.sum(Order.total))
         .filter(Order.user_id.isnot(None), Order.status.in_(("pendente", "pago")))
         .group_by(Order.user_id).having(func.max(Order.created_at) < corte)
         .order_by(func.sum(Order.total).desc()).limit(limite).all())
    usuarios = {u.id: u for u in User.query.filter(User.id.in_([r[0] for r in q]))} if q else {}
    return [{"u": usuarios.get(uid), "ultimo": ult, "pedidos": int(n), "total": Decimal(t or 0),
             "dias": (agora_utc() - ult).days if ult else None} for uid, ult, n, t in q if usuarios.get(uid)]


def nome_cliente(u):
    if not u:
        return "Sem conta (pedido antigo ou conta excluída)"
    return u.razao_social or u.name


def planilha(r, metodos):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Resumo"
    for linha in (["Período", f"{r['de']:%d/%m/%Y} a {r['ate']:%d/%m/%Y}"], ["Pedidos", SITUACOES[r["situacao"]]],
                  ["Quantidade de pedidos", r["pedidos"]], ["Faturamento", float(r["total"])],
                  ["Ticket médio", float(r["ticket"])], ["Itens vendidos", r["itens"]],
                  ["Descontos de cupom", float(r["descontos"])], ["Frete cobrado", float(r["fretes"])]):
        ws.append(linha)
    prod = wb.create_sheet("Produtos")
    prod.append(["Curva", "Código", "Produto", "Quantidade", "Receita", "% da receita", "Pedidos"])
    for x in r["produtos"]:
        prod.append([x["classe"], (x["p"].codigo if x["p"] else "") or "", x["p"].name if x["p"] else "(excluído)",
                     x["qtd"], float(x["receita"]), round(float(x["pct"]), 2), x["pedidos"]])
    cli = wb.create_sheet("Clientes")
    cli.append(["Cliente", "CPF/CNPJ", "E-mail", "Pedidos", "Total", "Último pedido no período"])
    for x in r["clientes"]:
        u = x["u"]
        cli.append([nome_cliente(u), (u.documento if u else "") or "", (u.email if u else ""), x["pedidos"],
                    float(x["total"]), dia_br(x["ultimo"])])
    pag = wb.create_sheet("Pagamento")
    pag.append(["Forma de pagamento", "Pedidos", "Total"])
    for m, n, t in r["pagamentos"]:
        pag.append([metodos.get(m, m), n, float(t)])
    ina = wb.create_sheet("Pararam de comprar")
    ina.append(["Cliente", "E-mail", "Telefone", "Último pedido", "Dias sem comprar", "Pedidos", "Total comprado"])
    for x in r["inativos"]:
        u = x["u"]
        ina.append([nome_cliente(u), u.email, u.phone or "", dia_br(x["ultimo"]), x["dias"], x["pedidos"], float(x["total"])])
    from planilha import arquivo_xlsx
    return arquivo_xlsx(wb)
