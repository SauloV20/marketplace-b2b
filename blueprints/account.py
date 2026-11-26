import hmac
from decimal import Decimal
from functools import wraps

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session,
                   url_for)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash

import avisos
import editar_pedido
import credito
import entrega
import equipe_empresa
import historico
import avise_me
import lgpd
import listas
import minha_conta
from produtos import link as link_produto
from b2b import (ajustar_qtd, disponivel, ler_documento, pedido_minimo, prazos_do_cliente, preco_unitario,
                 tem_condicoes_de_empresa)
from blueprints.shop import METODOS
from models import Endereco, ListaCompra, Order, Product, User, agora_utc, db, mudar_status
from email_utils import enviar_email, link_externo
from receita import ENCONTRADO, NAO_ENCONTRADO, consultar_cnpj
from security import (HASH_FALSO, acertou, bloqueado, entrar_sessao, falhou, marca_senha, pode_cadastrar,
                      pode_consultar_cnpj, pode_enviar_email, pode_enviar_sms, renovar_sessao, sair_da_loja)
from verificacao import (APROVADO, ERRADO, EXPIRADO, celular_pendente, conferir_codigo, enviar_codigo,
                         formatar_celular, ligado as sms_ligado, normalizar_celular)

account = Blueprint("account", __name__)
STATUS = {"orcamento": "Orçamento em análise", "aprovacao": "Aguardando aprovação da empresa",
          "pendente": "Aguardando pagamento", "pago": "Pago", "cancelado": "Cancelado"}
VALIDADE_CONFIRMACAO = 48 * 3600  # link de confirmação vale 48 horas
VALIDADE_SENHA = 3600  # link de nova senha vale 1 hora
MSG_ENVIO = "Se houver uma conta com esse e-mail, enviamos as instruções. Confira também o spam."


def _assinador(finalidade):
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=finalidade)


def enviar_confirmacao(user):
    token = _assinador("confirmar-email").dumps({"id": user.id, "e": user.email})
    link = link_externo(url_for("account.confirmar", token=token))
    return enviar_email(user.email, "Confirme seu e-mail no Marketplace B2B",
                        f"Olá, {user.name}!\n\nPara ativar sua conta no Marketplace B2B, confirme o seu e-mail (o link vale por 48 horas).\n\n"
                        f"Confirmar e-mail: {link}\n\nSe você não criou esta conta, ignore este e-mail.\n\nEquipe Marketplace B2B")


def enviar_nova_senha(user):
    # depois que a senha muda, a marca muda e o link deixa de valer
    # o token leva uma marca (HMAC) da senha atual, nunca um pedaço do hash: o link fica legível no e-mail
    token = _assinador("nova-senha").dumps({"id": user.id, "h": marca_senha(user, "nova-senha")})
    link = link_externo(url_for("account.nova_senha", token=token))
    return enviar_email(user.email, "Redefinir sua senha no Marketplace B2B",
                        f"Olá, {user.name}!\n\nPara criar uma nova senha, use o link abaixo (vale por 1 hora).\n\n"
                        f"Criar nova senha: {link}\n\n"
                        "Se você não pediu isso, ignore este e-mail: sua senha continua a mesma.\n\nEquipe Marketplace B2B")


def ler_token(finalidade, token, validade):
    try:
        return _assinador(finalidade).loads(token, max_age=validade)
    except (SignatureExpired, BadSignature):
        return None


@account.context_processor
def contexto():
    return {"status": STATUS, "membro": g.get("membro"), "nome_papel": equipe_empresa.nome_papel}


def entrar_na_conta(user):
    carrinho = session.get("cart")  # quem acabou de entrar não perde o carrinho
    session.clear()
    entrar_sessao("cliente_id", user)
    if carrinho:
        session["cart"] = carrinho


LIVRES = {"account.celular", "account.sair", "account.confirmar", "account.confirme", "account.entrar",
          "account.reenviar", "account.esqueci", "account.nova_senha", "account.parar_lembrete", "static", "shop.privacidade", "shop.termos",
          "account.excluir_conta", "account.baixar_dados"}


@account.before_app_request
def exigir_celular():
    """Com a confirmação por SMS ligada, quem entrou na conta só usa a loja depois de confirmar o celular."""
    if request.endpoint in LIVRES or (request.endpoint or "").startswith("admin.") or not session.get("cliente_id"):
        return None
    if not celular_pendente(db.session.get(User, session["cliente_id"])):
        return None
    if request.endpoint == "shop.cart_add":
        return jsonify(ok=False, login=url_for("account.celular"), msg="Confirme seu celular para comprar."), 403
    return redirect(url_for("account.celular"))


