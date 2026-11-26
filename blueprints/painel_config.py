"""Configurações do painel (só acesso total): regras de venda, frete, equipe e backup."""
import secrets
from decimal import Decimal, InvalidOperation

from flask import Response, abort, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash
from b2b import cnpj_valido, pedido_minimo, salvar_config
import armazenamento
import backup
import frete
from formato import tamanho
from email_utils import enviar_email_detalhado, link_externo
import lgpd
import permissoes
from models import RegraFrete, User, agora_utc, db
from security import acertou, bloqueado, falhou
from blueprints.admin import admin, login_required


@admin.route("/regras", methods=["GET", "POST"])
@login_required
def regras():
    if request.method == "POST":
        try:
            minimo = Decimal(request.form.get("pedido_minimo", "0").replace(",", ".").strip() or "0")
        except InvalidOperation:
            minimo = Decimal(-1)
        empresa = {k: " ".join(request.form.get(k, "").split())[:200] for k in lgpd.CAMPOS_EMPRESA}
        empresa["empresa_cnpj"] = "".join(ch for ch in empresa["empresa_cnpj"] if ch.isdigit())
        if not minimo.is_finite() or not 0 <= minimo < 100_000_000:
            flash("Informe o pedido mínimo em reais (0 para não ter mínimo).")
        elif empresa["empresa_cnpj"] and not cnpj_valido(empresa["empresa_cnpj"]):
            flash("O CNPJ da empresa é inválido.")
        elif empresa["empresa_email_privacidade"] and "@" not in empresa["empresa_email_privacidade"]:
            flash("Informe um e-mail válido para assuntos de privacidade.")
        else:
            salvar_config("pedido_minimo", minimo.quantize(Decimal("0.01")))
            for k, v in empresa.items():
                salvar_config(k, v)
            db.session.commit()
            flash("Regras de venda e dados da empresa salvos.")
        return redirect(url_for("admin.regras"))
    return render_template("admin/regras.html", minimo=pedido_minimo(), e=lgpd.dados_empresa(),
                           campos=lgpd.CAMPOS_EMPRESA)


# ---------- frete ----------
@admin.route("/frete", methods=["GET", "POST"])
@login_required
def frete_lista():
    import melhor_envio
    teste = None
    if request.method == "POST":
        if request.form.get("acao") == "melhor_envio":
            dados, erro = melhor_envio.ler_ajustes(request.form)
            if erro:
                flash(erro)
            else:
                melhor_envio.gravar_ajustes(dados)
                db.session.commit()
                melhor_envio.limpar_cache()
                flash("Melhor Envio salvo." + ("" if melhor_envio.tem_token() or not dados["me_ativo"] else
                      " Falta colocar o MELHOR_ENVIO_TOKEN nas variáveis do Render para a cotação funcionar."))
            return redirect(url_for("admin.frete_lista") + "#melhor-envio")
        if request.form.get("acao") == "simular":
            try:
                valor = Decimal((request.form.get("sim_valor") or "0").replace(".", "").replace(",", "."))
                peso = Decimal((request.form.get("sim_peso") or "0").replace(",", "."))
            except InvalidOperation:
                valor, peso = Decimal(0), Decimal(0)
            cep = frete.normalizar_cep(request.form.get("sim_cep", ""))
            teste = {"cep": frete.formatar_cep(cep) if cep else request.form.get("sim_cep", ""), "valor": valor,
                     "peso": peso, "uf": frete.uf_do_cep(cep) if cep else None,
                     "valido": bool(cep)}
            item = {"pid": 0, "qtd": 1, "preco": valor, "altura": None, "largura": None, "comprimento": None,
                    "peso": peso if peso > 0 else None}  # um volume com a caixa padrão e o peso informado
            teste["opcoes"] = frete.opcoes(cep, valor, peso, [item]) if cep else []
        else:
            dados, erro = frete.ler_regra(request.form)
            if erro:
                flash(erro)
            else:
                rid = request.form.get("rid", type=int)
                r = db.get_or_404(RegraFrete, rid) if rid else RegraFrete()
                if not rid:
                    dados["ativo"] = True
                for campo, valor in dados.items():
                    setattr(r, campo, valor)
                db.session.add(r)
                db.session.commit()
                flash(f"Regra “{r.nome}” salva.")
            return redirect(url_for("admin.frete_lista"))
    regras = RegraFrete.query.order_by(RegraFrete.ordem, RegraFrete.id).all()
    return render_template("admin/frete.html", regras=regras, teste=teste, fmt_cep=frete.formatar_cep, ufs=frete.UFS,
                           me=melhor_envio.ajustes(), me_token=melhor_envio.tem_token(),
                           me_ligado=melhor_envio.ligado(), me_servicos=melhor_envio.SERVICOS,
                           me_sandbox=bool(current_app.config.get("MELHOR_ENVIO_SANDBOX")))


