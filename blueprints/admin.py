"""Painel de administração: blueprint, login (com verificação em duas etapas), permissões e a conta do
administrador. As telas de cada área ficam em blueprints/painel_*.py e registram as rotas neste blueprint."""
import time
from functools import wraps

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template, request, session,
    url_for)
from werkzeug.security import check_password_hash
import dois_fatores
from email_utils import enviar_email_detalhado, provedor_email
import permissoes
from pix import pix_qr_svg
from models import User, db
from security import (HASH_FALSO, _sair, acertou, bloqueado, entrar_sessao, falhou, pode_tentar_codigo_2fa,
                      renovar_sessao)

admin = Blueprint("admin", __name__)


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = db.session.get(User, session.get("admin_id") or 0)
        if not user or not user.is_admin:
            session.pop("admin_id", None)  # não apaga carrinho/login do cliente
            return redirect(url_for("admin.login"))
        if request.method == "GET":
            import backup_auto
            backup_auto.talvez_enviar()
        if not permissoes.pode_tela(user, request.endpoint):  # cada administrador vê só as áreas liberadas
            destino = permissoes.primeira_tela(user)
            if request.endpoint != "admin.dashboard":
                flash("Você não tem acesso a essa parte do painel. Fale com o dono da loja se precisar.")
            if request.method != "GET" or destino == request.endpoint:
                abort(403)
            return redirect(url_for(destino))
        return view(*args, **kwargs)
    return wrapper


@admin.app_context_processor
def _permissoes_do_painel():
    if request.blueprint != "admin":
        return {}
    user = db.session.get(User, session.get("admin_id") or 0)
    return {"pode": lambda area: permissoes.pode(user, area), "acesso_total": permissoes.total(user)}


@admin.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email, senha = request.form.get("email", "").strip().lower(), request.form.get("password", "")
        if bloqueado(email):
            flash("Muitas tentativas. Aguarde 10 minutos e tente de novo.")
        else:
            user = User.query.filter_by(email=email, is_admin=True).first()
            if check_password_hash(user.password_hash if user else HASH_FALSO, senha) and user:
                session.clear()
                if dois_fatores.exigido(user):  # senha certa: falta o código do app
                    session["admin_2fa"] = {"uid": user.id, "ate": int(time.time()) + 300}
                    return redirect(url_for("admin.login_codigo"))
                acertou(email)
                entrar_sessao("admin_id", user)
                return redirect(url_for("admin.dashboard"))
            falhou(email)
            flash("E-mail ou senha incorretos. Confira os dados e tente de novo.")
    return render_template("admin/login.html")


@admin.route("/login/codigo", methods=["GET", "POST"])
def login_codigo():
    """Segunda etapa do login: código do app autenticador (ou um código de recuperação)."""
    pendente = session.get("admin_2fa") or {}
    user = db.session.get(User, pendente.get("uid") or 0)
    if not user or not user.is_admin or pendente.get("ate", 0) < time.time() or not dois_fatores.exigido(user):
        session.pop("admin_2fa", None)
        flash("Entre de novo com e-mail e senha.")
        return redirect(url_for("admin.login"))
    if request.method == "POST":
        if bloqueado(user.email):
            session.pop("admin_2fa", None)
            flash("Muitas tentativas. Aguarde 10 minutos e entre de novo.")
            return redirect(url_for("admin.login"))
        if not pode_tentar_codigo_2fa(user.id):  # vale para a conta, de qualquer IP
            session.pop("admin_2fa", None)
            flash("Muitas tentativas de código nesta conta. Aguarde uma hora e entre de novo.")
            return redirect(url_for("admin.login"))
        digitado = request.form.get("codigo", "")
        passo = dois_fatores.conferir(user.totp_segredo, digitado, user.totp_ultimo)
        recuperacao = not passo and dois_fatores.usar_codigo_recuperacao(user, digitado)
        if passo or recuperacao:
            if passo:
                user.totp_ultimo = passo
            db.session.commit()
            acertou(user.email)
            session.clear()
            entrar_sessao("admin_id", user)
            if recuperacao:
                flash(f"Você entrou com um código de recuperação (cada um vale uma vez; restam "
                      f"{dois_fatores.restantes(user)}). Se perdeu o celular, gere novos em Minha conta.")
            return redirect(url_for("admin.dashboard"))
        falhou(user.email)
        flash("Código incorreto ou já usado. Confira o app e digite o código que aparece agora.")
    return render_template("admin/login_codigo.html")


@admin.route("/logout", methods=["POST"])
def logout():
    _sair("admin_id")
    return redirect(url_for("shop.index"))


@admin.post("/conta/email-teste")
@login_required
def email_teste():
    """Manda um e-mail de teste para o admin e mostra o erro exato, se houver."""
    user = db.session.get(User, session["admin_id"])
    ok, motivo = enviar_email_detalhado(user.email, "Teste de e-mail do Marketplace B2B",
                                        "Se você recebeu esta mensagem, o envio de e-mails do site está funcionando.")
    flash(f"E-mail de teste enviado para {user.email}. Confira a caixa de entrada e o spam." if ok
          else f"O e-mail de teste falhou: {motivo}")
    return redirect(url_for("admin.conta"))


