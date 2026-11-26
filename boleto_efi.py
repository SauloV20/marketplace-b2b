"""Boleto registrado pela Efí (API de Cobranças), com baixa automática no Financeiro. O cartão de crédito
(cartao_efi.py) usa a mesma API, as mesmas credenciais e o mesmo endereço de avisos.

Cada parcela a receber de um pedido vira um boleto. Quando o cliente paga, a Efí avisa o site (notificação em
/efi/boleto, com a URL do SITE_URL) e a parcela é baixada sozinha; a última parcela paga quita o pedido. O botão
"Atualizar boletos" no Financeiro consulta os boletos em aberto (para quando o aviso não chegar).

Usa as mesmas credenciais do Pix (EFI_CLIENT_ID e EFI_CLIENT_SECRET) e EFI_SANDBOX=1 para o ambiente de testes.
A aplicação na Efí precisa ter a API de Cobranças (Emissões) liberada. A Efí cobra uma tarifa por boleto pago.
Docs: https://dev.efipay.com.br/docs/api-cobrancas/
"""
import base64
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal

from flask import current_app

import financeiro
from b2b import config, hoje, salvar_config, so_digitos
from models import Lancamento, agora_utc, db

PRODUCAO, HOMOLOGACAO = "https://cobrancas.api.efipay.com.br", "https://cobrancas-h.api.efipay.com.br"
PAGO = ("paid", "settled")
MINIMO = Decimal("5.00")  # a Efí não emite boleto abaixo de R$ 5,00
_token = {"valor": "", "expira": 0.0}


class ErroBoleto(Exception):
    pass


def ligado():
    c = current_app.config
    return bool(c.get("EFI_CLIENT_ID") and c.get("EFI_CLIENT_SECRET"))


def _base():
    return HOMOLOGACAO if current_app.config.get("EFI_SANDBOX") else PRODUCAO