@account.route("/conta/celular", methods=["GET", "POST"])
def celular():
    user = db.session.get(User, session.get("cliente_id") or 0)
    if not user or user.is_admin:
        return redirect(url_for("account.entrar"))
    if session.get("membro_id"):  # o celular confirmado é o do titular da conta da empresa
        flash("O celular da conta é cuidado pelo titular da empresa.")
        return redirect(url_for("account.conta"))
    if not sms_ligado():
        return redirect(url_for("account.dados"))
    pendente = session.get("celular_novo")
    if request.method == "POST":
        f = request.form
        if f.get("acao") == "enviar":
            numero = normalizar_celular(f.get("celular", ""))
            if not numero:
                flash("Informe um celular com DDD, como (11) 98765-4321.")
            elif not pode_enviar_sms(user.id):
                flash("Aguarde um minuto antes de pedir outro código (máximo de 5 por hora).")
            elif enviar_codigo(numero):
                session["celular_novo"] = numero
                flash(f"Enviamos um código por SMS para {formatar_celular(numero)}. Ele vale por 10 minutos.")
            else:
                flash("Não conseguimos enviar o SMS agora. Confira o número e tente de novo em instantes.")
        elif f.get("acao") == "conferir" and pendente:
            codigo = "".join(ch for ch in f.get("codigo", "") if ch.isdigit())[:10]
            resultado = conferir_codigo(pendente, codigo) if codigo else ERRADO
            if resultado == APROVADO:
                user.phone, user.telefone_verificado = pendente, True
                db.session.commit()
                session.pop("celular_novo", None)
                flash("Celular confirmado. Obrigado!")
                return redirect(url_for("account.conta"))
            if resultado == EXPIRADO:
                session.pop("celular_novo", None)
                flash("O código venceu ou teve tentativas demais. Peça um novo.")
            else:
                flash("Código incorreto. Confira o SMS e tente de novo." if resultado == ERRADO
                      else "Não conseguimos conferir o código agora. Tente de novo em instantes.")
        return redirect(url_for("account.celular"))
    return render_template("shop/celular.html", cliente=user, pendente=pendente, fmt_cel=formatar_celular,
                           obrigatorio=not user.telefone_verificado)


def cliente_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        cliente = db.session.get(User, session.get("cliente_id") or 0)
        if not cliente or cliente.is_admin:
            return redirect(url_for("account.entrar"))
        g.cliente, g.membro = cliente, None  # g.cliente = a conta (empresa); g.membro = quem da equipe entrou
        if session.get("membro_id"):
            membro = db.session.get(User, session["membro_id"])
            if not membro or membro.empresa_id != cliente.id:  # saiu da equipe: a sessão acaba
                session.clear()
                flash("Seu acesso à conta da empresa foi encerrado. Fale com o responsável.")
                return redirect(url_for("account.entrar"))
            g.membro = membro
        return view(*args, **kwargs)
    return wrapper


def checar_na_receita(cnpj):
    """Devolve (dados da Receita ou None se a consulta não deu certo, erro que impede o cadastro)."""
    status, dados = consultar_cnpj(cnpj)
    if status == NAO_ENCONTRADO:
        return None, "Não encontramos este CNPJ na Receita Federal. Confira os números."
    if status == ENCONTRADO and dados["situacao"] != "ATIVA":
        return None, (f"Este CNPJ está com situação {dados['situacao'] or 'irregular'} na Receita Federal. "
                      "Só aceitamos empresas ativas; fale com o Marketplace B2B se achar que é um engano.")
    return dados, None  # dados None = API fora do ar: segue e fica "não verificado"


def ler_empresa(f, tipo):
    """Documento, razão social e IE do formulário (CNPJ conferido na Receita). Devolve (dados, erro)."""
    if tipo not in ("pf", "pj"):
        return None, "Escolha se a conta é de pessoa física (CPF) ou de empresa (CNPJ)."
    doc, erro = ler_documento(tipo, f.get("documento", ""))
    if erro:
        return None, erro
    if User.query.filter_by(documento=doc).first():
        return None, ("Este CNPJ" if tipo == "pj" else "Este CPF") + " já tem conta. Tente entrar ou recuperar a senha."
    if tipo == "pf":
        return dict(tipo=tipo, documento=doc, razao_social="", inscricao_estadual=""), None
    receita, erro = checar_na_receita(doc)
    if erro:
        return None, erro
    # com a Receita respondendo, a razão social oficial vale mais que a digitada
    razao = (receita or {}).get("razao_social") or " ".join(f.get("razao_social", "").split())[:160]
    if not razao:
        return None, "Informe a razão social da empresa."
    ie = f.get("inscricao_estadual", "").strip()[:30]
    return dict(tipo=tipo, documento=doc, razao_social=razao, inscricao_estadual=ie, receita=receita), None


def aplicar_empresa(user, empresa):
    receita = empresa.pop("receita", None)
    for campo, valor in empresa.items():
        setattr(user, campo, valor)
    if receita:
        user.guardar_receita(receita)
        user.address = user.address or receita["endereco"]


def meu_pedido(oid):
    pedido = db.get_or_404(Order, oid)
    if pedido.user_id != g.cliente.id:
        abort(404)
    return pedido


@account.route("/cadastro", methods=["GET", "POST"])
def cadastro():
    if request.method == "POST" and not pode_cadastrar():
        flash("Muitas tentativas de cadastro seguidas. Aguarde um pouco e tente de novo.")
        return render_template("shop/cadastro.html", f=request.form), 429
    if request.method == "POST":
        f = request.form
        nome, email, senha = " ".join(f.get("nome", "").split()), f.get("email", "").strip().lower(), f.get("senha", "")
        empresa, erro_empresa = ler_empresa(f, f.get("tipo", ""))
        if not nome or "@" not in email or len(email) > 120:
            flash("Informe um nome e um e-mail válidos.")
        elif erro_empresa:
            flash(erro_empresa)
        elif f.get("aceite") != "1":
            flash("Para criar a conta, é preciso aceitar a Política de Privacidade e os Termos de Uso.")
        elif not 8 <= len(senha) <= 128:
            flash("A senha deve ter entre 8 e 128 caracteres.")
        elif senha != f.get("senha2", ""):
            flash("As senhas não coincidem.")
        else:
            user = User(name=nome[:120], email=email, is_admin=False, termos_aceitos_em=agora_utc(),
                        termos_versao=lgpd.VERSAO_TERMOS)
            aplicar_empresa(user, empresa)
            user.set_password(senha)
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("Este e-mail ou documento já está cadastrado. Tente entrar.")
            else:
                pode_enviar_email(email)
                if not enviar_confirmacao(user):
                    flash("Sua conta foi criada, mas não conseguimos enviar o e-mail de confirmação agora. "
                          "Tente reenviar abaixo em alguns minutos; se continuar, fale com o Marketplace B2B.")
                return redirect(url_for("account.confirme", email=email))
    return render_template("shop/cadastro.html", f=request.form)


