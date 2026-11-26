"""Compras do ERP: sugestão de reposição, pedido ao fornecedor e entrada da mercadoria (estoque, custo médio e
contas a pagar)."""
import math
from datetime import timedelta
from decimal import Decimal

from b2b import hoje, ler_prazos
from formato import brl, ler_numero
import estoque
import formacao
from models import Compra, CompraItem, Lancamento, Order, OrderItem, Product, ProdutoFornecedor, agora_utc, db

CENTAVO = Decimal("0.01")
CEM = Decimal(100)
STATUS = {"rascunho": "Rascunho", "enviado": "Enviado ao fornecedor", "recebido": "Recebido", "cancelado": "Cancelado"}
ABERTAS = ("rascunho", "enviado")
DIAS_VENDAS = 90  # média de vendas dos últimos 90 dias


# ---------- sugestão de compra ----------
def vendas_por_mes(dias=DIAS_VENDAS):
    """{product_id: unidades vendidas por mês (média)} dos pedidos aguardando pagamento e pagos."""
    desde = agora_utc() - timedelta(days=dias)
    linhas = (db.session.query(OrderItem.product_id, db.func.sum(OrderItem.quantity))
              .join(Order, Order.id == OrderItem.order_id)
              .filter(Order.status.in_(("pendente", "pago")), Order.created_at >= desde)
              .group_by(OrderItem.product_id).all())
    return {pid: Decimal(int(q or 0)) * 30 / dias for pid, q in linhas}


def em_compra(exceto=None):
    """{product_id: unidades em compras ainda não recebidas}"""
    q = (db.session.query(CompraItem.product_id, db.func.sum(CompraItem.quantidade))
         .join(Compra, Compra.id == CompraItem.compra_id).filter(Compra.status.in_(ABERTAS)))
    if exceto:
        q = q.filter(Compra.id != exceto)
    return {pid: int(n or 0) for pid, n in q.group_by(CompraItem.product_id).all()}


def sugestao(cobertura_meses=1):
    """Produtos para repor: estoque + o que já está em compra não cobre o alerta mais `cobertura_meses` de vendas.

    Devolve {fornecedor (ou None): [linhas]} com a quantidade sugerida arredondada para a embalagem (múltiplo)."""
    vendas, pedidos = vendas_por_mes(), em_compra()
    grupos = {}
    for p in Product.query.filter(Product.active.is_(True)).order_by(Product.name).all():
        mensal = vendas.get(p.id, Decimal(0))
        alvo = Decimal(p.estoque_alerta or 0) + mensal * Decimal(cobertura_meses)
        falta = alvo - p.stock - pedidos.get(p.id, 0)
        if falta <= 0 or (not mensal and not p.estoque_alerta):
            continue
        mult = max(p.multiplo or 1, 1)
        qtd = math.ceil(falta / mult) * mult
        fonte = formacao.fonte(p)
        grupos.setdefault(fonte.fornecedor if fonte else None, []).append(
            {"p": p, "mensal": mensal, "em_compra": pedidos.get(p.id, 0), "sugerido": qtd,
             "preco": Decimal(fonte.preco_compra) if fonte else None})
    return grupos


# ---------- pedido de compra ----------
def _preco_do_fornecedor(p, fornecedor):
    link = next((c for c in p.custos if c.fornecedor_id == fornecedor.id), None)
    if link:
        return Decimal(link.preco_compra), Decimal(link.ipi_pct or 0), Decimal(link.st_pct or 0)
    return Decimal(p.custo_medio or 0), Decimal(0), Decimal(0)


def nova(fornecedor, itens=(), autor=""):
    """Cria um rascunho de compra com os itens [(produto, quantidade)] e os preços do cadastro do fornecedor."""
    c = Compra(fornecedor=fornecedor, status="rascunho", autor=autor[:120])
    for p, qtd in itens:
        incluir(c, p, qtd)
    db.session.add(c)
    return c