def _http(metodo, caminho, corpo=None, autorizacao=""):
    req = urllib.request.Request(_base() + caminho, method=metodo,
                                 data=json.dumps(corpo).encode() if corpo is not None else None)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", autorizacao)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detalhe = e.read()[:300].decode("utf-8", "replace")
        raise ErroBoleto(f"A Efí recusou ({e.code}): {detalhe}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ErroBoleto(f"Efí indisponível agora ({e.__class__.__name__}). Tente de novo em alguns minutos.") from e


def _chamar(metodo, caminho, corpo=None):
    """Chamada autenticada na API de Cobranças (o token vale por uns minutos e é reaproveitado)."""
    if not ligado():
        raise ErroBoleto("Boleto pela Efí desligado: faltam EFI_CLIENT_ID e EFI_CLIENT_SECRET no Render.")
    if time.time() > _token["expira"] - 60:
        c = current_app.config
        cred = base64.b64encode(f"{c['EFI_CLIENT_ID']}:{c['EFI_CLIENT_SECRET']}".encode()).decode()
        r = _http("POST", "/v1/authorize", {"grant_type": "client_credentials"}, "Basic " + cred)
        _token.update(valor=r["access_token"], expira=time.time() + int(r.get("expires_in", 600)))
    return _http(metodo, caminho, corpo, "Bearer " + _token["valor"])


# ---------- configuração ----------
def multa_juros():
    try:
        return Decimal(config("boleto_multa", "2") or 0), Decimal(config("boleto_juros", "1") or 0)
    except Exception:
        return Decimal(2), Decimal(1)


def automatico():
    return config("boleto_auto", "0") == "1"


def salvar_config_boleto(multa, juros, auto):
    salvar_config("boleto_multa", multa)
    salvar_config("boleto_juros", juros)
    salvar_config("boleto_auto", "1" if auto else "0")


# ---------- emissão ----------
def _cliente(o):
    doc = so_digitos(o.documento)
    cliente = {}
    if len(doc) == 14:
        cliente["juridical_person"] = {"corporate_name": (o.razao_social or o.customer_name)[:255], "cnpj": doc}
    elif len(doc) == 11:
        cliente.update(name=o.customer_name[:255], cpf=doc)
    else:
        raise ErroBoleto("O pedido não tem CPF ou CNPJ válido do cliente (o boleto registrado exige).")
    if o.customer_email and "@" in o.customer_email:
        cliente["email"] = o.customer_email
    fone = so_digitos(o.customer_phone)
    if len(fone) in (10, 11):
        cliente["phone_number"] = fone
    return cliente


def gerar(l):
    """Emite o boleto da parcela `l` (conta a receber de um pedido). Devolve a mensagem."""
    if not ligado():
        raise ErroBoleto("Boleto pela Efí desligado: faltam EFI_CLIENT_ID e EFI_CLIENT_SECRET no Render.")
    o = l.order
    if l.tipo != "receber" or not o:
        raise ErroBoleto("Boleto só para parcela a receber de um pedido.")
    if l.pago_em:
        raise ErroBoleto("Esta parcela já foi recebida.")
    if l.boleto_id:
        raise ErroBoleto("Esta parcela já tem boleto.")
    if Decimal(l.valor) < MINIMO:
        raise ErroBoleto("A Efí só emite boleto a partir de R$ 5,00.")
    vencimento = max(l.vencimento, hoje())
    multa, juros = multa_juros()
    from email_utils import link_externo
    corpo = {"items": [{"name": f"Pedido {o.id}" + (f" - parcela {l.parcela}" if l.parcela else ""), "amount": 1,
                        "value": int((Decimal(l.valor) * 100).to_integral_value())}],
             "metadata": {"custom_id": f"lanc-{l.id}", "notification_url": link_externo("/efi/boleto")},
             "payment": {"banking_billet": {
                 "expire_at": vencimento.isoformat(), "customer": _cliente(o),
                 "message": f"Pedido {o.id} - Marketplace B2B"[:80],
                 "configurations": {"fine": int(multa * 100), "interest": int(juros * 100 / 30)}}}}
    dados = _chamar("POST", "/v1/charge/one-step", corpo).get("data") or {}
    if not dados.get("charge_id"):
        raise ErroBoleto("A Efí não devolveu o boleto. Tente de novo.")
    l.boleto_id = str(dados["charge_id"])
    l.boleto_link = (dados.get("billet_link") or dados.get("link") or (dados.get("pdf") or {}).get("charge") or "")[:300]
    l.boleto_linha = (dados.get("barcode") or "")[:60]
    l.forma = "boleto" if o.payment_method != "faturado" else "faturado"
    if vencimento != l.vencimento:
        l.vencimento = vencimento
    db.session.commit()
    return f"Boleto gerado ({l.parcela or 'parcela única'}, vence {vencimento:%d/%m/%Y})."


def gerar_do_pedido(o, avisar=True):
    """Boletos de todas as parcelas em aberto do pedido que ainda não têm. Devolve (quantos, erros)."""
    feitos, erros = 0, []
    for l in [x for x in o.lancamentos if x.tipo == "receber" and not x.pago_em and not x.boleto_id]:
        try:
            gerar(l)
            feitos += 1
        except ErroBoleto as e:
            erros.append(str(e))
            db.session.rollback()
            break
    if feitos and avisar:
        enviar_ao_cliente(o)
    return feitos, erros


def enviar_ao_cliente(o):
    from avisos import _link_cliente
    from email_utils import enviar_email
    linhas = [f"- {('Parcela ' + l.parcela) if l.parcela else 'Boleto'}: R$ {l.valor:.2f}, vence em "
              f"{l.vencimento:%d/%m/%Y}\n  {l.boleto_link}" + (f"\n  Linha digitável: {l.boleto_linha}" if l.boleto_linha else "")
              for l in o.lancamentos if l.boleto_id and not l.pago_em]
    if not linhas or not o.customer_email:
        return False
    texto = (f"Olá, {o.customer_name}!\n\nSeguem os boletos do pedido #{o.id}:\n\n" + "\n".join(linhas)
             + f"\n\nQuando o pagamento compensar, a baixa é automática.\nSeus pedidos: {_link_cliente(o)}\n\nEquipe Marketplace B2B")
    return enviar_email(o.customer_email, f"Marketplace B2B: boleto(s) do pedido #{o.id}", texto)


# ---------- pagamento ----------
def _data(texto):
    try:
        return datetime.fromisoformat(str(texto)[:19]).date()
    except (TypeError, ValueError):
        return hoje()


def _receber(l, valor_centavos=None, quando=None):
    if l.pago_em:
        return ""
    valor = Decimal(valor_centavos) / 100 if valor_centavos else Decimal(l.valor)
    return financeiro.recebido(l, quando or hoje(), valor, "boleto")


def consultar(l):
    """Situação do boleto na Efí. Se pago, dá baixa. Devolve a mensagem."""
    if not l.boleto_id:
        raise ErroBoleto("Esta parcela não tem boleto.")
    dados = _chamar("GET", f"/v1/charge/{l.boleto_id}").get("data") or {}
    status = dados.get("status", "")
    if status in PAGO:
        pago = dados.get("paid_value") or (dados.get("payment") or {}).get("paid_value") or dados.get("total")
        return "Boleto pago: " + (_receber(l, pago, _data(dados.get("paid_at") or (dados.get("payment") or {}).get("paid_at"))) or "baixa feita.")
    return {"waiting": "Aguardando pagamento.", "unpaid": "Vencido e não pago.", "canceled": "Boleto cancelado.",
            "expired": "Boleto expirado."}.get(status, f"Situação na Efí: {status or 'desconhecida'}.")


def atualizar_abertos():
    """Consulta todos os boletos em aberto (para quando o aviso da Efí não chegou). Devolve (pagos, erros)."""
    pagos, erros = 0, []
    for l in Lancamento.query.filter(Lancamento.boleto_id.isnot(None), Lancamento.boleto_id != "",
                                     Lancamento.pago_em.is_(None)).limit(200).all():
        try:
            if consultar(l).startswith("Boleto pago"):
                pagos += 1
        except ErroBoleto as e:
            erros.append(str(e))
            break
    salvar_config("boleto_ultima_consulta", agora_utc().isoformat(timespec="seconds"))
    db.session.commit()
    return pagos, erros


def notificacao(token):
    """A Efí avisa que algo mudou: o token é trocado pelo histórico na própria API (não confiamos no aviso em si).
    Devolve quantas parcelas foram baixadas."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", token or ""):  # vai no caminho da URL da Efí: nada de / ? .. %
        return 0
    baixadas = 0
    eventos = [ev for ev in (_chamar("GET", f"/v1/notification/{token}").get("data") or []) if isinstance(ev, dict)]
    import cartao_efi
    ultimo = {}  # a notificação traz o histórico inteiro: para o cartão vale a situação mais recente de cada cobrança
    for ev in sorted(eventos, key=lambda e: e.get("id") or 0):
        ultimo[str((ev.get("identifiers") or {}).get("charge_id", ""))] = ev
    cartoes = {c for c, ev in ultimo.items() if c and cartao_efi.notificacao_evento(ev)}
    for ev in eventos:
        atual = (ev.get("status") or {}).get("current", "")
        charge = str((ev.get("identifiers") or {}).get("charge_id", ""))
        if charge in cartoes:
            continue
        if atual not in PAGO or not charge:
            continue
        l = Lancamento.query.filter_by(boleto_id=charge).first()
        if l and not l.pago_em:
            _receber(l, ev.get("value"), _data(ev.get("received_by_bank_at") or ev.get("created_at")))
            baixadas += 1
    return baixadas


def cancelar(l):
    """Cancela na Efí o boleto de uma parcela que deixou de existir (pedido cancelado). Melhor esforço."""
    if not l.boleto_id or l.pago_em or not ligado():
        return
    try:
        _chamar("PUT", f"/v1/charge/{l.boleto_id}/cancel")
    except ErroBoleto as e:
        current_app.logger.warning("Não foi possível cancelar o boleto %s na Efí: %s", l.boleto_id, e)

