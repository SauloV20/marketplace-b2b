"""Devolução e troca: o cliente pede pela conta, a loja aprova (ou recusa), recebe os produtos e conclui.

Passos: solicitada -> aprovada (o cliente envia os produtos) -> recebida (o estoque pode voltar) -> concluída.
A loja pode recusar enquanto não concluiu. Na devolução, concluir registra o reembolso no Financeiro (conta a
pagar "Devolução a cliente", já paga) e, no cartão pela Efí, pode estornar direto na Efí. Na troca, a loja envia
os produtos novos combinando com o cliente; concluir só encerra o pedido de troca.

Prazo para pedir (Painel > Devoluções): conta da entrega (ou do envio, ou do pedido, se não houver).
A nota fiscal de devolução, quando precisar, a loja emite pelo emissor de NF-e.
"""
import json
from datetime import timedelta
from decimal import Decimal

from flask import url_for

import historico
from b2b import config, salvar_config
from email_utils import enviar_email, link_externo
from formato import brl, hoje
from models import Devolucao, Lancamento, Order, agora_utc, db, devolver_estoque

STATUS = {"solicitada": "Aguardando análise do Marketplace B2B", "aprovada": "Aprovada: envie os produtos",
          "recebida": "Produtos recebidos pelo Marketplace B2B", "concluida": "Concluída", "recusada": "Recusada"}
ABERTAS = ("solicitada", "aprovada", "recebida")
TIPOS = {"devolucao": "Devolução com reembolso", "troca": "Troca"}
PRAZO_PADRAO = 30
FORMAS_REEMBOLSO = {"cartao": "Estorno no cartão pela Efí", "pix": "Pix", "transferencia": "Transferência",
                    "dinheiro": "Dinheiro"}


class ErroDevolucao(Exception):
    pass


# ---------- configuração ----------
def prazo_dias():
    try:
        return max(int(config("devolucao_dias", str(PRAZO_PADRAO))), 0)
    except ValueError:
        return PRAZO_PADRAO


def salvar_prazo(texto):
    try:
        dias = int(str(texto).strip())
    except ValueError:
        dias = -1
    if not 0 <= dias <= 365:
        return "Informe de 0 a 365 dias (0 = o cliente não pede pelo site)."
    salvar_config("devolucao_dias", str(dias))
    return None


# ---------- regras ----------
def itens(d):
    try:
        return json.loads(d.itens or "[]")
    except ValueError:
        return []


def _inicio_do_prazo(o):
    return o.entregue_em or o.enviado_em or o.created_at


def prazo_ate(o):
    inicio = _inicio_do_prazo(o)
    return (inicio + timedelta(days=prazo_dias())) if inicio else None


def aberta(o):
    return next((d for d in o.devolucoes if d.status in ABERTAS), None)


def ja_devolvido(o):
    """{id do item: quantidade em devoluções ou trocas não recusadas}"""
    total = {}
    for d in o.devolucoes:
        if d.status == "recusada":
            continue
        for i in itens(d):
            total[i["item"]] = total.get(i["item"], 0) + int(i["qtd"])
    return total


def disponiveis(o):
    """[(item do pedido, quantidade que ainda dá para devolver)]"""
    usados = ja_devolvido(o)
    return [(i, i.quantity - usados.get(i.id, 0)) for i in o.items if i.quantity - usados.get(i.id, 0) > 0]


def motivo_bloqueio(o):
    """Por que o cliente não pode pedir devolução ou troca deste pedido agora (None = pode)."""
    if not prazo_dias():
        return "Para devolver ou trocar, fale com o Marketplace B2B."
    if not o.envio or o.status not in ("pago", "pendente"):
        return "Devolução e troca ficam disponíveis depois que o pedido é enviado."
    if aberta(o):
        return "Já existe um pedido de devolução ou troca em andamento para este pedido."
    if agora_utc() > prazo_ate(o):
        return f"O prazo para pedir pelo site ({prazo_dias()} dias) terminou. Fale com o Marketplace B2B."
    if not disponiveis(o):
        return "Todos os itens deste pedido já foram devolvidos ou trocados."
    return None


