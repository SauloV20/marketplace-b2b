"""Cartão de crédito pela Efí, com link de pagamento (a tela de pagamento é da própria Efí).

O cliente fecha o pedido em "Cartão de crédito", o site cria na Efí um link de pagamento no valor total do pedido
(produtos, frete e cupom) e mostra o botão "Pagar com cartão". O cliente digita o cartão e escolhe as parcelas na
página da Efí: os dados do cartão nunca passam pelo site da loja (fora do escopo do PCI). Quando o pagamento é
confirmado, a Efí avisa o site no mesmo endereço do boleto (/efi/boleto) e o pedido passa para pago, com a baixa no
Financeiro e o e-mail ao cliente. Cartão recusado ou link vencido: o cliente gera outro link na página do pedido.

Usa as credenciais da API de Cobranças do boleto (EFI_CLIENT_ID, EFI_CLIENT_SECRET, EFI_SANDBOX). A Efí precisa
liberar o cartão na conta (formulário de análise de limite, até 5 dias úteis). Liga em Painel > Financeiro.
Docs: https://dev.efipay.com.br/docs/api-cobrancas/link-de-pagamento
"""
from datetime import timedelta
from decimal import Decimal

from flask import current_app

import boleto_efi
import financeiro
import historico
from b2b import config, salvar_config
from boleto_efi import ErroBoleto as ErroCartao
from formato import brl, hoje
from models import Order, db

PAGO = ("paid", "settled")
RECUSADO = ("unpaid", "canceled", "expired", "refunded", "contested")
DIAS_LINK = 3  # o link vale por 3 dias; depois o cliente gera outro na página do pedido
MINIMO = Decimal("5.00")
SITUACOES = {"link": "Link criado, aguardando o pagamento", "waiting": "Pagamento em análise na Efí",
             "approved": "Aprovado pela operadora, aguardando a confirmação", "paid": "Pago",
             "settled": "Pago", "unpaid": "Cartão recusado", "canceled": "Cobrança cancelada",
             "expired": "Link vencido", "refunded": "Pagamento devolvido", "contested": "Pagamento contestado"}


def ligado():
    return boleto_efi.ligado() and config("cartao_efi", "0") == "1"


def salvar_config_cartao(ativo):
    salvar_config("cartao_efi", "1" if ativo else "0")


def pode_gerar(o):
    """O pedido pode ganhar (outro) link de pagamento?"""
    return (ligado() and o.payment_method == "cartao" and o.status == "pendente"
            and (not o.cartao_id or o.cartao_situacao in RECUSADO))


def gerar_link(o, autor="sistema"):
    """Cria na Efí o link de pagamento do pedido (no valor total) e guarda no pedido. Devolve a mensagem."""
    if not ligado():
        raise ErroCartao("Cartão pela Efí desligado (Painel > Financeiro) ou sem as credenciais da Efí no Render.")
    if not pode_gerar(o):
        raise ErroCartao("Este pedido já tem um link de pagamento válido, ou não está aguardando pagamento.")
    if Decimal(o.total) < MINIMO:
        raise ErroCartao("A Efí só cobra no cartão a partir de R$ 5,00.")
    from email_utils import link_externo
    corpo = {"items": [{"name": f"Pedido {o.id} - Marketplace B2B", "value": int((Decimal(o.total) * 100).to_integral_value()),
                        "amount": 1}],
             "metadata": {"custom_id": f"pedido-{o.id}", "notification_url": link_externo("/efi/boleto")},
             "settings": {"payment_method": "credit_card", "expire_at": (hoje() + timedelta(days=DIAS_LINK)).isoformat(),
                          "request_delivery_address": False, "message": f"Pedido #{o.id} no Marketplace B2B"[:80]}}
    if o.customer_email and "@" in o.customer_email:
        corpo["customer"] = {"email": o.customer_email}
    dados = boleto_efi._chamar("POST", "/v1/charge/one-step/link", corpo).get("data") or {}
    if not dados.get("charge_id") or not str(dados.get("payment_url", "")).startswith("https://"):
        raise ErroCartao("A Efí não devolveu o link de pagamento. Tente de novo.")
    novo = bool(o.cartao_id)
    o.cartao_id, o.cartao_link = str(dados["charge_id"])[:30], str(dados["payment_url"])[:300]
    o.cartao_situacao = str(dados.get("status") or "link")[:15]
    db.session.commit()
    historico.registrar(o.id, ("Novo link" if novo else "Link") + " de pagamento com cartão criado na Efí", autor=autor)
    return "Link de pagamento com cartão criado."


