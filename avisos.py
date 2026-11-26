"""Avisos por e-mail: para a loja (todas as contas de administrador) e para o cliente.

Um aviso que falha não atrapalha a compra: enviar_email só registra o erro no log."""
from flask import url_for

from b2b import METODOS, formatar_documento
from formato import brl, num
from email_utils import enviar_email, link_externo
from models import User, db

STATUS = {"orcamento": "orçamento em análise", "aprovacao": "aguardando aprovação da empresa",
          "pendente": "aguardando pagamento", "pago": "pago", "cancelado": "cancelado"}


def _itens(o):
    linhas = [f"- {i.quantity}x {i.product.name}{' (fracionado)' if i.fracionado else ''}: "
              f"{brl(i.unit_price)} cada = {brl(i.unit_price * i.quantity)}" for i in o.items]
    if o.desconto:
        linhas.append(f"Cupom {o.cupom}: -{brl(o.desconto)}")
    if o.frete_combinar:
        linhas.append("Frete: a combinar")
    elif o.cep:
        rotulo = " · ".join(p for p in (o.frete_servico or "", o.frete_prazo or "") if p)
        linhas.append(f"Frete: {'grátis' if not o.frete else brl(o.frete)}" + (f" ({rotulo})" if rotulo else ""))
    return "\n".join(linhas) + f"\nTotal: {brl(o.total)}"


def _pagamento(o):
    return METODOS.get(o.payment_method, o.payment_method or "-") + (f" em {o.prazo} dias" if o.prazo else "")


def _quem(o):
    if o.razao_social:
        return f"{o.razao_social} (CNPJ {formatar_documento(o.documento)}), responsável {o.customer_name}"
    return o.customer_name + (f" (CPF {formatar_documento(o.documento)})" if o.documento else "")


def _admins(assunto, texto, area="pedidos"):
    """E-mail para os administradores que cuidam da área (acesso total recebe tudo)."""
    from permissoes import pode
    for email in [u.email for u in User.query.filter_by(is_admin=True).all() if pode(u, area)]:
        enviar_email(email, assunto, texto)


def _link_admin(endpoint, **kw):
    return link_externo(url_for(endpoint, **kw))


def _link_cliente(o):
    return link_externo(url_for("account.pedido_detalhe", oid=o.id))


# ---------- pedidos ----------
def pedido_recebido(o):
    orcamento = o.status == "orcamento"
    tipo = "orçamento" if orcamento else "pedido"
    if orcamento:
        corpo = ("Recebemos sua solicitação de orçamento. Vamos conferir preços, prazo e frete e responder por aqui. "
                 "O estoque é reservado quando o orçamento for aprovado.")
    else:
        corpo = f"Recebemos seu pedido. Forma de pagamento: {_pagamento(o)}."
    pagar = ""
    if o.status == "pendente" and o.payment_method in ("pix", "boleto", "cartao"):
        pagar = f"Veja como pagar: {link_externo(url_for('shop.pedido', oid=o.id))}\n\n"
    enviar_email(o.customer_email, f"Marketplace B2B: {tipo} #{o.id} recebido",
                 f"Olá, {o.customer_name}!\n\n{corpo}\n\n{_itens(o)}\n\n{pagar}Acompanhe em: {_link_cliente(o)}"
                 "\n\nEquipe Marketplace B2B")
    _admins(f"Novo {tipo} #{o.id}: {brl(o.total)}",
            f"Novo {tipo} na loja.\n\nCliente: {_quem(o)}\nContato: {o.customer_email} | {o.customer_phone}\n"
            f"Entrega: {o.address}\nPagamento: {_pagamento(o)}\n\n{_itens(o)}\n\n"
            + (f"Revisar orçamento: {_link_admin('admin.orcamento', oid=o.id)}" if orcamento
               else f"Ver pedidos: {_link_admin('admin.orders')}"))


def pagamento_confirmado(o):
    enviar_email(o.customer_email, f"Marketplace B2B: pagamento do pedido #{o.id} confirmado",
                 f"Olá, {o.customer_name}!\n\nConfirmamos o pagamento do pedido #{o.id} ({brl(o.total)}). "
                 f"Agora vamos separar e combinar a entrega.\n\nAcompanhe em: {_link_cliente(o)}\n\nEquipe Marketplace B2B")
    if o.pagador_confere is False:
        _admins(f"Atenção: pedido #{o.id} pago por outra pessoa",
                f"O Pix do pedido #{o.id} ({brl(o.total)}) foi pago, mas o pagador não confere com o cadastro.\n\n"
                f"Cadastro: {_quem(o)}\nPagador informado pela Efí: {o.pagador_nome or '-'} "
                f"({o.pagador_documento or 'sem documento'})\n\nConfira antes de enviar: {_link_admin('admin.orders')}")
    else:
        _admins(f"Pedido #{o.id} pago: {brl(o.total)}",
                f"O pedido #{o.id} de {_quem(o)} foi pago ({_pagamento(o)}).\n\nVer pedidos: {_link_admin('admin.orders')}")