def sugestao_reembolso(d):
    """Valor dos itens devolvidos, com o desconto do cupom rateado (o frete não entra)."""
    o = d.order
    soma = sum((Decimal(str(i["preco"])) * int(i["qtd"]) for i in itens(d)), Decimal(0))
    subtotal = sum((i.unit_price * i.quantity for i in o.items), Decimal(0))
    if o.desconto and subtotal:
        soma -= (soma * Decimal(o.desconto) / subtotal)
    return max(soma, Decimal(0)).quantize(Decimal("0.01"))


def reembolsado(o):
    return sum((Decimal(d.valor or 0) for d in o.devolucoes if d.status == "concluida"), Decimal(0))


# ---------- cliente ----------
def solicitar(o, quem, tipo, motivo, form):
    """Cria o pedido de devolução ou troca com as quantidades do formulário (campos qtd_<id do item>)."""
    bloqueio = motivo_bloqueio(o)
    if bloqueio:
        raise ErroDevolucao(bloqueio)
    if tipo not in TIPOS:
        raise ErroDevolucao("Escolha devolução ou troca.")
    motivo = " ".join((motivo or "").split())[:1000]
    if len(motivo) < 5:
        raise ErroDevolucao("Conte o motivo (por exemplo: produto com defeito, veio errado, não serviu).")
    escolhidos = []
    for item, livre in disponiveis(o):
        try:
            qtd = int(form.get(f"qtd_{item.id}") or 0)
        except ValueError:
            qtd = 0
        if qtd < 0 or qtd > livre:
            raise ErroDevolucao(f"{item.product.name}: dá para devolver até {livre}.")
        if qtd:
            escolhidos.append({"item": item.id, "produto": item.product_id, "nome": item.product.name, "qtd": qtd,
                               "preco": str(item.unit_price), "frac": bool(item.fracionado)})
    if not escolhidos:
        raise ErroDevolucao("Informe a quantidade de pelo menos um produto.")
    d = Devolucao(order=o, pedido_por=quem.name[:120], tipo=tipo, motivo=motivo, itens=json.dumps(escolhidos))
    db.session.add(d)
    db.session.commit()
    historico.registrar(o.id, f"{TIPOS[tipo]} pedida pelo cliente: {_resumo(d)}", autor="cliente", nome=quem.name)
    import avisos
    avisos._admins(f"Marketplace B2B: {TIPOS[tipo].lower()} pedida no pedido #{o.id}",
                   f"{quem.name} pediu {TIPOS[tipo].lower()} no pedido #{o.id}.\n\nItens:\n{_linhas(d)}\n\n"
                   f"Motivo: {motivo}\n\nResponder: {link_externo(url_for('admin.devolucao', did=d.id))}")
    return d


def _linhas(d):
    return "\n".join(f"- {i['qtd']}x {i['nome']}" for i in itens(d))


def _resumo(d):
    return ", ".join(f"{i['qtd']}x {i['nome']}" for i in itens(d))[:300]


def _avisar_cliente(d, assunto, corpo):
    o = d.order
    enviar_email(o.customer_email, f"Marketplace B2B: {assunto} (pedido #{o.id})",
                 f"Olá, {o.customer_name}!\n\n{corpo}\n\nItens:\n{_linhas(d)}\n\n"
                 f"Acompanhe: {link_externo(url_for('account.pedido_detalhe', oid=o.id))}\n\nEquipe Marketplace B2B")


# ---------- loja ----------
def _mudar(d, status):
    d.status, d.atualizado_em = status, agora_utc()


def aprovar(d, instrucoes):
    if d.status != "solicitada":
        raise ErroDevolucao("Só dá para aprovar um pedido aguardando análise.")
    d.resposta = " ".join((instrucoes or "").split())[:500]
    _mudar(d, "aprovada")
    db.session.commit()
    historico.registrar(d.order_id, f"{TIPOS[d.tipo]} aprovada" + (f": {d.resposta}" if d.resposta else ""))
    _avisar_cliente(d, f"{TIPOS[d.tipo].lower()} aprovada",
                    f"Sua {TIPOS[d.tipo].lower()} foi aprovada." +
                    (f"\n\nComo enviar os produtos: {d.resposta}" if d.resposta else
                     " Em breve o Marketplace B2B entra em contato para combinar o envio dos produtos."))