@admin.post("/frete/<int:rid>/excluir")
@login_required
def frete_excluir(rid):
    db.session.delete(db.get_or_404(RegraFrete, rid))
    db.session.commit()
    flash("Regra de frete excluída.")
    return redirect(url_for("admin.frete_lista"))


# ---------- equipe do painel (vários administradores) ----------
VALIDADE_CONVITE = 7 * 86400


def _convite_token(u):
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="convite-admin").dumps(
        {"id": u.id, "h": u.password_hash[-16:]})


def _enviar_convite(u, quem):
    link = link_externo(url_for("admin.convite", token=_convite_token(u)))
    ok, _ = enviar_email_detalhado(
        u.email, "Convite para o painel do Marketplace B2B",
        f"Olá, {u.name}!\n\n{quem} convidou você para o painel de administração da loja Marketplace B2B.\n"
        f"Acesso: {permissoes.texto(u)}.\n\nCrie sua senha neste link (vale por 7 dias):\n{link}\n\n"
        "Se você não esperava este convite, ignore este e-mail.")
    return ok, link


def _donos():
    return [u for u in User.query.filter_by(is_admin=True).all() if permissoes.total(u)]


@admin.route("/equipe", methods=["GET", "POST"])
@login_required
def equipe():
    eu = db.session.get(User, session["admin_id"])
    link_convite = None
    if request.method == "POST":
        f, acao = request.form, request.form.get("acao", "")
        from security import pode_enviar_email
        if acao == "convidar":
            nome, email = " ".join(f.get("nome", "").split())[:120], f.get("email", "").strip().lower()[:120]
            if not nome or "@" not in email:
                flash("Informe o nome e um e-mail válido.")
            elif User.query.filter_by(email=email).first():
                flash("Esse e-mail já tem conta na loja (de cliente ou do painel). Use outro e-mail.")
            elif not pode_enviar_email(email):
                flash("Aguarde um minuto antes de enviar outro convite (e no máximo 10 por hora).")
            else:
                u = User(name=nome, email=email, is_admin=True, email_verified=True, permissoes=permissoes.ler(f) or "nenhuma")
                u.set_password(secrets.token_urlsafe(32))  # ninguém sabe: a pessoa cria a dela pelo convite
                db.session.add(u)
                db.session.commit()
                ok, link = _enviar_convite(u, eu.name)
                if ok:
                    flash(f"Convite enviado para {email}. A pessoa cria a senha pelo link do e-mail (vale 7 dias).")
                else:
                    link_convite = link
                    flash(f"{nome} foi cadastrado, mas o e-mail não saiu. Copie o link abaixo e envie você mesmo.")
                if not link_convite:
                    return redirect(url_for("admin.equipe"))
        else:
            u = db.get_or_404(User, f.get("uid", type=int) or 0)
            if not u.is_admin:
                abort(404)
            if u.id == eu.id and acao in ("salvar", "remover"):
                flash("Você não pode mudar o seu próprio acesso nem se remover. Peça para outro administrador.")
            elif acao == "salvar":
                novas = permissoes.ler(f) or "nenhuma"
                if permissoes.total(u) and novas != permissoes.TUDO and len(_donos()) <= 1:
                    flash("Precisa sobrar pelo menos um administrador com acesso total.")
                else:
                    u.permissoes = novas
                    db.session.commit()
                    flash(f"Acesso de {u.name} atualizado: {permissoes.texto(u)}.")
            elif acao == "remover":
                if permissoes.total(u) and len(_donos()) <= 1:
                    flash("Precisa sobrar pelo menos um administrador com acesso total.")
                else:
                    nome = u.name
                    db.session.delete(u)
                    db.session.commit()
                    flash(f"{nome} não tem mais acesso ao painel.")
            elif acao == "reenviar" and not pode_enviar_email(u.email):
                flash("Aguarde um minuto antes de reenviar o convite.")
            elif acao == "reenviar":
                ok, link = _enviar_convite(u, eu.name)
                if ok:
                    flash(f"Convite reenviado para {u.email}.")
                else:
                    link_convite = link
                    flash("O e-mail não saiu. Copie o link abaixo e envie você mesmo.")
            if not link_convite:
                return redirect(url_for("admin.equipe"))
    equipe_lista = User.query.filter_by(is_admin=True).order_by(User.name).all()
    return render_template("admin/equipe.html", equipe=equipe_lista, eu=eu, areas=permissoes.AREAS,
                           areas_de=permissoes.areas, total=permissoes.total, texto=permissoes.texto,
                           link_convite=link_convite)


