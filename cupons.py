"""Cupons de desconto: o cliente digita o código no carrinho e o desconto sai do valor dos produtos.

O frete não entra no desconto, e o pedido mínimo da loja vale sobre os produtos antes do cupom.
Os usos são contados pelos pedidos gravados (cancelados não contam): não há contador para ficar fora de sincronia.
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from formato import brl, hoje, ler_numero, num
from models import Cupom, Order

CENTAVO = Decimal("0.01")


def normalizar(codigo):
    return "".join((codigo or "").split()).upper()[:30]


def usos(cupom, cliente=None):
    q = Order.query.filter(Order.cupom == cupom.codigo, Order.status != "cancelado")
    if cliente is not None:
        q = q.filter(Order.user_id == cliente.id)
    return q.count()


def calcular(cupom, subtotal):
    """Quanto o cupom desconta de um subtotal (nunca mais que o próprio subtotal)."""
    if cupom.tipo == "percentual":
        d = subtotal * cupom.valor / 100
    else:
        d = cupom.valor
    return max(min(Decimal(d), subtotal), Decimal(0)).quantize(CENTAVO)


def descricao(cupom):
    if cupom.tipo == "percentual":
        return f"{num(cupom.valor)}% de desconto"
    return brl(cupom.valor) + " de desconto"


def validar(codigo, cliente, subtotal, linhas=None):
    """Confere se o cupom vale para este cliente e este carrinho. Devolve (cupom, desconto, erro).

    Com as linhas do carrinho e a proteção de margem em "bloquear", recusa o cupom que derruba a margem."""
    zero = Decimal(0)
    c = Cupom.query.filter_by(codigo=normalizar(codigo)).first() if normalizar(codigo) else None
    if not c or not c.ativo:
        return None, zero, "Cupom não encontrado. Confira o código."
    if c.validade and hoje() > c.validade:
        return None, zero, f"O cupom {c.codigo} venceu em {c.validade.strftime('%d/%m/%Y')}."
    if c.limite_usos and usos(c) >= c.limite_usos:
        return None, zero, f"O cupom {c.codigo} já foi usado o máximo de vezes."
    if c.um_por_cliente and cliente is not None and usos(c, cliente):
        return None, zero, f"Você já usou o cupom {c.codigo} em outro pedido."
    if subtotal < c.minimo:
        return None, zero, (f"O cupom {c.codigo} vale para compras a partir de {brl(c.minimo)} em produtos. "
                            f"Faltam {brl(c.minimo - subtotal)}.")
    desconto = calcular(c, subtotal)
    if linhas:
        import formacao
        import margem
        if margem.bloqueia() and margem.das_linhas(linhas, formacao.uf_do_cliente(cliente), desconto):
            return None, zero, f"O cupom {c.codigo} não vale para estes produtos."
    return c, desconto, None


def do_carrinho(sessao, cliente, subtotal, linhas=None):
    """O cupom guardado no carrinho, conferido de novo agora. Devolve (cupom, desconto, erro); sem cupom: (None, 0, None)."""
    codigo = sessao.get("cupom")
    if not codigo:
        return None, Decimal(0), None
    return validar(codigo, cliente, subtotal, linhas)


def recalcular(pedido):
    """Desconto de um orçamento depois que a loja muda os preços: % recalcula, valor fixo fica (até o subtotal)."""
    if not pedido.cupom:
        return Decimal(0)
    subtotal = pedido.subtotal
    c = Cupom.query.filter_by(codigo=pedido.cupom).first()
    if c:
        return calcular(c, subtotal)
    return max(min(pedido.desconto or Decimal(0), subtotal), Decimal(0)).quantize(CENTAVO)


def ler(form, cid=None):
    """Valida o formulário do painel. Devolve (dados, erro)."""
    codigo = normalizar(form.get("codigo", ""))
    if not codigo or not codigo.replace("-", "").replace("_", "").isalnum():
        return None, "O código do cupom usa letras, números, - ou _ (sem espaços), até 30 caracteres."
    existente = Cupom.query.filter(Cupom.codigo == codigo, Cupom.id != (cid or 0)).first()
    if existente:
        return None, f"Já existe um cupom com o código {codigo}."
    tipo = form.get("tipo", "percentual")
    if tipo not in ("percentual", "valor"):
        return None, "Escolha o tipo do desconto."
    try:
        valor, minimo = ler_numero(form.get("valor"), 0), ler_numero(form.get("minimo"), 0)
    except InvalidOperation:
        return None, "Confira o valor do desconto e o mínimo da compra (números como 10 ou 150,00)."
    if not valor.is_finite() or not minimo.is_finite():
        return None, "Confira o valor do desconto e o mínimo da compra."
    if tipo == "percentual" and not 0 < valor <= 100:
        return None, "O desconto em % vai de 0,01 a 100."
    if tipo == "valor" and not 0 < valor < 1_000_000:
        return None, "Informe o desconto em reais (maior que zero)."
    if not 0 <= minimo < 100_000_000:
        return None, "O mínimo da compra não pode ser negativo."
    validade = None
    if form.get("validade"):
        try:
            validade = date.fromisoformat(form.get("validade"))
        except ValueError:
            return None, "Data de validade inválida."
    limite = None
    if (form.get("limite_usos") or "").strip():
        try:
            limite = int(form.get("limite_usos"))
        except ValueError:
            return None, "O limite de usos é um número inteiro (ou vazio, para não ter limite)."
        if not 1 <= limite <= 1_000_000:
            return None, "O limite de usos vai de 1 a 1.000.000 (ou vazio, para não ter limite)."
    return dict(codigo=codigo, tipo=tipo, valor=valor.quantize(CENTAVO), minimo=minimo.quantize(CENTAVO),
                validade=validade, limite_usos=limite, um_por_cliente="um_por_cliente" in form,
                ativo="ativo" in form or cid is None), None
