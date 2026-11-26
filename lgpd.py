"""LGPD: dados da empresa (controladora), versão dos termos, exportação e exclusão de conta pelo titular."""
from flask import current_app

from b2b import config, formatar_documento
from models import Order, db

# Mude a data quando alterar o texto da política ou dos termos: novos aceites registram a versão nova.
VERSAO_TERMOS = "2026-11-18"

CAMPOS_EMPRESA = {"empresa_razao_social": "Razão social", "empresa_cnpj": "CNPJ",
                  "empresa_endereco": "Endereço", "empresa_email_privacidade": "E-mail para assuntos de privacidade"}


def dados_empresa():
    d = {k: config(k, "") for k in CAMPOS_EMPRESA}
    d["empresa_cnpj"] = formatar_documento(d["empresa_cnpj"])
    d["completo"] = all(d[k] for k in ("empresa_razao_social", "empresa_cnpj", "empresa_email_privacidade"))
    return d


def operadores():
    """Serviços que tratam dados em nome da loja, conforme o que está configurado."""
    c = current_app.config
    banco = (c.get("SQLALCHEMY_DATABASE_URI") or "").lower()
    if "neon.tech" in banco:
        lista = [("Render", "hospedagem do site"), ("Neon", "hospedagem do banco de dados")]
    elif "render.com" in banco:
        lista = [("Render", "hospedagem do site e do banco de dados")]
    else:
        lista = [("Render", "hospedagem do site"), ("provedor do banco de dados", "hospedagem do banco de dados")]
    if c.get("BREVO_API_KEY"):
        lista.append(("Brevo", "envio de e-mails"))
    elif c.get("RESEND_API_KEY"):
        lista.append(("Resend", "envio de e-mails"))
    elif c.get("MAIL_SERVER"):
        lista.append(("provedor de e-mail (SMTP)", "envio de e-mails"))
    if c.get("TWILIO_VERIFY_SID"):
        lista.append(("Twilio", "envio do código de confirmação do celular por SMS"))
    if c.get("EFI_CLIENT_ID"):
        lista.append(("Efí (Efí Pay)", "processamento de pagamentos Pix, boleto e cartão"))
    if c.get("ANTHROPIC_API_KEY"):
        lista.append(("Anthropic", "atendente virtual com inteligência artificial: recebe as mensagens do chat e "
                                   "os dados de produtos e pedidos consultados para responder"))
    import analytics
    if analytics.medicao_id():
        lista.append(("Google (Google Analytics)", "estatísticas de visitas e vendas da loja, só para quem aceita os "
                                                   "cookies"))
    if c.get("MELHOR_ENVIO_TOKEN"):
        lista.append(("Melhor Envio", "cotação do frete e etiqueta de envio: recebe o CEP, e na etiqueta o nome, "
                                      "o CPF ou CNPJ, o endereço, o telefone e o e-mail do destinatário, além dos "
                                      "produtos enviados"))
    if c.get("CONSULTA_CNPJ", True):
        lista.append(("BrasilAPI", "consulta de dados públicos do CNPJ na Receita Federal"))
    return lista


def pedidos_abertos(user):
    return Order.query.filter(Order.user_id == user.id,
                              Order.status.in_(("pendente", "orcamento", "aprovacao"))).count()


def _carrinho(u):
    import carrinho_abandonado
    salvo = u.carrinho_salvo[0] if u.carrinho_salvo else None
    if not salvo:
        return []
    return [{"produto": p.name, "quantidade": q} for p, q in carrinho_abandonado.produtos_guardados(salvo)]


def _avaliacoes(u):
    return [{"produto": a.product.name, "nota": a.nota, "comentario": a.comentario, "nome_publicado": a.nome,
             "situacao": a.status, "data": a.criado_em.isoformat() if a.criado_em else None} for a in u.avaliacoes]


