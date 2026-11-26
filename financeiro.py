"""Financeiro do ERP: contas a receber (parcelas dos pedidos, criadas e baixadas sozinhas), contas a pagar (compras
recebidas e despesas lançadas à mão) e fluxo de caixa.

Parcelas de um pedido: no boleto faturado, uma por prazo (30/60/90 dias a partir da data do pedido); nas outras
formas, uma só, vencendo no dia do pedido. O pedido pago baixa as parcelas em aberto; cancelado ou excluído, apaga
as que não foram pagas. Quando a loja baixa a última parcela pelo financeiro, o pedido passa para pago.
"""
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal

from formato import data_br, hoje
from models import Lancamento, Order, db

CENTAVO = Decimal("0.01")
CATEGORIAS = {"pagar": ["Compra de mercadoria", "Frete", "Aluguel", "Salários e pró-labore", "Impostos",
                        "Energia, água e internet", "Marketing", "Manutenção", "Devolução a cliente", "Outros"],
              "receber": ["Venda", "Outros"]}
FORMAS = {"pix": "Pix", "boleto": "Boleto", "cartao": "Cartão", "transferencia": "Transferência", "dinheiro": "Dinheiro",
          "faturado": "Boleto faturado", "whatsapp": "Combinado", "orcamento": "Orçamento"}


# ---------- parcelas dos pedidos ----------
def parcelas_do_pedido(o):
    total = Decimal(o.total or 0).quantize(CENTAVO)
    if total <= 0:
        return []
    base = data_br(o.created_at)
    dias = [int(d) for d in (o.prazo or "").split("/") if d.isdigit()] if o.payment_method == "faturado" else []
    dias = dias or [0]
    valor = (total / len(dias)).quantize(CENTAVO, ROUND_DOWN)
    return [(f"{n}/{len(dias)}" if len(dias) > 1 else "", base + timedelta(days=d),
             valor if n < len(dias) else total - valor * (len(dias) - 1)) for n, d in enumerate(dias, 1)]


def pedido_mudou(o, status=None, pago_em=None):
    """Acerta as contas a receber do pedido com o status `status` (o pedido em memória pode estar com o antigo)."""
    status = status or o.status
    abertos = [l for l in o.lancamentos if l.pago_em is None]
    pagos = [l for l in o.lancamentos if l.pago_em is not None]
    if status not in ("pendente", "pago"):
        for l in abertos:
            _cancelar_boleto(l)
            o.lancamentos.remove(l)
        if o.cartao_aberto:
            import cartao_efi
            cartao_efi.cancelar(o)
        return
    if not pagos:
        desejadas = parcelas_do_pedido(o)
        if [(l.parcela, l.vencimento, Decimal(l.valor)) for l in abertos] != desejadas:
            for l in abertos:
                _cancelar_boleto(l)
                o.lancamentos.remove(l)
            for parcela, vencimento, valor in desejadas:
                o.lancamentos.append(Lancamento(tipo="receber", categoria="Venda", parcela=parcela, valor=valor,
                                                vencimento=vencimento, forma=o.payment_method or ""))
    if status == "pago":
        for l in o.lancamentos:
            if l.pago_em is None:
                l.pago_em, l.valor_pago = pago_em or hoje(), l.valor


def _cancelar_boleto(l):
    if l.boleto_id:
        import boleto_efi
        boleto_efi.cancelar(l)


def recebido(l, quando, valor, forma=""):
    """Baixa a parcela; se era a última do pedido, ele passa para pago (com o e-mail ao cliente). Devolve o texto."""
    from models import mudar_status
    l.pago_em, l.valor_pago = quando, valor
    if forma:
        l.forma = forma
    db.session.commit()
    o = l.order
    if o and o.status == "pendente" and all(x.pago_em for x in o.lancamentos):
        ok, _ = mudar_status(o, "pago")
        if ok:
            import avisos
            import historico
            historico.registrar(o.id, "Pagamento confirmado: todas as parcelas recebidas", autor="sistema")
            avisos.pagamento_confirmado(o)
            return f"todas as parcelas recebidas, o pedido #{o.id} passou para pago."
    return "parcela baixada."


def sincronizar():
    """Rede de segurança: pedidos sem parcelas (feitos antes do financeiro), pagos ou cancelados com parcela aberta."""
    sem = Order.query.filter(Order.status.in_(("pendente", "pago")), ~Order.lancamentos.any()).all()
    for o in sem:
        pedido_mudou(o, pago_em=data_br(o.created_at) if o.status == "pago" else None)
    abertos = Lancamento.pago_em.is_(None)
    for o in Order.query.filter(Order.status.notin_(("pendente",)), Order.lancamentos.any(abertos)).all():
        pedido_mudou(o)
    db.session.commit()  # (a consulta acima já gravou parte das mudanças: confirma tudo)


def descricao(l):
    if l.order_id:
        nome = (l.order.razao_social or l.order.customer_name) if l.order else ""
        return f"Pedido #{l.order_id}" + (f" · {nome}" if nome else "")
    return l.descricao or l.categoria or "Lançamento"


# ---------- lançamentos à mão ----------
def _valor(texto):
    t = (texto or "").replace("R$", "").replace(" ", "")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    v = Decimal(t)
    if not v.is_finite():
        raise ArithmeticError
    return v.quantize(CENTAVO)


def _data(texto, padrao=None):
    return date.fromisoformat(texto) if texto else padrao