def recusar(d, motivo):
    if d.status not in ABERTAS:
        raise ErroDevolucao("Este pedido de devolução já foi encerrado.")
    motivo = " ".join((motivo or "").split())[:500]
    if not motivo:
        raise ErroDevolucao("Escreva o motivo da recusa: ele vai para o cliente.")
    if d.estoque_devolvido:
        raise ErroDevolucao("Os produtos já voltaram ao estoque: conclua em vez de recusar.")
    d.resposta = motivo
    _mudar(d, "recusada")
    db.session.commit()
    historico.registrar(d.order_id, f"{TIPOS[d.tipo]} recusada: {motivo}")
    _avisar_cliente(d, f"{TIPOS[d.tipo].lower()} não aprovada",
                    f"Não foi possível aprovar sua {TIPOS[d.tipo].lower()}.\n\nMotivo: {motivo}")


def receber(d, voltar_ao_estoque):
    """Os produtos chegaram na loja. Com `voltar_ao_estoque`, eles voltam a ser vendáveis."""
    if d.status != "aprovada":
        raise ErroDevolucao("Aprove o pedido antes de marcar os produtos como recebidos.")
    if voltar_ao_estoque and not d.estoque_devolvido:
        for i in itens(d):
            devolver_estoque(i["produto"], int(i["qtd"]), int(i["qtd"]) if i.get("frac") else 0, "devolucao",
                             d.order_id)
        d.estoque_devolvido = True
    _mudar(d, "recebida")
    db.session.commit()
    historico.registrar(d.order_id, "Produtos da " + TIPOS[d.tipo].lower() + " recebidos" +
                        (" e devolvidos ao estoque" if d.estoque_devolvido else " (não voltaram ao estoque)"))


def concluir(d, valor_txt="", forma=""):
    """Encerra. Na devolução, registra o reembolso (e estorna no cartão pela Efí, se for a forma escolhida)."""
    if d.status not in ("aprovada", "recebida"):
        raise ErroDevolucao("Aprove o pedido antes de concluir.")
    o = d.order
    if d.tipo == "devolucao":
        from decimal import InvalidOperation
        from formato import ler_numero
        try:
            valor = ler_numero(valor_txt).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            valor = Decimal(-1)
        if valor < 0:
            raise ErroDevolucao("Informe o valor do reembolso (0 se não houver).")
        limite = Decimal(o.total) - reembolsado(o)
        if valor > limite:
            raise ErroDevolucao(f"O reembolso passa do que falta devolver deste pedido ({brl(limite)}).")
        if valor and forma not in FORMAS_REEMBOLSO:
            raise ErroDevolucao("Escolha como o valor foi devolvido.")
        if valor and forma == "cartao":
            import cartao_efi
            try:
                cartao_efi.estornar(o, valor)
            except cartao_efi.ErroCartao as e:
                raise ErroDevolucao(f"A Efí não fez o estorno: {e}") from e
        d.valor, d.reembolso_forma = valor, forma if valor else ""
        if valor:
            db.session.add(Lancamento(tipo="pagar", categoria="Devolução a cliente", valor=valor,
                                      descricao=f"Devolução do pedido #{o.id} - {o.customer_name}"[:160],
                                      vencimento=hoje(), pago_em=hoje(), valor_pago=valor,
                                      forma=forma,
                                      observacao=FORMAS_REEMBOLSO.get(forma, "")))
    _mudar(d, "concluida")
    db.session.commit()
    if d.tipo == "devolucao":
        texto = (f"reembolso de {brl(d.valor)} ({FORMAS_REEMBOLSO.get(d.reembolso_forma, '')})" if d.valor
                 else "sem reembolso")
        historico.registrar(o.id, f"Devolução concluída: {texto}")
        _avisar_cliente(d, "devolução concluída", "Sua devolução foi concluída. " + (
            f"Reembolso: {brl(d.valor)}, por {FORMAS_REEMBOLSO[d.reembolso_forma].lower()}." +
            (" No cartão, o valor aparece na fatura em até duas faturas, conforme o banco emissor."
             if d.reembolso_forma == "cartao" else "") if d.valor else ""))
    else:
        historico.registrar(o.id, "Troca concluída")
        _avisar_cliente(d, "troca concluída", "Sua troca foi concluída. Obrigado pela paciência!")


def lista(status=""):
    q = Devolucao.query.join(Order)
    if status == "abertas":
        q = q.filter(Devolucao.status.in_(ABERTAS))
    elif status in STATUS:
        q = q.filter(Devolucao.status == status)
    return q.order_by(Devolucao.id.desc()).limit(300).all()


def abertas_count():
    return Devolucao.query.filter(Devolucao.status.in_(ABERTAS)).count()