def incluir(c, p, qtd):
    item = next((i for i in c.itens if i.product_id == p.id), None)
    if item:
        item.quantidade += qtd
        return item
    preco, ipi, st = _preco_do_fornecedor(p, c.fornecedor)
    item = CompraItem(product=p, quantidade=qtd, preco=preco, ipi_pct=ipi, st_pct=st)
    c.itens.append(item)
    return item


def totais(c, recebido=False):
    produtos = impostos = Decimal(0)
    for i in c.itens:
        q = (i.recebido or 0) if recebido else i.quantidade
        base = Decimal(i.preco) * q
        produtos += base
        impostos += base * (Decimal(i.ipi_pct or 0) + Decimal(i.st_pct or 0)) / CEM
    frete = Decimal(c.frete or 0)
    return {"produtos": produtos.quantize(CENTAVO), "impostos": impostos.quantize(CENTAVO), "frete": frete,
            "total": (produtos + impostos + frete).quantize(CENTAVO)}


def ler_rascunho(c, f):
    """Grava quantidades, preços, frete, condição, previsão e observações do rascunho. Devolve erro ou None."""
    from datetime import date
    try:
        for i in list(c.itens):
            qtd = int(f.get(f"qtd_{i.id}", i.quantidade) or 0)
            preco = ler_numero(f.get(f"preco_{i.id}", str(i.preco)), 0, Decimal("0.0001"))
            ipi = ler_numero(f.get(f"ipi_{i.id}", str(i.ipi_pct)), 0, CENTAVO)
            st = ler_numero(f.get(f"st_{i.id}", str(i.st_pct)), 0, CENTAVO)
            if not (0 <= qtd <= 10_000_000 and 0 <= preco < 100_000_000 and 0 <= ipi <= 100 and 0 <= st <= 100):
                raise ValueError
            if qtd == 0:
                c.itens.remove(i)
                continue
            i.quantidade, i.preco, i.ipi_pct, i.st_pct = qtd, preco, ipi, st
        frete = ler_numero(f.get("frete", ""), 0, CENTAVO)
        if not 0 <= frete < 10_000_000:
            raise ValueError
    except (ValueError, ArithmeticError):
        return "Confira quantidades (números inteiros), preços, IPI, ST e frete (como 16,50)."
    prazos, erro = ler_prazos(f.get("condicao", ""))
    if erro or len(prazos) > 1:
        return erro or "Informe uma só condição de pagamento, como 28 ou 30/60/90 (vazio = à vista)."
    try:
        previsao = date.fromisoformat(f["previsao"]) if f.get("previsao") else None
    except ValueError:
        return "Data de previsão inválida."
    c.frete, c.condicao, c.previsao = frete, (prazos[0] if prazos else ""), previsao
    c.observacoes = (f.get("observacoes") or "").strip()[:2000]
    return None


def texto_para_fornecedor(c):
    """Mensagem com o pedido, para colar no WhatsApp ou no e-mail."""
    linhas = [f"Pedido de compra #{c.id}", ""]
    for i in c.itens:
        cod = next((x.codigo for x in i.product.custos if x.fornecedor_id == c.fornecedor_id and x.codigo), "")
        linhas.append(f"- {i.quantidade} un · {i.product.name}" + (f" (cód. {cod})" if cod else "")
                      + f" · {brl(i.preco)}")
    t = totais(c)
    linhas += ["", f"Total dos produtos: {brl(t['produtos'])}"]
    if c.condicao:
        linhas.append(f"Pagamento: {c.condicao} dias")
    if c.previsao:
        linhas.append(f"Entrega desejada até {c.previsao:%d/%m/%Y}")
    if c.observacoes:
        linhas += ["", c.observacoes]
    return "\n".join(linhas)