def pedido_cancelado(o, pela_loja):
    if pela_loja:
        enviar_email(o.customer_email, f"Marketplace B2B: pedido #{o.id} cancelado",
                     f"Olá, {o.customer_name}!\n\nO pedido #{o.id} foi cancelado. Se tiver dúvidas, fale com o Marketplace B2B "
                     f"pelo WhatsApp ou pelo site.\n\nDetalhes: {_link_cliente(o)}\n\nEquipe Marketplace B2B")
    else:
        _admins(f"Cliente cancelou o pedido #{o.id}",
                f"{_quem(o)} cancelou o pedido #{o.id} ({brl(o.total)}).\n\nVer pedidos: {_link_admin('admin.orders')}")


def orcamento_respondido(o, aprovado, espera_empresa=False):
    if aprovado and espera_empresa:
        texto = (f"O Marketplace B2B aprovou o seu orçamento #{o.id}. Como o valor passa do seu limite de compra, ele agora "
                 f"precisa da aprovação da sua empresa (titular ou aprovador), que já foi avisada por e-mail. "
                 f"Depois disso, ele vira pedido.\n\n{_itens(o)}\n\nPagamento: {_pagamento(o)}\n\n"
                 f"Acompanhe em: {_link_cliente(o)}")
    elif aprovado:
        texto = (f"Seu orçamento #{o.id} foi aprovado e virou pedido.\n\n{_itens(o)}\n\nPagamento: {_pagamento(o)}\n\n"
                 f"Veja como pagar em: {link_externo(url_for('shop.pedido', oid=o.id))}")
    else:
        texto = (f"Não conseguimos atender o orçamento #{o.id} desta vez. Se quiser, fale com o Marketplace B2B para "
                 f"ajustarmos quantidades ou prazos.\n\nDetalhes: {_link_cliente(o)}")
    enviar_email(o.customer_email, f"Marketplace B2B: orçamento #{o.id} {'aprovado' if aprovado else 'não aprovado'}",
                 f"Olá, {o.customer_name}!\n\n{texto}\n\nEquipe Marketplace B2B")


def pedido_alterado(o):
    enviar_email(o.customer_email, f"Marketplace B2B: pedido #{o.id} atualizado",
                 f"Olá, {o.customer_name}!\n\nAjustamos o seu pedido #{o.id}, como combinado. Confira como ficou:\n\n"
                 f"{_itens(o)}\n\nPagamento: {_pagamento(o)}\n\nDetalhes: {_link_cliente(o)}\n\nEquipe Marketplace B2B")


def pedido_alterado_pelo_cliente(o, mudancas):
    tipo = "orçamento" if o.status == "orcamento" else "pedido"
    _admins(f"Cliente alterou o {tipo} #{o.id}",
            f"{_quem(o)} alterou o {tipo} #{o.id}:\n- " + "\n- ".join(mudancas) + f"\n\nComo ficou:\n{_itens(o)}\n\n"
            + (f"Revisar orçamento: {_link_admin('admin.orcamento', oid=o.id)}" if o.status == "orcamento"
               else f"Ver pedido: {_link_admin('admin.pedido', oid=o.id)}"))


# ---------- aprovação dentro da empresa cliente ----------
def pedido_para_aprovar(o, aprovadores):
    for u in aprovadores:
        enviar_email(u.email, f"Marketplace B2B: pedido #{o.id} aguardando sua aprovação",
                     f"Olá, {u.name}!\n\n{o.comprador_nome or o.customer_name} fez o pedido #{o.id} pela conta da "
                     f"{o.razao_social or 'empresa'} e ele precisa da sua aprovação para seguir para o Marketplace B2B.\n\n"
                     f"{_itens(o)}\n\nPagamento: {_pagamento(o)}\n\nAprovar ou recusar: {_link_cliente(o)}\n\nEquipe Marketplace B2B")