@admin.route("/convite/<token>", methods=["GET", "POST"])
def convite(token):
    """O administrador convidado cria a própria senha (o link vale 7 dias e uma vez só)."""
    from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
    try:
        dados = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="convite-admin").loads(
            token, max_age=VALIDADE_CONVITE)
    except (BadSignature, SignatureExpired):
        dados = None
    u = db.session.get(User, dados["id"]) if dados else None
    if not u or not u.is_admin or u.password_hash[-16:] != dados.get("h"):
        flash("Convite inválido, vencido ou já usado. Peça um novo a quem convidou você.")
        return redirect(url_for("admin.login"))
    if request.method == "POST":
        nova = request.form.get("nova", "")
        if not 12 <= len(nova) <= 128:
            flash("A senha deve ter entre 12 e 128 caracteres.")
        elif nova != request.form.get("nova2", ""):
            flash("As senhas não coincidem.")
        else:
            u.set_password(nova)
            db.session.commit()
            session.clear()
            flash("Senha criada. Entre com o seu e-mail e a nova senha.")
            return redirect(url_for("admin.login"))
    return render_template("admin/convite.html", u=u)


# ---------- backup ----------
@admin.get("/backup")
@login_required
def backup_pagina():
    from models import Configuracao
    ultimo = db.session.get(Configuracao, "ultimo_backup")
    depois_foto, depois_banner = request.args.get("df", 0, type=int), request.args.get("db", 0, type=int)
    import backup_auto
    import exemplos
    return render_template("admin/backup.html", ultimo=ultimo.valor if ultimo else None, uso=armazenamento.uso(),
                           exemplos=exemplos.resumo(),
                           auto=backup_auto.situacao(),
                           a_comprimir=armazenamento.a_comprimir(depois_foto, depois_banner),
                           depois_foto=depois_foto, depois_banner=depois_banner)


@admin.post("/backup/automatico")
@login_required
def backup_automatico():
    import backup_auto
    emails = [e.strip() for e in request.form.get("backup_email", "").replace(";", ",").split(",") if e.strip()]
    if any("@" not in e or len(e) > 120 for e in emails) or len(emails) > 3:
        flash("Informe até 3 e-mails válidos, separados por vírgula.")
        return redirect(url_for("admin.backup_pagina"))
    salvar_config("backup_email", ", ".join(emails))
    db.session.commit()
    if request.form.get("acao") == "enviar":
        _, msg = backup_auto.enviar(forcar=True)
        flash(msg)
    else:
        flash("E-mail do backup semanal salvo.")
    return redirect(url_for("admin.backup_pagina"))


@admin.post("/backup/comprimir")
@login_required
def comprimir_fotos():
    """Comprime um lote das fotos e banners grandes que foram enviados antes da compressão automática."""
    mudaram, economia, df, db_ = armazenamento.comprimir_existentes(request.form.get("df", 0, type=int),
                                                                     request.form.get("db", 0, type=int))
    resta = armazenamento.a_comprimir(df, db_)
    flash(f"{mudaram} imagem(ns) comprimida(s), {tamanho(economia)} a menos."
          + (f" Ainda há {resta}: clique de novo para continuar." if resta else " Pronto: todas as imagens grandes foram vistas."))
    return redirect(url_for("admin.backup_pagina", df=df, db=db_) + "#espaco" if resta else
                    url_for("admin.backup_pagina") + "#espaco")


