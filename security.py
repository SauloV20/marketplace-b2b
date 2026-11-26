import hashlib
import hmac
import secrets
import time
from urllib.parse import urlsplit

from flask import abort, current_app, flash, redirect, request, session
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from models import LimiteUso, LoginFalha, db

MAX_FALHAS, BLOQUEIO_S, JANELA_S = 5, 600, 900  # 5 erros em 15 min bloqueiam por 10 min
HASH_FALSO = generate_password_hash("senha-falsa")  # iguala o tempo quando o e-mail não existe

CSP = ("default-src 'self'; script-src 'self'; script-src-attr 'none'; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src 'self' https: data:; frame-ancestors 'none'; form-action 'self'; base-uri 'self'")


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


def _chave(email):
    return hashlib.sha256(f"{request.remote_addr}|{email.lower()}".encode()).hexdigest()


def bloqueado(email):
    reg = db.session.get(LoginFalha, _chave(email))
    return bool(reg and reg.bloqueado_ate > time.time())


def falhou(email, _tentativa=0):
    chave, agora = _chave(email), int(time.time())
    try:
        # trava a linha: dois workers contando ao mesmo tempo não perdem falhas
        reg = db.session.get(LoginFalha, chave, with_for_update=True, populate_existing=True)
        if reg is None:
            reg = LoginFalha(chave=chave, falhas=0, ultima=0, bloqueado_ate=0)
            db.session.add(reg)
        if agora - reg.ultima > JANELA_S:
            reg.falhas = 0
        reg.falhas, reg.ultima = reg.falhas + 1, agora
        if reg.falhas >= MAX_FALHAS:
            reg.falhas, reg.bloqueado_ate = 0, agora + BLOQUEIO_S
        db.session.execute(delete(LoginFalha).where(LoginFalha.ultima < agora - 86400,
                                                    LoginFalha.bloqueado_ate < agora))  # limpa registros velhos
        db.session.commit()
    except IntegrityError:  # outro worker criou o registro no mesmo instante
        db.session.rollback()
        if _tentativa == 0:
            falhou(email, 1)


def acertou(email):
    db.session.execute(delete(LoginFalha).where(LoginFalha.chave == _chave(email)))
    db.session.commit()


# ---------- sessão presa à senha ----------
# Cada login guarda na sessão uma "marca" da senha atual da conta. Quando a senha muda (troca, link de nova senha,
# trocar-senha-admin), as sessões abertas em outros navegadores deixam de valer: quem roubou um cookie perde o acesso.
CHAVES_SESSAO = ("admin_id", "cliente_id", "membro_id")


def marca_senha(user, uso="sessao"):
    """HMAC da senha atual (com a SECRET_KEY): muda quando a senha muda e não revela nada do hash."""
    texto = f"{uso}|{user.id}|{user.password_hash}".encode()
    return hmac.new(current_app.config["SECRET_KEY"].encode(), texto, hashlib.sha256).hexdigest()[:24]


def entrar_sessao(chave, user):
    """Põe a conta na sessão (admin_id, cliente_id ou membro_id) junto com a marca da senha."""
    session[chave] = user.id
    session[chave + "_v"] = marca_senha(user)
    if chave == "admin_id":
        session["admin_desde"] = session["admin_visto"] = int(time.time())
    else:  # cliente e usuário da equipe: a sessão da loja vale por um prazo máximo (CLIENTE_SESSAO_MAX_DIAS)
        session["cliente_desde"] = int(time.time())


def renovar_sessao(user):
    """Depois que a própria pessoa troca a senha, esta sessão continua valendo (as outras não)."""
    for chave in CHAVES_SESSAO:
        if session.get(chave) == user.id:
            session[chave + "_v"] = marca_senha(user)


def sair_da_loja():
    """Sai da conta do cliente (e do usuário da equipe) e limpa tudo o que a sessão da loja guardava."""
    _sair("cliente_id")


def _sair(chave):
    if chave == "admin_id":
        chaves = ("admin_id",)
        session.pop("admin_desde", None)
        session.pop("admin_visto", None)
    else:  # cliente e usuário da equipe saem juntos (a sessão da loja é uma só)
        chaves = ("cliente_id", "membro_id")
        session.pop("orders", None)
        session.pop("cliente_desde", None)
        session.pop("cart", None)  # o carrinho fica guardado na conta e volta no próximo login
        session.pop("cart_sync", None)
    for c in chaves:
        session.pop(c, None)
        session.pop(c + "_v", None)