def pedido_recusado_pela_empresa(o, quem):
    enviar_email(o.customer_email, f"Marketplace B2B: pedido #{o.id} não aprovado",
                 f"Olá, {o.customer_name}!\n\n{quem.name} não aprovou o pedido #{o.id}. Se precisar, ajuste o "
                 f"carrinho e envie de novo, ou fale com o responsável da sua empresa.\n\nDetalhes: {_link_cliente(o)}"
                 f"\n\nEquipe Marketplace B2B")


# ---------- entrega e documentos ----------
def pedido_enviado(o):
    rastreio = ""
    if o.rastreio:
        rastreio += f"\nCódigo de rastreio: {o.rastreio}"
    if o.rastreio_link:
        rastreio += f"\nAcompanhe a entrega: {o.rastreio_link}"
    enviar_email(o.customer_email, f"Marketplace B2B: pedido #{o.id} enviado",
                 f"Olá, {o.customer_name}!\n\nSeu pedido #{o.id} foi enviado por {o.transportadora}.{rastreio}\n\n"
                 f"Entrega em: {o.address}\n\nDetalhes e documentos: {_link_cliente(o)}\n\nEquipe Marketplace B2B")


def pedido_entregue(o):
    enviar_email(o.customer_email, f"Marketplace B2B: pedido #{o.id} entregue",
                 f"Olá, {o.customer_name}!\n\nO pedido #{o.id} consta como entregue. Se algo chegou faltando ou com "
                 f"problema, peça a devolução ou troca na página do pedido ou fale com o Marketplace B2B pelo WhatsApp.\n\n"
                 f"Gostou? Conte o que achou: na página do pedido, clique em \"Avaliar\" ao lado de cada produto. "
                 f"Sua avaliação ajuda outros clientes a escolher."
                 f"\n\nDetalhes: {_link_cliente(o)}\n\nEquipe Marketplace B2B")


def documento_disponivel(o, arquivo):
    from entrega import TIPOS
    enviar_email(o.customer_email, f"Marketplace B2B: {TIPOS[arquivo.tipo].lower()} do pedido #{o.id}",
                 f"Olá, {o.customer_name}!\n\nJá está disponível: {TIPOS[arquivo.tipo].lower()} do pedido #{o.id} "
                 f"({arquivo.nome}). Para baixar, entre na sua conta do Marketplace B2B.\n\nBaixar: {_link_cliente(o)}\n\nEquipe Marketplace B2B")


# ---------- estoque ----------
def estoque_baixo(vendidos):
    """Avisa a loja quando uma venda faz o estoque chegar ao nível de alerta do produto.

    vendidos = [(produto, quantidade que acabou de sair)]. Só avisa na venda que cruza o nível (não a cada venda
    depois disso); a lista completa fica no painel, em Vendas."""
    baixos = []
    for p, qtd in vendidos:
        db.session.refresh(p)
        if p.estoque_alerta and p.stock <= p.estoque_alerta < p.stock + qtd:
            baixos.append(p)
    if not baixos:
        return
    linhas = "\n".join(f"- {p.name}{f' (cód. {p.codigo})' if p.codigo else ''}: restam {p.stock} un "
                       f"(alerta em {p.estoque_alerta})" for p in baixos)
    _admins("Estoque baixo: " + (baixos[0].name if len(baixos) == 1 else f"{len(baixos)} produtos"),
            f"Estes produtos chegaram ao nível de alerta de estoque:\n\n{linhas}\n\n"
            f"Ver no painel: {_link_admin('admin.products')}", area="produtos")


# ---------- empresas ----------
def nova_empresa(u):
    _admins(f"Nova empresa para aprovar: {u.razao_social}",
            f"{u.razao_social} (CNPJ {formatar_documento(u.documento)}) se cadastrou e confirmou o e-mail.\n"
            f"Responsável: {u.name} | {u.email}\nReceita Federal: {u.receita_situacao or 'não verificado'}\n\n"
            f"Analisar: {_link_admin('admin.cliente', uid=u.id)}", area="clientes")


def empresa_aprovada(u):
    condicoes = []
    if u.desconto:
        condicoes.append(f"- {num(u.desconto)}% de desconto sobre a tabela")
    if u.prazos:
        condicoes.append(f"- Boleto faturado em {u.prazos} dias")
    enviar_email(u.email, "Marketplace B2B: cadastro da empresa aprovado",
                 f"Olá, {u.name}!\n\nO cadastro da {u.razao_social} foi aprovado."
                 + ("\n\nCondições liberadas:\n" + "\n".join(condicoes) if condicoes else "")
                 + f"\n\nAcesse a loja: {link_externo(url_for('shop.index'))}\n\nEquipe Marketplace B2B")
