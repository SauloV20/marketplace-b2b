"""Proteção de margem: avisa (ou bloqueia) venda com lucro abaixo do mínimo, inclusive abaixo do custo.

Lucro de um item = preço − custo (médio das compras ou da formação de preço) − impostos e despesas da venda (% do
estado de entrega e os percentuais do produto, da Formação de preço). Margem = lucro ÷ preço. Item sem custo cadastrado não é avaliado.

Onde vale (Formação de preço > Proteção de margem):
- "avisar": o painel mostra o aviso (edição de produto, pedido, orçamento, painel do dia);
- "bloquear": além de avisar, a loja recusa o preço editado no pedido, a aprovação do orçamento e o cupom que
  deixariam a venda abaixo do mínimo. O desconto da empresa e a promoção só geram aviso (a loja revê o cadastro).
"""
from decimal import Decimal

from b2b import config, salvar_config
from formato import brl, uma_casa

CEM = Decimal(100)
MODOS = {"desligado": "Desligada", "avisar": "Só avisar", "bloquear": "Avisar e bloquear"}


def modo():
    m = config("margem_modo", "avisar")
    return m if m in MODOS else "avisar"


def minimo():
    try:
        return Decimal(config("margem_minima", "0") or 0)
    except Exception:
        return Decimal(0)


def salvar(m, minima):
    salvar_config("margem_modo", m if m in MODOS else "avisar")
    salvar_config("margem_minima", minima)


def bloqueia():
    return modo() == "bloquear"


def avaliar(p, preco, uf=None, custo=None):
    """{"custo", "lucro", "margem", "abaixo", "prejuizo"} de vender `p` a `preco`, ou None sem custo/preço."""
    import estoque
    import formacao
    if modo() == "desligado":
        return None
    custo = estoque.custo_atual(p) if custo is None else Decimal(custo)
    preco = Decimal(preco or 0)
    if custo is None or preco <= 0:
        return None
    impostos = preco * formacao.venda_pct(uf, p) / CEM
    lucro = preco - custo - impostos
    margem = lucro * CEM / preco
    return {"custo": custo, "lucro": lucro, "margem": margem, "abaixo": margem < minimo(), "prejuizo": lucro < 0}


def texto(p, r):
    if r["prejuizo"]:
        return f"{p.name}: vendido abaixo do custo (prejuízo de {brl(-r['lucro'])} por unidade)"
    return f"{p.name}: margem de {uma_casa(r['margem'])}%, abaixo do mínimo de {uma_casa(minimo())}%"


def problemas_itens(itens, uf=None, desconto=Decimal(0)):
    """itens = [(produto, preço unitário, quantidade, custo ou None)]. O desconto do pedido (cupom) é rateado.
    Devolve a lista de avisos."""
    subtotal = sum((Decimal(preco) * q for _, preco, q, _ in itens), Decimal(0))
    fator = (subtotal - Decimal(desconto or 0)) / subtotal if subtotal else Decimal(1)
    avisos, vistos = [], set()
    for p, preco, _, custo in itens:
        r = avaliar(p, Decimal(preco) * fator, uf, custo)
        if r and r["abaixo"] and p.id not in vistos:
            vistos.add(p.id)
            avisos.append(texto(p, r))
    return avisos


def do_pedido(o):
    """Avisos de margem de um pedido (com o custo guardado em cada item)."""
    import formacao
    itens = [(i.product, i.unit_price, i.quantity, i.custo_unitario) for i in o.items
             if i.product and i.custo_unitario is not None]
    return problemas_itens(itens, formacao.uf_do_pedido(o) or None, o.desconto)


def das_linhas(lines, uf=None, desconto=Decimal(0)):
    """Avisos para as linhas do carrinho (custo de agora)."""
    itens = []
    for l in lines:
        if l["fechadas"]:
            itens.append((l["p"], l["unit"], l["fechadas"], None))
        if l["frac"]:
            itens.append((l["p"], l["unit_frac"], l["frac"], None))
    return problemas_itens(itens, uf, desconto)


def do_produto(p):
    """Avisos do cadastro: preço de tabela, faixas e promoção."""
    avisos = []
    precos = [("preço de tabela", p.price)] + [(f"faixa de {f.qtd_minima} un", f.preco) for f in p.faixas]
    if p.preco_promocional:
        precos.append(("preço promocional", p.preco_promocional))
    for rotulo, preco in precos:
        r = avaliar(p, preco)
        if r and r["abaixo"]:
            avisos.append(f"{rotulo}: " + texto(p, r).split(": ", 1)[1])
    return avisos