@account.post("/cadastro/cnpj")
def consultar_cnpj_cadastro():
    """Preenche o formulário de cadastro com os dados da Receita (chamado pelo navegador ao digitar o CNPJ)."""
    doc, erro = ler_documento("pj", request.form.get("documento", ""))
    if erro:
        return jsonify(ok=False, msg=erro)
    if not pode_consultar_cnpj():
        return jsonify(ok=False, msg="Muitas consultas seguidas. Preencha a razão social à mão."), 429
    receita, erro = checar_na_receita(doc)
    if erro:
        return jsonify(ok=False, msg=erro)
    if not receita:
        return jsonify(ok=True, verificado=False,
                       msg="Não conseguimos consultar a Receita agora. Preencha a razão social; conferimos depois.")
    return jsonify(ok=True, verificado=True, razao_social=receita["razao_social"],
                   nome_fantasia=receita["nome_fantasia"], endereco=receita["endereco"],
                   msg=f"Empresa encontrada na Receita: {receita['razao_social']} ({receita['situacao']}).")


@account.route("/entrar", methods=["GET", "POST"])
def entrar():
    if request.method == "POST":
        email, senha = request.form.get("email", "").strip().lower(), request.form.get("senha", "")
        if bloqueado(email):
            flash("Muitas tentativas. Aguarde 10 minutos e tente de novo.")
        else:
            user = User.query.filter_by(email=email, is_admin=False).first()
            if check_password_hash(user.password_hash if user else HASH_FALSO, senha) and user:
                acertou(email)
                if not user.email_verified:
                    flash("Confirme seu e-mail antes de entrar. Procure a mensagem do Marketplace B2B na sua caixa de entrada.")
                    return redirect(url_for("account.confirme", email=email))
                if user.empresa_id:  # usuário da equipe: entra na conta da empresa
                    titular = db.session.get(User, user.empresa_id)
                    entrar_na_conta(titular)
                    entrar_sessao("membro_id", user)
                else:
                    entrar_na_conta(user)
                return redirect(url_for("account.conta"))
            falhou(email)
            flash("E-mail ou senha incorretos.")
    return render_template("shop/entrar.html")


@account.route("/confirme-seu-email")
def confirme():
    return render_template("shop/confirme.html", email=request.args.get("email", "")[:120])


@account.route("/confirmar/<token>")
def confirmar(token):
    dados = ler_token("confirmar-email", token, VALIDADE_CONFIRMACAO)
    user = db.session.get(User, dados["id"]) if dados else None
    if not user or user.is_admin or user.email != dados.get("e"):
        flash("Link inválido ou expirado. Peça um novo abaixo.")
        return redirect(url_for("account.confirme"))
    primeira_vez = not user.email_verified
    user.email_verified = True
    db.session.commit()
    if primeira_vez and user.tipo == "pj":
        avisos.nova_empresa(user)
    entrar_na_conta(user)
    flash("E-mail confirmado. Bem-vindo ao Marketplace B2B!")
    return redirect(url_for("account.conta"))


@account.route("/reenviar-confirmacao", methods=["POST"])
def reenviar():
    email = request.form.get("email", "").strip().lower()[:120]
    if pode_enviar_email(email):
        user = User.query.filter_by(email=email, is_admin=False).first()
        if user and not user.email_verified:
            enviar_confirmacao(user)
        flash(MSG_ENVIO)
    else:
        flash("Aguarde um minuto antes de pedir outro e-mail.")
    return redirect(url_for("account.confirme", email=email))


@account.route("/esqueci-senha", methods=["GET", "POST"])
def esqueci():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:120]
        if pode_enviar_email(email):
            user = User.query.filter_by(email=email, is_admin=False).first()
            if user:
                enviar_nova_senha(user)
            flash(MSG_ENVIO)
        else:
            flash("Aguarde um minuto antes de pedir outro e-mail.")
        return redirect(url_for("account.esqueci"))
    return render_template("shop/esqueci.html")


@account.route("/nova-senha/<token>", methods=["GET", "POST"])
def nova_senha(token):
    dados = ler_token("nova-senha", token, VALIDADE_SENHA)
    user = db.session.get(User, dados["id"]) if dados else None
    if not user or user.is_admin or not hmac.compare_digest(str(dados.get("h", "")), marca_senha(user, "nova-senha")):
        flash("Link inválido, expirado ou já usado. Peça um novo.")
        return redirect(url_for("account.esqueci"))
    if request.method == "POST":
        nova = request.form.get("nova", "")
        if not 8 <= len(nova) <= 128:
            flash("A nova senha deve ter entre 8 e 128 caracteres.")
        elif nova != request.form.get("nova2", ""):
            flash("As senhas não coincidem.")
        else:
            user.set_password(nova)
            user.email_verified = True  # quem abriu o link provou que o e-mail é dele
            db.session.commit()
            acertou(user.email)
            session.clear()
            flash("Senha alterada. Entre com a nova senha.")
            return redirect(url_for("account.entrar"))
    return render_template("shop/nova_senha.html")