def ler_lancamento(f):
    """Conta lançada à mão. Devolve (dados, repetir, erro)."""
    tipo = f.get("tipo")
    if tipo not in CATEGORIAS:
        return None, 0, "Escolha se é a pagar ou a receber."
    descricao_ = " ".join((f.get("descricao") or "").split())[:160]
    if not descricao_:
        return None, 0, "Descreva a conta (ex.: Aluguel do galpão)."
    try:
        valor = _valor(f.get("valor"))
        vencimento = _data(f.get("vencimento"))
        repetir = int(f.get("repetir") or 1)
    except (ArithmeticError, ValueError):
        return None, 0, "Confira valor (como 1.500,00), vencimento e repetições."
    if not vencimento:
        return None, 0, "Informe o vencimento."
    if not 0 < valor < 100_000_000:
        return None, 0, "O valor precisa ser maior que zero."
    if not 1 <= repetir <= 60:
        return None, 0, "Repita de 1 a 60 meses."
    categoria = f.get("categoria") if f.get("categoria") in CATEGORIAS[tipo] else "Outros"
    return {"tipo": tipo, "descricao": descricao_, "valor": valor, "vencimento": vencimento, "categoria": categoria,
            "observacao": (f.get("observacao") or "").strip()[:300]}, repetir, None


def mais_meses(d, n):
    """Mesmo dia `n` meses depois (no fim do mês quando o dia não existe, como 31 em fevereiro)."""
    mes = d.month - 1 + n
    ano, mes = d.year + mes // 12, mes % 12 + 1
    for dia in (d.day, 30, 29, 28):
        try:
            return date(ano, mes, dia)
        except ValueError:
            continue


def criar(dados, repetir=1, pago=False):
    criados = []
    for n in range(repetir):
        l = Lancamento(**{**dados, "vencimento": mais_meses(dados["vencimento"], n),
                          "parcela": f"{n + 1}/{repetir}" if repetir > 1 else ""})
        if pago and n == 0:
            l.pago_em, l.valor_pago = min(l.vencimento, hoje()), l.valor
        db.session.add(l)
        criados.append(l)
    return criados


def baixar(l, f):
    """Marca como paga/recebida. Devolve erro ou None."""
    try:
        quando = _data(f.get("pago_em"), hoje())
        valor = _valor(f.get("valor_pago")) if (f.get("valor_pago") or "").strip() else Decimal(l.valor)
    except (ArithmeticError, ValueError):
        return "Confira a data e o valor pago."
    if not 0 <= valor < 100_000_000:
        return "Valor pago inválido."
    l.pago_em, l.valor_pago = quando, valor
    if f.get("forma") in FORMAS:
        l.forma = f["forma"]
    return None


# ---------- painéis ----------
def resumo():
    d = hoje()
    abertos = Lancamento.query.filter(Lancamento.pago_em.is_(None)).all()
    r = {}
    for tipo in ("receber", "pagar"):
        do_tipo = [l for l in abertos if l.tipo == tipo]
        r[tipo] = {"aberto": sum((Decimal(l.valor) for l in do_tipo), Decimal(0)),
                   "vencido": sum((Decimal(l.valor) for l in do_tipo if l.vencimento < d), Decimal(0)),
                   "semana": sum((Decimal(l.valor) for l in do_tipo if d <= l.vencimento <= d + timedelta(days=7)),
                                 Decimal(0))}
    return r


def lista(tipo, situacao="abertas", de=None, ate=None):
    q = Lancamento.query.filter(Lancamento.tipo == tipo)
    if situacao == "abertas":
        q = q.filter(Lancamento.pago_em.is_(None)).order_by(Lancamento.vencimento, Lancamento.id)
    elif situacao == "vencidas":
        q = q.filter(Lancamento.pago_em.is_(None), Lancamento.vencimento < hoje()).order_by(Lancamento.vencimento)
    else:
        q = q.filter(Lancamento.pago_em.isnot(None))
        if de:
            q = q.filter(Lancamento.pago_em >= de)
        if ate:
            q = q.filter(Lancamento.pago_em <= ate)
        q = q.order_by(Lancamento.pago_em.desc(), Lancamento.id.desc())
    return q.limit(500).all()


def fluxo(de, ate):
    """Dia a dia: o que foi pago/recebido (pela data do pagamento) e o que está previsto (pelo vencimento).

    Contas vencidas e não pagas entram como previstas no dia de hoje (se hoje estiver no período)."""
    d = hoje()
    dias = {}

    def dia(x):
        return dias.setdefault(x, {"data": x, "entrou": Decimal(0), "saiu": Decimal(0), "a_receber": Decimal(0),
                                   "a_pagar": Decimal(0)})

    for l in Lancamento.query.filter(Lancamento.pago_em >= de, Lancamento.pago_em <= ate).all():
        dia(l.pago_em)["entrou" if l.tipo == "receber" else "saiu"] += Decimal(l.valor_pago or l.valor)
    for l in Lancamento.query.filter(Lancamento.pago_em.is_(None), Lancamento.vencimento <= ate).all():
        quando = max(l.vencimento, d)
        if de <= quando <= ate:
            dia(quando)["a_receber" if l.tipo == "receber" else "a_pagar"] += Decimal(l.valor)
    acumulado = Decimal(0)
    linhas = []
    for x in sorted(dias):
        linha = dias[x]
        linha["saldo"] = linha["entrou"] - linha["saiu"] + linha["a_receber"] - linha["a_pagar"]
        acumulado += linha["saldo"]
        linha["acumulado"] = acumulado
        linhas.append(linha)
    totais = {k: sum((l[k] for l in linhas), Decimal(0)) for k in ("entrou", "saiu", "a_receber", "a_pagar")}
    totais["saldo"] = acumulado
    return linhas, totais
