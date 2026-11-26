"""Estoque do ERP: histórico de movimentos (kardex), ajuste, inventário, entrada de compra e custo médio.

As vendas, cancelamentos e alterações de pedido são registradas em models.baixar_estoque/devolver_estoque.
As mudanças feitas pelo cadastro (formulário do produto, planilha, ajuste, inventário) são registradas aqui, sozinhas,
antes de gravar no banco: quem mexer no estoque de um produto deixa a linha no histórico.
"""
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import event, inspect, select, update
from sqlalchemy.orm import Session

from models import MovimentoEstoque, Product, User, db

QUATRO = Decimal("0.0001")
TIPOS = {"venda": "Venda", "cancelamento": "Cancelamento", "pedido": "Alteração de pedido", "compra": "Compra recebida",
         "ajuste": "Ajuste", "inventario": "Inventário", "cadastro": "Cadastro",
         "devolucao": "Devolução de cliente"}
MOTIVOS_AJUSTE = ["Correção de contagem", "Perda ou avaria", "Uso interno", "Devolução de cliente", "Amostra", "Outro"]


def autor_atual():
    """Nome do administrador logado (para o histórico)."""
    from flask import has_request_context, session
    if not has_request_context() or not session.get("admin_id"):
        return ""
    with db.session.no_autoflush:
        u = db.session.get(User, session["admin_id"])
    return u.name if u else ""


@event.listens_for(Session, "before_flush")
def _registrar_mudancas_do_cadastro(session, contexto, instancias):
    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, Product):
            continue
        hist = inspect(obj).attrs.stock.history
        if not hist.added:
            continue
        novo = hist.added[0] or 0
        antigo = (hist.deleted[0] or 0) if hist.deleted else 0
        if novo == antigo:
            continue
        motivo = getattr(obj, "_motivo_estoque", None) or {}
        mov = MovimentoEstoque(tipo=motivo.get("tipo") or ("cadastro" if obj in session.new else "ajuste"),
                               quantidade=novo - antigo, saldo=novo, autor=(motivo.get("autor") or autor_atual())[:120],
                               observacao=(motivo.get("obs") or "")[:200])
        if obj.id:
            mov.product_id = obj.id
        else:
            mov.produto = obj
        session.add(mov)
        obj._motivo_estoque = None



def ajustar(p, novo, tipo="ajuste", obs=""):
    """Troca o estoque do produto pelo painel (o histórico registra a diferença)."""
    p._motivo_estoque = {"tipo": tipo, "obs": obs}
    p.stock = novo
    p.estoque_avulso = min(p.estoque_avulso or 0, novo)


def entrada_compra(pid, qtd, custo, compra_id, autor=""):
    """Soma ao estoque o que chegou de uma compra e recalcula o custo médio. Devolve o novo estoque."""
    db.session.execute(update(Product).where(Product.id == pid).values(stock=Product.stock + qtd)
                       .execution_options(synchronize_session=False))
    estoque, medio = db.session.execute(select(Product.stock, Product.custo_medio).where(Product.id == pid)).one()
    antes = max(estoque - qtd, 0)
    custo = Decimal(custo)
    if medio is None or not antes:
        novo_medio = custo
    else:
        novo_medio = (Decimal(medio) * antes + custo * qtd) / (antes + qtd)
    db.session.execute(update(Product).where(Product.id == pid)
                       .values(custo_medio=novo_medio.quantize(QUATRO, ROUND_HALF_UP))
                       .execution_options(synchronize_session=False))
    db.session.add(MovimentoEstoque(product_id=pid, tipo="compra", quantidade=qtd, saldo=estoque,
                                    custo_unitario=custo.quantize(QUATRO, ROUND_HALF_UP), compra_id=compra_id,
                                    autor=autor[:120]))
    return estoque


def custo_atual(p):
    """Custo de uma unidade agora: o médio das compras recebidas (mais os percentuais sobre o custo); sem compra, o da
    formação de preço."""
    if p is None:
        return None
    import formacao
    if p.custo_medio is not None and p.custo_medio > 0:
        extra = formacao.soma_percentuais(p, base="custo")  # percentuais sobre o custo (embalagem, etc.)
        return (Decimal(p.custo_medio) * (100 + extra) / 100).quantize(QUATRO, ROUND_HALF_UP) if extra \
            else Decimal(p.custo_medio)
    c = formacao.fonte(p)
    return formacao.custo_de(c)["total"].quantize(QUATRO, ROUND_HALF_UP) if c else None


def historico(pid, limite=300):
    return (MovimentoEstoque.query.filter_by(product_id=pid)
            .order_by(MovimentoEstoque.quando.desc(), MovimentoEstoque.id.desc()).limit(limite).all())


def ultimos_movimentos(pids):
    """{product_id: data do último movimento}"""
    if not pids:
        return {}
    linhas = (db.session.query(MovimentoEstoque.product_id, db.func.max(MovimentoEstoque.quando))
              .filter(MovimentoEstoque.product_id.in_(list(pids))).group_by(MovimentoEstoque.product_id).all())
    return dict(linhas)


def ler_inventario(form, produtos):
    """Quantidades contadas (só os campos preenchidos). Devolve ({produto: contado}, erro)."""
    contados = {}
    for p in produtos:
        texto = (form.get(f"contado_{p.id}") or "").strip()
        if not texto:
            continue
        try:
            q = int(texto)
        except ValueError:
            return None, f"{p.name}: a contagem precisa ser um número inteiro."
        if not 0 <= q <= 10_000_000:
            return None, f"{p.name}: a contagem vai de 0 a 10.000.000."
        if q != p.stock:
            contados[p] = q
    return contados, None
