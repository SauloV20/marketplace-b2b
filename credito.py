"""Limite de crédito do boleto faturado: quanto a empresa pode ter em boletos a prazo ainda não pagos.

"Em aberto" = pedidos no faturado que estão aguardando pagamento (inclusive os já enviados). Quando a loja marca
como pago, o valor volta a ficar disponível. Sem limite cadastrado, o faturado não tem teto.
"""
from decimal import Decimal

from sqlalchemy import func

from formato import brl
from models import Order, db


def em_aberto(cliente, exceto=None):
    q = db.session.query(func.coalesce(func.sum(Order.total), 0)).filter(
        Order.user_id == cliente.id, Order.payment_method == "faturado", Order.status == "pendente")
    if exceto:
        q = q.filter(Order.id != exceto)
    return Decimal(q.scalar() or 0)


def disponivel(cliente, exceto=None):
    """Quanto ainda cabe no faturado (None = sem limite)."""
    if cliente is None or cliente.limite_credito is None:
        return None
    return max(Decimal(cliente.limite_credito) - em_aberto(cliente, exceto), Decimal(0))


def dias_de_atraso(cliente):
    """Há quantos dias vence a parcela mais antiga em aberto dos pedidos da empresa (0 = nada vencido)."""
    from b2b import hoje
    from models import Lancamento
    mais_antiga = (db.session.query(func.min(Lancamento.vencimento)).join(Order, Order.id == Lancamento.order_id)
                   .filter(Order.user_id == cliente.id, Lancamento.tipo == "receber", Lancamento.pago_em.is_(None),
                           Lancamento.vencimento < hoje()).scalar())
    return (hoje() - mais_antiga).days if mais_antiga else 0


def tolerancia_atraso():
    """Dias de atraso que bloqueiam o faturado (0 = não bloqueia). Configurado no Financeiro."""
    from b2b import config
    try:
        return max(int(config("bloqueio_atraso_dias", "0") or 0), 0)
    except ValueError:
        return 0


def bloqueado_por_atraso(cliente):
    dias = tolerancia_atraso()
    return bool(cliente is not None and dias and dias_de_atraso(cliente) > dias)


def cabe(cliente, valor, exceto=None):
    if bloqueado_por_atraso(cliente):
        return False
    livre = disponivel(cliente, exceto)
    return livre is None or Decimal(valor) <= livre


def mensagem(cliente, exceto=None):
    if bloqueado_por_atraso(cliente):
        return (f"Há um boleto vencido há {dias_de_atraso(cliente)} dias: o boleto faturado fica bloqueado até o "
                "pagamento. Escolha outra forma de pagamento ou fale com o Marketplace B2B.")
    livre = disponivel(cliente, exceto)
    return (f"O boleto faturado passa do limite de crédito da empresa ({brl(cliente.limite_credito)}): "
            f"disponível agora {brl(livre)}. Escolha outra forma de pagamento ou fale com o Marketplace B2B.")