# ---------- recebimento ----------
def custo_da_unidade(c, i, total_produtos):
    """Custo de cada unidade que chegou: preço + IPI + ST + frete (o desta compra, rateado pelo valor; sem ele,
    a % do fornecedor) − créditos de impostos do fornecedor."""
    preco = Decimal(i.preco)
    custo = preco * (CEM + Decimal(i.ipi_pct or 0) + Decimal(i.st_pct or 0)
                     - Decimal(c.fornecedor.credito_pct or 0)) / CEM
    if c.frete and total_produtos:
        custo += Decimal(c.frete) * preco / total_produtos
    else:
        custo += preco * formacao.frete_compra_pct(c.fornecedor) / CEM
    return custo


def receber(c, f, autor=""):
    """Dá entrada no estoque do que chegou, atualiza o custo médio e cria as contas a pagar.

    Devolve (ok, mensagem, ids dos produtos que mudaram). Em erro, nada é gravado."""
    if c.status not in ABERTAS:
        return False, "Esta compra já foi recebida ou cancelada.", []
    try:
        for i in c.itens:
            i.recebido = int(f.get(f"recebido_{i.id}", i.quantidade) or 0)
            preco = ler_numero(f.get(f"preco_{i.id}", str(i.preco)), 0, Decimal("0.0001"))
            if not (0 <= i.recebido <= 10_000_000 and 0 <= preco < 100_000_000):
                raise ValueError
            i.preco = preco
        frete = ler_numero(f.get("frete", str(c.frete or 0)), 0, CENTAVO)
        if not 0 <= frete < 10_000_000:
            raise ValueError
    except (ValueError, ArithmeticError):
        db.session.rollback()
        return False, "Confira as quantidades recebidas, os preços e o frete.", []
    if not any(i.recebido for i in c.itens):
        db.session.rollback()
        return False, "Nenhum item chegou: para desistir da compra, cancele.", []
    prazos, erro = ler_prazos(f.get("condicao", c.condicao or ""))
    if erro or len(prazos) > 1:
        db.session.rollback()
        return False, erro or "Informe uma só condição de pagamento, como 28 ou 30/60/90.", []
    c.frete, c.condicao = frete, (prazos[0] if prazos else "")
    c.nota = (f.get("nota") or "").strip()[:60]
    total_produtos = sum((Decimal(i.preco) * (i.recebido or 0) for i in c.itens), Decimal(0))
    atualizar = "atualizar_precos" in f
    mudaram = []
    for i in c.itens:
        if not i.recebido:
            continue
        estoque.entrada_compra(i.product_id, i.recebido, custo_da_unidade(c, i, total_produtos), c.id, autor)
        mudaram.append(i.product_id)
        if atualizar:
            p = i.product
            link = next((x for x in p.custos if x.fornecedor_id == c.fornecedor_id), None)
            if link:
                link.preco_compra, link.ipi_pct, link.st_pct = i.preco, i.ipi_pct, i.st_pct
            else:
                db.session.add(ProdutoFornecedor(product=p, fornecedor=c.fornecedor, preco_compra=i.preco,
                                                 ipi_pct=i.ipi_pct, st_pct=i.st_pct, principal=not p.custos))
    c.status, c.recebido_em = "recebido", agora_utc()
    _contas_a_pagar(c)
    if atualizar:
        formacao.recalcular(mudaram)
    return True, f"Compra #{c.id} recebida: {len(mudaram)} produto(s) entraram no estoque.", mudaram


def _contas_a_pagar(c):
    total = totais(c, recebido=True)["total"]
    if total <= 0:
        return
    dias = [int(d) for d in c.condicao.split("/")] if c.condicao else [0]
    valor = (total / len(dias)).quantize(CENTAVO, rounding="ROUND_DOWN")
    for n, d in enumerate(dias, 1):
        db.session.add(Lancamento(
            tipo="pagar", compra=c, fornecedor_id=c.fornecedor_id, categoria="Compra de mercadoria",
            descricao=f"Compra #{c.id} · {c.fornecedor.nome}" + (f" · NF {c.nota}" if c.nota else ""),
            parcela=f"{n}/{len(dias)}" if len(dias) > 1 else "", vencimento=hoje() + timedelta(days=d),
            valor=valor if n < len(dias) else total - valor * (len(dias) - 1)))