@account.route("/conta")
@cliente_required
def conta():
    pedidos = Order.query.filter_by(user_id=g.cliente.id).order_by(Order.id.desc()).all()
    resumo = {"total": len(pedidos), "abertos": sum(p.status in ("pendente", "orcamento", "aprovacao") for p in pedidos),
              "gasto": sum((p.total for p in pedidos if p.status == "pago"), Decimal(0))}
    return render_template("shop/conta.html", cliente=g.cliente, pedidos=pedidos[:3], resumo=resumo,
                           empresa=tem_condicoes_de_empresa(g.cliente), prazos=prazos_do_cliente(g.cliente),
                           minimo=pedido_minimo(), credito_livre=credito.disponivel(g.cliente),
                           avisos_chegada=avise_me.do_cliente(g.cliente),
                           para_aprovar=[p for p in pedidos if p.status == "aprovacao"]
                           if equipe_empresa.pode_aprovar(g.membro) else [])


@account.get("/conta/pedidos.xlsx")
@cliente_required
def pedidos_planilha():
    conteudo = minha_conta.planilha_pedidos(g.cliente, METODOS)
    return current_app.response_class(conteudo, mimetype=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"), headers={
        "Content-Disposition": f"attachment; filename=meus-pedidos-marketplace-{agora_utc():%Y-%m-%d}.xlsx",
        "Cache-Control": "private, no-store"})


@account.route("/conta/pedidos")
@cliente_required
def pedidos():
    lista = Order.query.filter_by(user_id=g.cliente.id).order_by(Order.id.desc()).all()
    return render_template("shop/pedidos.html", cliente=g.cliente, pedidos=lista)


@account.route("/conta/pedidos/<int:oid>")
@cliente_required
def pedido_detalhe(oid):
    o = meu_pedido(oid)
    import devolucoes
    return render_template("shop/conta_pedido.html", cliente=g.cliente, o=o, metodos=METODOS,
                           pode_aprovar=equipe_empresa.pode_aprovar(g.membro),
                           bloqueio_edicao=editar_pedido.motivo_bloqueio_cliente(o),
                           tipos=entrega.TIPOS, dev=devolucoes, dev_bloqueio=devolucoes.motivo_bloqueio(o))


@account.post("/conta/pedidos/<int:oid>/devolucao")
@cliente_required
def pedir_devolucao(oid):
    import devolucoes
    o = meu_pedido(oid)
    try:
        d = devolucoes.solicitar(o, g.membro or g.cliente, request.form.get("tipo", ""), request.form.get("motivo"),
                                 request.form)
        flash(f"Pedido de {devolucoes.TIPOS[d.tipo].lower()} enviado. O Marketplace B2B responde por e-mail.")
    except devolucoes.ErroDevolucao as e:
        flash(str(e))
    return redirect(url_for("account.pedido_detalhe", oid=o.id) + "#devolucao")


@account.get("/conta/pedidos/<int:oid>/documentos/<int:aid>")
@cliente_required
def pedido_documento(oid, aid):
    o = meu_pedido(oid)
    a = next((a for a in o.arquivos if a.id == aid), None)
    if not a:
        abort(404)
    return entrega.baixar(a)


@account.route("/conta/pedidos/<int:oid>/repetir", methods=["POST"])
@cliente_required
def repetir(oid):
    cart = dict(session.get("cart", {}))
    for item in meu_pedido(oid).items:
        p = item.product
        qtd = ajustar_qtd(p, int(cart.get(str(p.id), 0)) + item.quantity) if p.active else 0
        if qtd:
            cart[str(p.id)] = qtd
    session["cart"] = cart
    flash("Itens adicionados ao carrinho (os que estão sem estoque foram ignorados).")
    return redirect(url_for("shop.cart"))


@account.get("/conta/documentos")
@cliente_required
def documentos():
    mes, tipo = request.args.get("mes", ""), request.args.get("tipo", "")
    lista, meses = minha_conta.documentos(g.cliente, mes, tipo if tipo in entrega.TIPOS else "")
    return render_template("shop/conta_documentos.html", cliente=g.cliente, docs=lista, meses=meses, mes=mes,
                           tipo=tipo, tipos=entrega.TIPOS, nome_do_mes=minha_conta.nome_do_mes)


@account.route("/conta/produtos", methods=["GET", "POST"])
@cliente_required
def produtos_comprados():
    itens = minha_conta.produtos_comprados(g.cliente)
    if request.method == "POST":
        cart = dict(session.get("cart", {}))
        adicionados, fora = 0, []
        por_id = {i["p"].id: i["p"] for i in itens}
        for pid_txt in request.form.getlist("pid"):
            p = por_id.get(int(pid_txt)) if pid_txt.isdigit() else None
            if not p:
                continue
            qtd = request.form.get(f"qtd_{p.id}", 0, type=int)
            nova = ajustar_qtd(p, int(cart.get(str(p.id), 0)) + qtd) if p.active and qtd > 0 else 0
            if nova:
                cart[str(p.id)] = nova
                adicionados += 1
            else:
                fora.append(p.name)
        session["cart"] = cart
        if not adicionados and not fora:
            flash("Marque os produtos que quer comprar de novo.")
            return redirect(url_for("account.produtos_comprados"))
        flash(f"{adicionados} produto(s) no carrinho." + (f" Sem estoque ou indisponível: {', '.join(fora)}." if fora else ""))
        return redirect(url_for("shop.cart"))
    precos = {i["p"].id: preco_unitario(i["p"], max(i["ultima_qtd"], 1), g.cliente) for i in itens}
    return render_template("shop/conta_produtos.html", cliente=g.cliente, itens=itens, precos=precos,
                           disponivel=disponivel)