# chamadas de máquina, sem sessão nem cookie: o backup exige o TAREFAS_TOKEN no cabeçalho; o aviso da Efí só traz um
# token que é conferido na própria API da Efí (nada no aviso é aceito sem essa consulta)
ISENTOS_CSRF = {"tarefas.backup_semanal", "tarefas.efi_boleto", "tarefas.rastreio", "tarefas.carrinhos",
                "tarefas.webhook_melhor_envio"}
# páginas com dados pessoais ou do painel: o navegador não guarda cópia (o "voltar" depois de sair não mostra nada)
SEM_CACHE = {"shop.cart", "shop.checkout", "shop.pedido", "shop.pedido_pix", "shop.rapido"}
# rotas que aceitam arquivo maior que o limite normal do site (MAX_CONTENT_LENGTH)
ENVIO_GRANDE = {"admin.backup_restaurar"}


def init_security(app):
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def ir_para_o_dominio():
        """Quem entra pelo endereço do Render (…onrender.com) vai para o domínio próprio (SITE_URL).

        Só para leitura (GET/HEAD): um formulário enviado não se perde no meio do redirecionamento."""
        site = urlsplit(app.config.get("SITE_URL") or "")
        if (request.method in ("GET", "HEAD") and site.netloc and site.netloc != request.host
                and request.host.endswith(".onrender.com") and not site.netloc.endswith(".onrender.com")):
            destino = f"{site.scheme or 'https'}://{site.netloc}{request.full_path}"
            return redirect(destino.rstrip("?"), 301)

    @app.before_request
    def conferir_sessao():
        """A sessão só vale com a marca da senha atual (e para uma conta que ainda existe)."""
        if request.endpoint == "static" or not any(c in session for c in CHAVES_SESSAO):
            return
        from models import User
        for chave in CHAVES_SESSAO:
            uid = session.get(chave)
            if uid is None:
                continue
            user = db.session.get(User, uid) if isinstance(uid, int) else None
            if not user or not hmac.compare_digest(str(session.get(chave + "_v", "")), marca_senha(user)):
                _sair(chave)
        if "admin_id" in session:
            conferir_tempo_do_painel()
        if "cliente_id" in session:
            conferir_tempo_da_loja()

    def conferir_tempo_da_loja():
        """A sessão do cliente vale no máximo CLIENTE_SESSAO_MAX_DIAS depois do login (mesmo usando): um cookie
        copiado de um computador alheio ou de um backup do navegador não fica valendo para sempre."""
        agora = int(time.time())
        desde = session.get("cliente_desde")
        if not isinstance(desde, int):  # sessão de antes desta regra: conta a partir de agora
            session["cliente_desde"] = agora
            return
        if agora - desde > app.config.get("CLIENTE_SESSAO_MAX_DIAS", 30) * 86400:
            _sair("cliente_id")
            flash("Por segurança, entre de novo na sua conta.")

    def conferir_tempo_do_painel():
        """O painel sai sozinho depois de um tempo parado (computador esquecido aberto) ou longo demais."""
        agora = int(time.time())
        inativo = app.config.get("ADMIN_INATIVO_MIN", 120) * 60
        maximo = app.config.get("ADMIN_SESSAO_MAX_H", 12) * 3600
        visto, desde = session.get("admin_visto"), session.get("admin_desde")
        if not isinstance(visto, int) or not isinstance(desde, int):  # sessão de antes desta regra: conta a partir de agora
            session["admin_desde"] = session["admin_visto"] = agora
            return
        if agora - visto > inativo or agora - desde > maximo:
            _sair("admin_id")
            if request.blueprint == "admin":
                flash("Sua sessão do painel expirou por segurança. Entre de novo.")
            return
        if request.blueprint == "admin" and agora - visto >= 60:  # só uso do painel conta; não regrava a cada clique
            session["admin_visto"] = agora

    @app.before_request
    def checar_csrf():
        if request.method == "POST" and request.endpoint in ENVIO_GRANDE and session.get("admin_id"):
            # tem de valer antes de ler o formulário (abaixo); só para admin já logado
            import backup
            request.max_content_length = backup.MAX_ARQUIVO
        if request.method == "POST" and request.endpoint not in ISENTOS_CSRF:
            enviado = request.form.get("_csrf", "").encode()
            esperado = session.get("_csrf", "").encode()
            if not esperado or not hmac.compare_digest(enviado, esperado):
                abort(400, "Formulário expirado ou inválido. Volte, recarregue a página e tente de novo.")

    @app.after_request
    def cabecalhos(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        import analytics
        resp.headers.setdefault("Content-Security-Policy", analytics.csp(CSP))
        if request.blueprint == "admin":  # o painel nunca vai para o Google (nem a página de login)
            resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        if request.blueprint in ("admin", "account") or request.endpoint in SEM_CACHE:
            resp.headers.setdefault("Cache-Control", "no-store")
        if app.config.get("PRODUCAO") and request.is_secure:  # no ar: o navegador só usa HTTPS neste domínio
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp


def dentro_do_limite(regras):
    """regras = [(chave, janela em segundos, máximo de usos na janela)]. Se todas permitem, registra o uso.

    Fica no banco (tabela limite_uso): os limites valem para todos os workers e não zeram quando o site reinicia."""
    agora = int(time.time())
    hashes = {}
    for chave, janela, maximo in regras:
        h = hashes.setdefault(chave, hashlib.sha256(chave.encode()).hexdigest())
        usados = LimiteUso.query.filter(LimiteUso.chave == h, LimiteUso.momento > agora - janela).count()
        if usados >= maximo:
            return False
    for h in hashes.values():
        db.session.add(LimiteUso(chave=h, momento=agora))
    db.session.execute(delete(LimiteUso).where(LimiteUso.momento < agora - 2 * 86400))  # limpa o que já venceu
    db.session.commit()
    return True


def pode_enviar_email(email):
    """No máximo 1 e-mail por endereço a cada 60 s e 10 por IP por hora."""
    return dentro_do_limite([(f"email:{email.lower()}", 60, 1), (f"email-ip:{request.remote_addr}", 3600, 10)])


def pode_enviar_sms(uid):
    """SMS custa: 1 a cada 60 s e 5 por hora por conta, 10 por hora por IP."""
    conta = f"sms:{uid}"
    return dentro_do_limite([(conta, 60, 1), (conta, 3600, 5), (f"sms-ip:{request.remote_addr}", 3600, 10)])


def pode_cadastrar():
    """No máximo 15 tentativas de cadastro por IP por hora (cada uma pode consultar a Receita e criar uma conta)."""
    return dentro_do_limite([(f"cadastro-ip:{request.remote_addr}", 3600, 15)])


def pode_tentar_codigo_2fa(uid):
    """Código do app no login do painel: além do bloqueio por IP, no máximo 10 tentativas por hora e 30 por dia
    por conta, venham de onde vierem (6 dígitos não aguentam tentativas de muitos IPs ao mesmo tempo)."""
    conta = f"2fa:{uid}"
    return dentro_do_limite([(conta, 3600, 10), (conta, 86400, 30)])


def pode_receber_aviso_efi():
    """Avisos de boleto da Efí: cada um gera uma consulta na API da Efí. Limite generoso por IP para não virar abuso."""
    return dentro_do_limite([(f"efi-aviso-ip:{request.remote_addr}", 600, 120)])


def pode_receber_aviso_melhor_envio():
    """Avisos de etiqueta do Melhor Envio: um por mudança de situação. Limite generoso por IP."""
    return dentro_do_limite([(f"me-aviso-ip:{request.remote_addr}", 600, 300)])


def pode_consultar_cnpj():
    """No máximo 30 consultas de CNPJ por IP por hora (a API é gratuita: não vamos abusar dela)."""
    return dentro_do_limite([(f"cnpj-ip:{request.remote_addr}", 3600, 30)])


def limpar_limites():
    """Zera os limites (usado pelos testes)."""
    from app import app
    try:
        with app.app_context():
            LimiteUso.query.delete()
            db.session.commit()
    except Exception:  # tabela ainda não criada: não há o que limpar
        pass