@admin.post("/backup/baixar")
@login_required
def backup_baixar():
    """O arquivo sai sempre protegido por senha: a BACKUP_SENHA do Render ou uma criada na hora."""
    senha = current_app.config.get("BACKUP_SENHA", "")
    if not senha:
        senha = request.form.get("senha_arquivo", "")
        if len(senha) < 10:
            flash("Crie uma senha de pelo menos 10 caracteres para proteger o arquivo do backup.")
            return redirect(url_for("admin.backup_pagina"))
        if senha != request.form.get("senha_arquivo2", ""):
            flash("As duas senhas do backup não são iguais. Digite de novo.")
            return redirect(url_for("admin.backup_pagina"))
    conteudo, resumo = backup.gerar()
    arquivo = backup.cifrar(conteudo, senha)
    salvar_config("ultimo_backup", agora_utc().strftime("%d/%m/%Y %H:%M") + " (UTC)")
    salvar_config("ultimo_backup_em", agora_utc().isoformat(timespec="seconds"))
    db.session.commit()
    nome = f"backup-marketplace-{agora_utc():%Y%m%d-%H%M}.json.gz.cifrado"
    current_app.logger.info("Backup baixado: %s", resumo)
    return Response(arquivo, mimetype="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename={nome}", "Cache-Control": "no-store"})


def _confirmacao_com_senha(user, f, palavra):
    """Ações que apagam dados: a palavra digitada e a senha do administrador (com limite de tentativas).
    Devolve a mensagem de erro, ou None se pode seguir."""
    if f.get("confirmacao", "").strip().upper() != palavra:
        return f"Digite {palavra} para confirmar."
    if bloqueado(user.email):
        return "Muitas tentativas. Aguarde 10 minutos e tente de novo."
    if not check_password_hash(user.password_hash, f.get("senha", "")):
        falhou(user.email)
        return "Senha incorreta. Nada foi alterado."
    return None


@admin.post("/backup/exemplos")
@login_required
def limpar_exemplos():
    """Tira os dados de exemplo (pedidos de "Cliente demo" e produtos de amostra) antes de abrir a loja."""
    import exemplos
    user = db.session.get(User, session["admin_id"])
    f = request.form
    voltar = redirect(url_for("admin.backup_pagina") + "#exemplos")
    if not exemplos.backup_recente():
        flash("Baixe um backup antes (no topo desta página). A limpeza fica liberada por 24 horas depois do backup.")
    elif erro := _confirmacao_com_senha(user, f, "LIMPAR"):
        flash(erro)
    else:
        pedidos, produtos = exemplos.limpar(f.get("produtos") == "on")
        current_app.logger.info("Dados de exemplo removidos por %s: %s pedidos, %s produtos desativados",
                                user.email, pedidos, produtos)
        flash(f"Pronto: {pedidos} pedido(s) de exemplo apagado(s)"
              + (f" e {produtos} produto(s) de amostra desativado(s) (reative em Produtos, se quiser)." if produtos
                 else "."))
    return voltar


@admin.post("/backup/restaurar")
@login_required
def backup_restaurar():
    # o limite maior (backup.MAX_ARQUIVO) é aplicado em security.checar_csrf, antes de o formulário ser lido
    user = db.session.get(User, session["admin_id"])
    f = request.form
    if erro := _confirmacao_com_senha(user, f, "RESTAURAR"):
        flash(erro)
    elif not request.files.get("arquivo") or not request.files["arquivo"].filename:
        flash("Escolha o arquivo de backup.")
    else:
        try:
            conteudo = backup.ler(request.files["arquivo"].read(), f.get("senha_backup", ""))
            feito = backup.restaurar(conteudo)
        except backup.ErroBackup as e:
            flash(str(e))
        except Exception:
            current_app.logger.exception("Falha ao restaurar backup")
            flash("Não foi possível restaurar esse backup. Nada foi alterado.")
        else:
            acertou(user.email)
            session.clear()  # as contas foram trocadas pelas do backup: entre de novo
            flash(f"Backup de {conteudo.get('criado_em', '?')} restaurado: {feito.get('product', 0)} produtos, "
                  f"{feito.get('user', 0)} contas e {feito.get('order', 0)} pedidos. Entre de novo no painel."
                  + (" Era um backup só de dados: envie de novo as fotos dos produtos e os anexos dos pedidos."
                     if conteudo.get("parcial") else ""))
            return redirect(url_for("admin.login"))
    return redirect(url_for("admin.backup_pagina"))