@account.route("/conta/enderecos", methods=["GET", "POST"])
@cliente_required
def enderecos():
    if request.method == "POST":
        eid = request.form.get("eid", type=int)
        e = Endereco.query.filter_by(id=eid, user_id=g.cliente.id).first_or_404() if eid else None
        dados, erro = minha_conta.ler_endereco(request.form, g.cliente, eid)
        if erro:
            flash(erro)
            return render_template("shop/conta_enderecos.html", cliente=g.cliente, f=request.form, editar=eid,
                                   fmt_cep=minha_conta.formatar_cep)
        if not e:
            e = Endereco(user_id=g.cliente.id, padrao=not g.cliente.enderecos)
            db.session.add(e)
        for campo, valor in dados.items():
            setattr(e, campo, valor)
        db.session.commit()
        flash(f"Endereço “{e.apelido}” salvo.")
        return redirect(url_for("account.enderecos"))
    return render_template("shop/conta_enderecos.html", cliente=g.cliente, f={},
                           editar=request.args.get("editar", type=int), fmt_cep=minha_conta.formatar_cep)


@account.post("/conta/enderecos/<int:eid>/padrao")
@cliente_required
def endereco_padrao(eid):
    e = Endereco.query.filter_by(id=eid, user_id=g.cliente.id).first_or_404()
    for outro in g.cliente.enderecos:
        outro.padrao = outro.id == e.id
    db.session.commit()
    flash(f"“{e.apelido}” agora é o endereço principal: ele já vem preenchido na finalização.")
    return redirect(url_for("account.enderecos"))


@account.post("/conta/enderecos/<int:eid>/excluir")
@cliente_required
def endereco_excluir(eid):
    e = Endereco.query.filter_by(id=eid, user_id=g.cliente.id).first_or_404()
    era_padrao = e.padrao
    db.session.delete(e)
    db.session.flush()
    if era_padrao:
        resto = Endereco.query.filter_by(user_id=g.cliente.id).order_by(Endereco.id).first()
        if resto:
            resto.padrao = True
    db.session.commit()
    flash(f"Endereço “{e.apelido}” removido.")
    return redirect(url_for("account.enderecos"))


# ---------- listas de compra ----------
@account.route("/conta/listas", methods=["GET", "POST"])
@cliente_required
def listas_lista():
    if request.method == "POST":
        lista, erro = listas.nova(g.cliente, request.form.get("nome", ""))
        if erro:
            flash(erro)
            return redirect(url_for("account.listas_lista"))
        db.session.commit()
        flash(f"Lista “{lista.nome}” criada. Agora é só salvar produtos nela (na página do produto ou do carrinho).")
        return redirect(url_for("account.lista_ver", lid=lista.id))
    return render_template("shop/conta_listas.html", cliente=g.cliente, listas=g.cliente.listas)


@account.route("/conta/listas/<int:lid>", methods=["GET", "POST"])
@cliente_required
def lista_ver(lid):
    lista = listas.da_conta(g.cliente, lid) or abort(404)
    if request.method == "POST":
        f, acao = request.form, request.form.get("acao", "")
        if acao.startswith("remover:"):
            item = next((i for i in lista.itens if str(i.id) == acao.split(":", 1)[1]), None)
            if item:
                lista.itens.remove(item)
                db.session.commit()
            return redirect(url_for("account.lista_ver", lid=lid))
        if acao in ("salvar", "carrinho"):  # guarda as quantidades digitadas (0 tira da lista)
            for i in list(lista.itens):
                qtd = f.get(f"qtd_{i.id}", type=int)
                if qtd is not None and qtd <= 0:
                    lista.itens.remove(i)
                elif qtd:
                    i.qtd = min(qtd, 1_000_000)
            db.session.commit()
        if acao == "carrinho":
            carrinho, adicionados, fora = listas.para_o_carrinho(lista, session.get("cart", {}))
            session["cart"] = carrinho
            flash(f"{adicionados} produto(s) da lista “{lista.nome}” no carrinho."
                  + (f" Ficaram de fora (sem estoque ou fora de linha): {', '.join(fora)}." if fora else ""))
            return redirect(url_for("shop.cart"))
        if acao == "renomear":
            nome = listas.nome_valido(f.get("nome", ""))
            outra = ListaCompra.query.filter(ListaCompra.user_id == g.cliente.id, ListaCompra.nome == nome,
                                             ListaCompra.id != lista.id).first()
            if not nome or outra:
                flash("Escolha um nome que você ainda não usou em outra lista.")
            else:
                lista.nome = nome
                db.session.commit()
        elif acao == "excluir":
            nome = lista.nome
            db.session.delete(lista)
            db.session.commit()
            flash(f"Lista “{nome}” apagada.")
            return redirect(url_for("account.listas_lista"))
        elif acao == "salvar":
            flash("Lista salva.")
        return redirect(url_for("account.lista_ver", lid=lid))
    linhas, total = listas.linhas(lista, g.cliente)
    return render_template("shop/conta_lista.html", cliente=g.cliente, lista=lista, linhas=linhas, total=total)