@admin.route("/conta", methods=["GET", "POST"])
@login_required
def conta():
    """O próprio admin troca nome, e-mail e senha pelo painel (sem precisar do Shell do Render)."""
    user = db.session.get(User, session["admin_id"])
    if request.method == "POST":
        f = request.form
        if bloqueado(user.email):
            flash("Muitas tentativas. Aguarde 10 minutos e tente de novo.")
        elif not check_password_hash(user.password_hash, f.get("atual", "")):
            falhou(user.email)
            flash("A senha atual está incorreta.")
        elif f.get("acao") == "dados":
            nome, email = " ".join(f.get("nome", "").split()), f.get("email", "").strip().lower()
            outro = User.query.filter(User.email == email, User.id != user.id).first()
            if not nome or "@" not in email or len(email) > 120:
                flash("Informe um nome e um e-mail válidos.")
            elif outro:
                flash("Esse e-mail já é usado por outra conta (talvez uma conta de cliente da loja).")
            else:
                acertou(user.email)
                user.name, user.email = nome[:120], email
                db.session.commit()
                flash("Dados atualizados. Use o novo e-mail no próximo login.")
        elif f.get("acao") == "senha":
            nova = f.get("nova", "")
            if not 12 <= len(nova) <= 128:
                flash("A nova senha deve ter entre 12 e 128 caracteres.")
            elif nova != f.get("nova2", ""):
                flash("As senhas novas não coincidem.")
            else:
                acertou(user.email)
                user.set_password(nova)
                db.session.commit()
                renovar_sessao(user)  # este navegador continua no painel; os outros precisam entrar de novo
                flash("Senha alterada. Quem estava no painel com esta conta em outro aparelho precisa entrar de novo.")
        return redirect(url_for("admin.conta"))
    return _pagina_conta(user)


def _pagina_conta(user, codigos_novos=None):
    novo = session.get("2fa_novo") if not dois_fatores.ativo(user) else None
    return render_template("admin/conta.html", user=user, provedor=provedor_email(),
                           remetente=current_app.config["MAIL_FROM"], dois_ativo=dois_fatores.ativo(user),
                           dois_desligado=dois_fatores.desligado_pela_variavel(),
                           restantes=dois_fatores.restantes(user), codigos_novos=codigos_novos,
                           qr_novo=pix_qr_svg(dois_fatores.link_do_app(novo, user.email)) if novo else "",
                           segredo_novo=dois_fatores.segredo_formatado(novo) if novo else "")


@admin.post("/conta/duas-etapas")
@login_required
def duas_etapas():
    """Ativar, desativar e gerar novos códigos de recuperação da verificação em duas etapas."""
    user = db.session.get(User, session["admin_id"])
    f, acao = request.form, request.form.get("acao")
    if acao == "cancelar":
        session.pop("2fa_novo", None)
        return redirect(url_for("admin.conta"))
    if acao == "confirmar":  # lê o QR e digita o primeiro código: só então liga
        segredo = session.get("2fa_novo")
        passo = dois_fatores.conferir(segredo, f.get("codigo", "")) if segredo else None
        if not passo:
            flash("Código incorreto. Confira se escaneou o QR Code e digite o código que aparece agora no app.")
            return redirect(url_for("admin.conta"))
        codigos = dois_fatores.ativar(user, segredo, passo)
        db.session.commit()
        session.pop("2fa_novo", None)
        flash("Verificação em duas etapas ativada. Guarde os códigos de recuperação abaixo.")
        return _pagina_conta(user, codigos)
    # as outras ações pedem a senha atual
    if bloqueado(user.email):
        flash("Muitas tentativas. Aguarde 10 minutos e tente de novo.")
        return redirect(url_for("admin.conta"))
    if not check_password_hash(user.password_hash, f.get("atual", "")):
        falhou(user.email)
        flash("A senha atual está incorreta.")
        return redirect(url_for("admin.conta"))
    if acao == "iniciar":
        if not dois_fatores.ativo(user):
            session["2fa_novo"] = dois_fatores.novo_segredo()
        return redirect(url_for("admin.conta") + "#duas-etapas")
    if not dois_fatores.ativo(user):
        return redirect(url_for("admin.conta"))
    digitado = f.get("codigo", "")
    passo = dois_fatores.conferir(user.totp_segredo, digitado, user.totp_ultimo)
    if not passo and not dois_fatores.usar_codigo_recuperacao(user, digitado) and not dois_fatores.desligado_pela_variavel():
        falhou(user.email)
        flash("Código do app incorreto ou já usado.")
        return redirect(url_for("admin.conta"))
    acertou(user.email)
    if acao == "desativar":
        dois_fatores.desativar(user)
        db.session.commit()
        flash("Verificação em duas etapas desativada: o painel volta a pedir só a senha.")
        return redirect(url_for("admin.conta"))
    if acao == "novos_codigos":
        if passo:
            user.totp_ultimo = passo
        codigos, user.codigos_recuperacao = dois_fatores.novos_codigos_recuperacao()
        db.session.commit()
        flash("Novos códigos de recuperação gerados. Os antigos não valem mais.")
        return _pagina_conta(user, codigos)
    return redirect(url_for("admin.conta"))


# ---------- planilhas ----------
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _baixar(conteudo, nome):
    return Response(conteudo, mimetype=XLSX, headers={"Content-Disposition": f"attachment; filename={nome}",
                                                       "Cache-Control": "no-store"})


@admin.get("/hoje")
@login_required
def hoje():
    """Painel do dia: pendências de cada área que o administrador vê."""
    import hoje as painel_do_dia
    user = db.session.get(User, session["admin_id"])
    return render_template("admin/hoje.html", quadros=painel_do_dia.montar(lambda area: permissoes.pode(user, area),
                                                                          permissoes.total(user)))


@admin.get("/manual")
@login_required
def manual():
    """Manual do painel: como usar cada parte (para treinar quem entra na equipe)."""
    return render_template("admin/manual.html")


# as telas de cada área (registram as rotas neste mesmo blueprint; os nomes continuam admin.*)
from blueprints import (painel_clientes, painel_compras, painel_config, painel_custos,  # noqa: E402,F401
                        painel_devolucoes, painel_estoque, painel_financeiro, painel_fiscal, painel_marketing, painel_pedidos,
                        painel_produtos, painel_vendas)
