"""Lucro por pedido: o custo de cada item é guardado na hora da venda (custo médio das compras ou, sem compra, o da
formação de preço). Lucro estimado = receita (itens − cupom) − custo − impostos e despesas da venda do estado de
entrega (formação de preço). O frete não entra: ele passa para a transportadora."""
from decimal import Decimal

from sqlalchemy import event
from sqlalchemy.orm import Session

import estoque
import formacao
from models import Order, OrderItem, Product, User

CENTAVO = Decimal("0.01")
CEM = Decimal(100)


@event.listens_for(Session, "before_flush")
def _guardar_custo_do_item(session, contexto, instancias):
    """Todo item de pedido novo sai com o custo do produto naquele momento."""
    novos = [o for o in session.new if isinstance(o, OrderItem) and o.custo_unitario is None]
    if not novos:
        return
    with session.no_autoflush:
        for item in novos:
            p = item.product or session.get(Product, item.product_id)
            item.custo_unitario = estoque.custo_atual(p)


def _uf(o):
    return formacao.uf_do_pedido(o) or formacao.parametros()["loja"]


def do_pedido(o):
    """Receita, custo, impostos estimados e lucro de um pedido."""
    subtotal = sum((Decimal(i.unit_price) * i.quantity for i in o.items), Decimal(0))
    receita = subtotal - Decimal(o.desconto or 0)
    custo = sum((Decimal(i.custo_unitario) * i.quantity for i in o.items if i.custo_unitario is not None), Decimal(0))
    sem_custo = [i for i in o.items if i.custo_unitario is None]
    fator_cupom = receita / subtotal if subtotal else Decimal(0)
    impostos = sum((Decimal(i.unit_price) * i.quantity * fator_cupom * formacao.venda_pct(_uf(o), i.product) / CEM
                    for i in o.items), Decimal(0))
    venda = (impostos * CEM / receita) if receita else formacao.venda_pct(_uf(o))
    lucro = receita - custo - impostos
    return {"receita": receita.quantize(CENTAVO), "custo": custo.quantize(CENTAVO), "venda_pct": venda,
            "impostos": impostos.quantize(CENTAVO), "lucro": lucro.quantize(CENTAVO),
            "margem": (lucro * CEM / receita) if receita else Decimal(0), "sem_custo": sem_custo}


def _soma(grupo, chave, rotulo, receita, custo, impostos, tem_custo):
    g = grupo.setdefault(chave, {"chave": chave, "rotulo": rotulo, "receita": Decimal(0), "custo": Decimal(0),
                                 "impostos": Decimal(0), "qtd": 0, "sem_custo": 0})
    g["receita"] += receita
    g["custo"] += custo
    g["impostos"] += impostos
    if not tem_custo:
        g["sem_custo"] += 1
    return g


def _fechar(grupo):
    linhas = []
    for g in grupo.values():
        g["lucro"] = g["receita"] - g["custo"] - g["impostos"]
        g["margem"] = g["lucro"] * CEM / g["receita"] if g["receita"] else Decimal(0)
        linhas.append(g)
    return sorted(linhas, key=lambda g: g["lucro"], reverse=True)


def relatorio(de, ate, situacao="vendas"):
    from relatorios import _filtro, nome_cliente
    pedidos = Order.query.filter(*_filtro(de, ate, situacao)).all()
    usuarios = {u.id: u for u in User.query.filter(User.id.in_({o.user_id for o in pedidos if o.user_id})).all()}
    por = {"produto": {}, "categoria": {}, "cliente": {}, "estado": {}}
    total = {"receita": Decimal(0), "custo": Decimal(0), "impostos": Decimal(0), "itens_sem_custo": 0}
    for o in pedidos:
        subtotal = sum((Decimal(i.unit_price) * i.quantity for i in o.items), Decimal(0))
        fator_cupom = (subtotal - Decimal(o.desconto or 0)) / subtotal if subtotal else Decimal(0)
        uf = _uf(o) or "—"
        u = usuarios.get(o.user_id)
        for i in o.items:
            receita = Decimal(i.unit_price) * i.quantity * fator_cupom
            tem = i.custo_unitario is not None
            custo = Decimal(i.custo_unitario) * i.quantity if tem else Decimal(0)
            impostos = receita * formacao.venda_pct(_uf(o), i.product) / CEM
            total["receita"] += receita
            total["custo"] += custo
            total["impostos"] += impostos
            total["itens_sem_custo"] += 0 if tem else 1
            p = i.product
            _soma(por["produto"], i.product_id, p.name if p else "(excluído)", receita, custo, impostos, tem)["qtd"] += i.quantity
            cat = p.category.name if p and p.category else "(sem categoria)"
            _soma(por["categoria"], cat, cat, receita, custo, impostos, tem)["qtd"] += i.quantity
            _soma(por["cliente"], o.user_id or 0, nome_cliente(u) if u else o.customer_name, receita, custo,
                  impostos, tem)["qtd"] += i.quantity
            _soma(por["estado"], uf, uf, receita, custo, impostos, tem)["qtd"] += i.quantity
    total["lucro"] = total["receita"] - total["custo"] - total["impostos"]
    total["margem"] = total["lucro"] * CEM / total["receita"] if total["receita"] else Decimal(0)
    total["pedidos"] = len(pedidos)
    return {"de": de, "ate": ate, "situacao": situacao, "total": total,
            **{k: _fechar(v) for k, v in por.items()}}


def preencher_custos(de, ate):
    """Pedidos antigos (de antes do ERP) ficam sem custo: usa o custo de hoje. Devolve quantos itens mudaram."""
    from relatorios import _filtro
    itens = (OrderItem.query.join(Order, Order.id == OrderItem.order_id)
             .filter(*_filtro(de, ate, "vendas"), OrderItem.custo_unitario.is_(None)).all())
    n = 0
    for i in itens:
        c = estoque.custo_atual(i.product)
        if c is not None:
            i.custo_unitario = c
            n += 1
    return n


def planilha(r):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Resumo"
    t = r["total"]
    for linha in (["Período", f"{r['de']:%d/%m/%Y} a {r['ate']:%d/%m/%Y}"], ["Pedidos", t["pedidos"]],
                  ["Receita (itens − cupom)", float(t["receita"])], ["Custo das mercadorias", float(t["custo"])],
                  ["Impostos e despesas (estimados)", float(t["impostos"])], ["Lucro estimado", float(t["lucro"])],
                  ["Margem (%)", round(float(t["margem"]), 2)], ["Itens sem custo", t["itens_sem_custo"]]):
        ws.append(linha)
    for chave, titulo in (("produto", "Produtos"), ("categoria", "Categorias"), ("cliente", "Clientes"),
                          ("estado", "Estados")):
        aba = wb.create_sheet(titulo)
        aba.append([titulo[:-1], "Quantidade", "Receita", "Custo", "Impostos", "Lucro", "Margem (%)", "Itens sem custo"])
        for g in r[chave]:
            aba.append([str(g["rotulo"]), g["qtd"], float(g["receita"]), float(g["custo"]), float(g["impostos"]),
                        float(g["lucro"]), round(float(g["margem"]), 2), g["sem_custo"]])
    from planilha import arquivo_xlsx
    return arquivo_xlsx(wb)