def _lista_escolhida(f):
    """Lista escolhida no formulário: uma existente ou "nova" (com o nome digitado). Devolve (lista, erro)."""
    escolha = f.get("lista", "")
    if escolha == "nova":
        return listas.nova(g.cliente, f.get("nome_lista", ""))
    lista = listas.da_conta(g.cliente, int(escolha)) if escolha.isdigit() else None
    return (lista, None) if lista else (None, "Escolha uma lista.")


@account.post("/conta/listas/adicionar")
@cliente_required
def lista_adicionar():
    p = db.get_or_404(Product, request.form.get("pid", 0, type=int))
    if not p.active:
        abort(404)
    lista, erro = _lista_escolhida(request.form)
    if not erro:
        erro = listas.colocar(lista, p, request.form.get("qtd", 1, type=int) or 1)
    if erro:
        db.session.rollback()
        flash(erro)
    else:
        db.session.commit()
        flash(f"{p.name} salvo na lista “{lista.nome}”.")
    return redirect(link_produto(p))


@account.post("/conta/listas/do-carrinho")
@cliente_required
def lista_do_carrinho():
    carrinho = session.get("cart", {})
    produtos_carrinho = {p.id: p for p in Product.query.filter(Product.id.in_([int(i) for i in carrinho])).all()} if carrinho else {}
    if not produtos_carrinho:
        flash("O carrinho está vazio.")
        return redirect(url_for("shop.cart"))
    lista, erro = _lista_escolhida(request.form)
    for pid, qtd in carrinho.items():
        if erro:
            break
        p = produtos_carrinho.get(int(pid))
        if p:
            erro = listas.colocar(lista, p, int(qtd), somar=False)
    if erro:
        db.session.rollback()
        flash(erro)
        return redirect(url_for("shop.cart"))
    db.session.commit()
    flash(f"Carrinho salvo na lista “{lista.nome}”. Da próxima vez, é só abrir a lista e mandar tudo ao carrinho.")
    return redirect(url_for("account.lista_ver", lid=lista.id))


@account.post("/conta/pedidos/<int:oid>/alterar")
@cliente_required
def alterar_pedido(oid):
    o = meu_pedido(oid)
    ok, msg, mudancas = editar_pedido.aplicar_cliente(o, g.cliente, request.form, g.membro)
    flash(msg)
    if ok and mudancas:
        o = db.session.get(Order, oid)
        avisos.pedido_alterado_pelo_cliente(o, mudancas)
        historico.registrar(oid, "Alterado pelo cliente: " + "; ".join(mudancas), autor="cliente",
                            nome=(g.membro or g.cliente).name)
        if any(m.startswith("tirou") or "→" in m for m in mudancas):
            avise_me.disparar()  # devolveu estoque: pode ter alguém esperando
    return redirect(url_for("account.pedido_detalhe", oid=oid) + ("" if ok else "#alterar"))


# ---------- equipe da empresa (vários usuários na mesma conta) ----------
VALIDADE_CONVITE = 7 * 86400


def enviar_convite_membro(u, titular):
    token = _assinador("convite-empresa").dumps({"id": u.id, "h": u.password_hash[-16:]})
    link = link_externo(url_for("account.convite", token=token))
    return enviar_email(u.email, f"Convite para comprar pela {titular.razao_social or titular.name} no Marketplace B2B",
                        f"Olá, {u.name}!\n\n{titular.name} convidou você para comprar no Marketplace B2B pela conta da "
                        f"{titular.razao_social or titular.name}, como {equipe_empresa.nome_papel(u).lower()}.\n\n"
                        f"O convite vale por 7 dias. Depois de criar a senha, é só entrar com o seu e-mail.\n\n"
                        f"Criar minha senha: {link}\n\n"
                        "Se você não esperava este convite, ignore este e-mail.\n\nEquipe Marketplace B2B"), link