def exportar_membro(u):
    """Dados de um usuário da equipe de uma empresa (os pedidos são da empresa, ficam com o titular)."""
    pedidos = Order.query.filter_by(comprador_id=u.id).order_by(Order.id).all()
    return {"conta": {"nome": u.name, "email": u.email, "telefone": u.phone, "empresa": u.titular.razao_social
                      if u.titular else "", "papel": u.papel,
                      "termos_aceitos_em": u.termos_aceitos_em.isoformat() if u.termos_aceitos_em else None},
            "carrinho": _carrinho(u), "lembrete_do_carrinho": u.lembrete_carrinho, "avaliacoes": _avaliacoes(u),
            "pedidos_que_fez": [{"numero": o.id, "data": o.created_at.isoformat() if o.created_at else None,
                                 "status": o.status, "total": str(o.total)} for o in pedidos]}


def excluir_conta(user):
    """Apaga a conta. Os pedidos ficam (obrigação fiscal), só sem ligação com a conta.

    Devolve quantos pedidos em aberto impediram a exclusão (0 = excluída)."""
    abertos = pedidos_abertos(user)
    if abertos:
        return abertos
    Order.query.filter_by(user_id=user.id).update({"user_id": None}, synchronize_session=False)
    db.session.delete(user)
    db.session.commit()
    return 0


def exportar(user):
    """Tudo o que a loja guarda sobre o titular (direito de acesso e portabilidade, art. 18 da LGPD)."""
    def data(d):
        return d.isoformat() if d else None

    pedidos = Order.query.filter_by(user_id=user.id).order_by(Order.id).all()
    return {
        "conta": {
            "nome": user.name, "email": user.email, "email_confirmado": user.email_verified,
            "tipo": "empresa" if user.tipo == "pj" else "pessoa física", "documento": user.documento,
            "razao_social": user.razao_social, "inscricao_estadual": user.inscricao_estadual,
            "telefone": user.phone, "telefone_confirmado": user.telefone_verificado, "endereco": user.address,
            "empresa_aprovada": user.empresa_aprovada, "desconto_percentual": str(user.desconto or 0),
            "prazos_faturado": user.prazos, "dados_receita_federal": user.receita,
            "termos_aceitos_em": data(user.termos_aceitos_em), "versao_termos_aceita": user.termos_versao,
        },
        "carrinho": _carrinho(user),
        "avaliacoes": _avaliacoes(user),
        "lembrete_do_carrinho": user.lembrete_carrinho,
        "listas_de_compra": [{"nome": l.nome, "itens": [{"produto": i.product.name, "quantidade": i.qtd}
                                                         for i in l.itens]} for l in user.listas],
        "avise_me": [{"produto": a.product.name, "pedido_em": data(a.criado_em), "avisado_em": data(a.avisado_em)}
                     for a in user.avisos_estoque],
        "equipe": [{"nome": m.name, "email": m.email, "papel": m.papel} for m in user.membros],
        "enderecos": [{"nome": e.apelido, "cep": e.cep, "endereco": e.endereco, "principal": e.padrao}
                      for e in user.enderecos],
        "pedidos": [{
            "numero": o.id, "data": data(o.created_at), "status": o.status, "total": str(o.total),
            "pagamento": o.payment_method, "prazo": o.prazo, "nome": o.customer_name, "email": o.customer_email,
            "telefone": o.customer_phone, "endereco": o.address, "cep": o.cep, "documento": o.documento,
            "frete": "a combinar" if o.frete_combinar else str(o.frete or 0),
            "envio": o.envio or "aguardando", "transportadora": o.transportadora, "rastreio": o.rastreio,
            "cupom": o.cupom, "desconto": str(o.desconto or 0),
            "razao_social": o.razao_social, "pagador_nome": o.pagador_nome, "pagador_documento": o.pagador_documento,
            "itens": [{"produto": i.product.name, "quantidade": i.quantity, "preco_unitario": str(i.unit_price),
                       "fracionado": i.fracionado} for i in o.items],
        } for o in pedidos],
    }