def _aplicar(o, situacao, valor_centavos=None, quando=None):
    """Aplica no pedido a situação da cobrança vinda da Efí (só depois de consultar a própria API)."""
    if not situacao or situacao == o.cartao_situacao:
        return ""
    if o.cartao_situacao in PAGO and situacao not in ("refunded", "contested"):
        return ""  # aviso atrasado ou repetido: depois de pago, só devolução ou contestação mudam a situação
    o.cartao_situacao = situacao[:15]
    db.session.commit()
    if situacao in PAGO and o.status != "pendente":
        historico.registrar(o.id, f"Atenção: a Efí confirmou o pagamento com cartão, mas o pedido está {o.status}. "
                                  "Se não for seguir com ele, devolva o valor pela conta da Efí.", autor="sistema")
        return "pago na Efí, mas o pedido não está aguardando pagamento."
    if situacao in PAGO:
        abertas = [l for l in o.lancamentos if l.tipo == "receber" and not l.pago_em]
        valor = Decimal(valor_centavos) / 100 if valor_centavos else None
        texto = ""
        for l in abertas:  # à vista: uma parcela só; se houver mais, a primeira leva o valor informado
            texto = financeiro.recebido(l, quando or hoje(), valor if valor is not None and len(abertas) == 1 else
                                        Decimal(l.valor), "cartao")
        historico.registrar(o.id, "Pagamento com cartão confirmado pela Efí", autor="sistema")
        return texto or "pagamento confirmado."
    if situacao == "refunded":
        historico.registrar(o.id, "Cartão: pagamento devolvido na Efí", autor="sistema")
    elif situacao == "contested":
        historico.registrar(o.id, "Atenção: pagamento com cartão contestado pelo cliente no banco. Confira o pedido "
                                  "e responda a contestação pela conta da Efí.", autor="sistema")
    elif situacao in RECUSADO:
        historico.registrar(o.id, f"Cartão: {SITUACOES.get(situacao, situacao).lower()} (o cliente pode gerar outro "
                                  "link na página do pedido)", autor="sistema")
    return SITUACOES.get(situacao, situacao)


def notificacao_evento(ev):
    """Um evento da notificação da Efí (já consultada na API pelo boleto_efi.notificacao). True se era de cartão."""
    charge = str((ev.get("identifiers") or {}).get("charge_id", ""))
    o = Order.query.filter_by(cartao_id=charge).first() if charge else None
    if not o:
        return False
    _aplicar(o, (ev.get("status") or {}).get("current", ""), ev.get("value"),
             boleto_efi._data(ev.get("received_by_bank_at") or ev.get("created_at")))
    return True


def consultar(o):
    """Situação da cobrança na Efí (para quando o aviso não chegou). Devolve a mensagem."""
    if not o.cartao_id:
        raise ErroCartao("Este pedido não tem link de pagamento com cartão.")
    dados = boleto_efi._chamar("GET", f"/v1/charge/{o.cartao_id}").get("data") or {}
    situacao = str(dados.get("status") or "")
    pago = dados.get("paid_value") or (dados.get("payment") or {}).get("paid_value")
    _aplicar(o, situacao, pago, boleto_efi._data((dados.get("payment") or {}).get("paid_at") or dados.get("paid_at")))
    return f"Cartão na Efí: {SITUACOES.get(situacao, situacao or 'situação desconhecida')}."


def atualizar_abertos():
    """Consulta os pedidos no cartão ainda aguardando pagamento. Devolve (pagos, erros)."""
    pagos, erros = 0, []
    for o in Order.query.filter(Order.cartao_id.isnot(None), Order.cartao_id != "", Order.status == "pendente",
                                Order.cartao_situacao.notin_(RECUSADO + PAGO)).limit(200).all():
        try:
            consultar(o)
            if o.cartao_situacao in PAGO:
                pagos += 1
        except ErroCartao as e:
            erros.append(str(e))
            current_app.logger.warning("Consulta do cartão do pedido %s: %s", o.id, e)
            break
    return pagos, erros


def cancelar(o):
    """Pedido cancelado com link em aberto: cancela a cobrança na Efí (melhor esforço)."""
    if not o.cartao_id or o.cartao_situacao in PAGO + RECUSADO or not boleto_efi.ligado():
        return
    try:
        boleto_efi._chamar("PUT", f"/v1/charge/{o.cartao_id}/cancel")
        o.cartao_situacao = "canceled"  # quem cancelou o pedido grava junto
    except ErroCartao as e:
        current_app.logger.warning("Não foi possível cancelar o cartão %s na Efí: %s", o.cartao_id, e)


def estornar(o, valor):
    """Estorna `valor` (total ou parcial) do pagamento com cartão na Efí. Levanta ErroCartao se não der.

    Regras da Efí: cobrança paga, um estorno parcial por dia, parcial em até 90 dias e total em até 360 dias."""
    if not o.cartao_id or o.cartao_situacao not in PAGO:
        raise ErroCartao("este pedido não tem pagamento com cartão confirmado pela Efí.")
    if not boleto_efi.ligado():
        raise ErroCartao("sem as credenciais da Efí no Render.")
    centavos = int((Decimal(valor) * 100).to_integral_value())
    if centavos <= 0:
        raise ErroCartao("informe um valor maior que zero.")
    boleto_efi._chamar("POST", f"/v1/charge/card/{o.cartao_id}/refund", {"amount": centavos})
    historico.registrar(o.id, f"Estorno de {brl(valor)} pedido na Efí (cartão)", autor="loja")