def so_titular(view):
    """Telas que só o titular da conta (quem tem o CNPJ) usa: a equipe."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.membro:
            flash("Só o titular da conta da empresa pode fazer isso.")
            return redirect(url_for("account.conta"))
        return view(*args, **kwargs)
    return wrapper


@account.route("/conta/equipe", methods=["GET", "POST"])
@cliente_required
@so_titular
def equipe():
    titular = g.cliente
    if titular.tipo != "pj":
        flash("A equipe é para contas de empresa (CNPJ).")
        return redirect(url_for("account.conta"))
    link = None
    if request.method == "POST":
        f, acao = request.form, request.form.get("acao", "convidar")
        espere = "Aguarde um minuto antes de enviar outro convite para esse e-mail (e no máximo 10 por hora)."
        if acao == "convidar":
            dados, erro = equipe_empresa.ler(f, titular)
            if erro:
                flash(erro)
            elif not pode_enviar_email(dados["email"]):  # convite também é e-mail: mesmo limite do cadastro
                flash(espere)
            else:
                u = equipe_empresa.novo_membro(titular, dados)
                db.session.commit()
                ok, link = enviar_convite_membro(u, titular)
                flash(f"Convite enviado para {u.email}." if ok else
                      f"{u.name} foi cadastrado, mas o e-mail não saiu. Copie o link abaixo e envie você mesmo.")
                if ok:
                    link = None
        else:
            u = next((m for m in titular.membros if m.id == f.get("uid", type=int)), None)
            if not u:
                abort(404)
            if acao == "salvar":
                dados, erro = equipe_empresa.ler(f, titular, u.id)
                if erro:
                    flash(erro)
                else:
                    u.papel, u.limite_aprovacao = dados["papel"], dados["limite_aprovacao"]
                    db.session.commit()
                    flash(f"{u.name}: {equipe_empresa.nome_papel(u).lower()}.")
            elif acao == "remover":
                nome = u.name
                titular.membros.remove(u)
                db.session.commit()
                flash(f"{nome} não compra mais pela conta da empresa.")
            elif acao == "reenviar" and not pode_enviar_email(u.email):
                flash(espere)
            elif acao == "reenviar":
                ok, link = enviar_convite_membro(u, titular)
                flash(f"Convite reenviado para {u.email}." if ok else "O e-mail não saiu. Copie o link abaixo.")
                if ok:
                    link = None
        if not link:
            return redirect(url_for("account.equipe"))
    return render_template("shop/conta_equipe.html", cliente=titular, membros=titular.membros, link=link,
                           papeis=equipe_empresa.PAPEIS)


@account.route("/convite/<token>", methods=["GET", "POST"])
def convite(token):
    """O usuário convidado pela empresa cria a senha (o link vale 7 dias e uma vez só)."""
    dados = ler_token("convite-empresa", token, VALIDADE_CONVITE)
    u = db.session.get(User, dados["id"]) if dados else None
    if not u or u.is_admin or not u.empresa_id or u.password_hash[-16:] != dados.get("h"):
        flash("Convite inválido, vencido ou já usado. Peça um novo para quem convidou você.")
        return redirect(url_for("account.entrar"))
    if request.method == "POST":
        nova = request.form.get("nova", "")
        if not 8 <= len(nova) <= 128:
            flash("A senha deve ter entre 8 e 128 caracteres.")
        elif nova != request.form.get("nova2", ""):
            flash("As senhas não coincidem.")
        else:
            u.set_password(nova)
            u.email_verified, u.termos_aceitos_em, u.termos_versao = True, agora_utc(), lgpd.VERSAO_TERMOS
            db.session.commit()
            session.clear()
            flash("Senha criada. Entre com o seu e-mail e a nova senha.")
            return redirect(url_for("account.entrar"))
    return render_template("shop/nova_senha.html", convite=u)


@account.post("/conta/pedidos/<int:oid>/aprovacao")
@cliente_required
def aprovar_pedido(oid):
    o = meu_pedido(oid)
    quem = g.membro or g.cliente
    if not equipe_empresa.pode_aprovar(g.membro):
        flash("Só o titular da conta e os aprovadores da empresa aprovam pedidos.")
    elif o.status != "aprovacao":
        flash("Este pedido não está aguardando aprovação.")
    elif request.form.get("acao") == "aprovar" and o.payment_method == "faturado" and not credito.cabe(g.cliente, o.total):
        flash(credito.mensagem(g.cliente))
    elif request.form.get("acao") == "aprovar":
        ok, erro = mudar_status(o, "pendente")  # agora prende o estoque e segue para o Marketplace B2B
        if ok:
            o = db.session.get(Order, oid)
            historico.registrar(oid, f"Aprovado por {quem.name} (empresa)", autor="cliente")
            avisos.pedido_recebido(o)
            avisos.estoque_baixo([(i.product, i.quantity) for i in o.items])
            flash(f"Pedido #{oid} aprovado e enviado para o Marketplace B2B.")
        else:
            flash(erro)
    elif request.form.get("acao") == "recusar":
        ok, erro = mudar_status(o, "cancelado")
        if ok:
            o = db.session.get(Order, oid)
            historico.registrar(oid, f"Recusado por {quem.name} (empresa)", autor="cliente")
            avisos.pedido_recusado_pela_empresa(o, quem)
            flash(f"Pedido #{oid} recusado.")
        else:
            flash(erro)
    return redirect(url_for("account.pedido_detalhe", oid=oid))


@account.route("/conta/pedidos/<int:oid>/cancelar", methods=["POST"])
@cliente_required
def cancelar(oid):
    pedido = meu_pedido(oid)
    if pedido.status not in ("pendente", "orcamento", "aprovacao") or pedido.envio:
        flash("Só dá para cancelar orçamentos e pedidos que ainda aguardam pagamento e não foram enviados.")
    else:
        ok, erro = mudar_status(pedido, "cancelado")
        flash(f"Pedido #{oid} cancelado." if ok else erro)
        if ok:
            avisos.pedido_cancelado(pedido, pela_loja=False)
            historico.registrar(oid, "Cancelado pelo cliente", autor="cliente", nome=(g.membro or g.cliente).name)
            avise_me.disparar()  # o estoque voltou: pode ter alguém esperando
    return redirect(url_for("account.pedido_detalhe", oid=oid))


@account.route("/conta/dados", methods=["GET", "POST"])
@cliente_required
def dados():
    if g.membro:  # usuário da equipe: muda só o próprio nome e telefone; os dados da empresa são do titular
        if request.method == "POST":
            nome = " ".join(request.form.get("nome", "").split())
            if not nome:
                flash("Informe seu nome.")
            else:
                g.membro.name, g.membro.phone = nome[:120], " ".join(request.form.get("telefone", "").split())[:30]
                db.session.commit()
                flash("Dados atualizados.")
                return redirect(url_for("account.dados"))
        return render_template("shop/dados_membro.html", cliente=g.cliente, eu=g.membro)
    if request.method == "POST":
        f, c = request.form, g.cliente
        nome = " ".join(f.get("nome", "").split())
        empresa, erro = (None, None) if c.documento else ler_empresa(f, f.get("tipo", ""))
        if not nome:
            flash("Informe seu nome.")
        elif erro:
            flash(erro)
        else:
            if empresa:  # conta antiga, sem documento: informa uma vez
                aplicar_empresa(c, empresa)
            elif c.tipo == "pj" and not c.empresa_aprovada:  # depois de aprovada, só o Marketplace B2B muda
                razao = " ".join(f.get("razao_social", "").split())[:160]
                c.razao_social = razao or c.razao_social
                c.inscricao_estadual = f.get("inscricao_estadual", "").strip()[:30]
            g.cliente.name = nome[:120]
            if not sms_ligado():  # com SMS ligado, o celular só muda pela confirmação de código
                g.cliente.phone = " ".join(request.form.get("telefone", "").split())[:30]
            g.cliente.address = " ".join(request.form.get("endereco", "").split())[:300]
            db.session.commit()
            flash("Dados atualizados.")
            return redirect(url_for("account.dados"))
    return render_template("shop/dados.html", cliente=g.cliente, sms=sms_ligado(), fmt_cel=formatar_celular)


@account.route("/conta/senha", methods=["POST"])
@cliente_required
def senha():
    quem = g.membro or g.cliente  # cada pessoa da equipe tem a própria senha
    f, email = request.form, quem.email
    nova = f.get("nova", "")
    if bloqueado(email):
        flash("Muitas tentativas. Aguarde 10 minutos e tente de novo.")
    elif not check_password_hash(quem.password_hash, f.get("atual", "")):
        falhou(email)
        flash("A senha atual está incorreta.")
    elif not 8 <= len(nova) <= 128:
        flash("A nova senha deve ter entre 8 e 128 caracteres.")
    elif nova != f.get("nova2", ""):
        flash("As senhas novas não coincidem.")
    else:
        acertou(email)
        quem.set_password(nova)
        db.session.commit()
        renovar_sessao(quem)  # este navegador continua na conta; os outros precisam entrar de novo
        flash("Senha alterada. Se a conta estava aberta em outro aparelho, lá será preciso entrar de novo.")
    return redirect(url_for("account.dados"))


@account.get("/conta/privacidade")
@cliente_required
def privacidade():
    if g.membro:
        return render_template("shop/privacidade_conta.html", cliente=g.membro, abertos=0, eh_membro=True)
    return render_template("shop/privacidade_conta.html", cliente=g.cliente, abertos=lgpd.pedidos_abertos(g.cliente))


@account.post("/conta/lembretes")
@cliente_required
def lembretes():
    quem = g.membro or g.cliente
    quem.lembrete_carrinho = "lembrete_carrinho" in request.form
    db.session.commit()
    flash("Lembretes do carrinho " + ("ligados." if quem.lembrete_carrinho else "desligados."))
    return redirect(url_for("account.privacidade"))


@account.route("/lembretes/parar/<token>", methods=["GET", "POST"])
def parar_lembrete(token):
    """Link do e-mail do carrinho: para os lembretes sem precisar entrar na conta (confirma com um botão)."""
    import carrinho_abandonado
    user = carrinho_abandonado.quem_parar(token)
    if not user:
        abort(404)
    parado = request.method == "POST"
    if parado and user.lembrete_carrinho:
        user.lembrete_carrinho = False
        db.session.commit()
    return render_template("shop/parar_lembrete.html", token=token, parado=parado or not user.lembrete_carrinho)


@account.get("/conta/meus-dados.json")
@cliente_required
def baixar_dados():
    resp = jsonify(lgpd.exportar_membro(g.membro) if g.membro else lgpd.exportar(g.cliente))
    resp.headers["Content-Disposition"] = "attachment; filename=meus-dados-marketplace.json"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@account.post("/conta/excluir")
@cliente_required
def excluir_conta():
    if g.membro:  # usuário da equipe: sai da empresa (os pedidos ficam com a empresa)
        m = g.membro
        if bloqueado(m.email):
            flash("Muitas tentativas. Aguarde 10 minutos e tente de novo.")
        elif not check_password_hash(m.password_hash, request.form.get("senha", "")):
            falhou(m.email)
            flash("Senha incorreta. A conta não foi excluída.")
        else:
            g.cliente.membros.remove(m)
            db.session.commit()
            session.clear()
            flash("Seu acesso foi excluído. Obrigado por ter comprado com o Marketplace B2B.")
            return redirect(url_for("shop.index"))
        return redirect(url_for("account.privacidade"))
    c = g.cliente
    if bloqueado(c.email):
        flash("Muitas tentativas. Aguarde 10 minutos e tente de novo.")
    elif not check_password_hash(c.password_hash, request.form.get("senha", "")):
        falhou(c.email)
        flash("Senha incorreta. A conta não foi excluída.")
    else:
        abertos = lgpd.excluir_conta(c)
        if abertos:
            flash(f"Você tem {abertos} pedido(s) ou orçamento(s) em aberto. Cancele ou aguarde a conclusão antes.")
        else:
            session.clear()
            flash("Sua conta foi excluída. Obrigado por ter comprado com o Marketplace B2B.")
            return redirect(url_for("shop.index"))
    return redirect(url_for("account.privacidade"))


@account.route("/sair", methods=["POST"])
def sair():
    sair_da_loja()  # também tira os pedidos recentes: o próximo a usar o navegador não os vê
    return redirect(url_for("shop.index"))
