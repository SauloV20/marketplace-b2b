"""Clientes do painel: lista, ficha da empresa (condições, crédito, equipe) e exclusão."""
from decimal import Decimal, InvalidOperation

from flask import abort, flash, redirect, render_template, request, url_for
from b2b import ler_prazos
import avisos
import credito
import lgpd
import receita
from models import Order, User, db
from blueprints.admin import admin, login_required


@admin.route("/clientes")
@login_required
def clientes():
    filtro = request.args.get("f", "")
    query = User.query.filter_by(is_admin=False, empresa_id=None)  # usuários da equipe aparecem dentro da empresa
    if filtro == "analise":
        query = query.filter_by(tipo="pj", empresa_aprovada=False)
    elif filtro in ("pj", "pf"):
        query = query.filter_by(tipo=filtro)
    q = request.args.get("q", "").strip()
    if q:
        query = query.filter(db.or_(User.name.ilike(f"%{q}%"), User.email.ilike(f"%{q}%"),
                                    User.razao_social.ilike(f"%{q}%"), User.documento.ilike(f"%{q}%")))
    return render_template("admin/clientes.html", clientes=query.order_by(User.id.desc()).limit(300).all(),
                           filtro=filtro, q=q)


@admin.route("/clientes/<int:uid>", methods=["GET", "POST"])
@login_required
def cliente(uid):
    membro = db.session.get(User, uid)
    if membro and membro.empresa_id:  # usuário da equipe: a ficha é a da empresa
        return redirect(url_for("admin.cliente", uid=membro.empresa_id))
    c = db.get_or_404(User, uid)
    if c.is_admin:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            desconto = Decimal(f.get("desconto", "0").replace(",", ".").strip() or "0")
        except InvalidOperation:
            desconto = Decimal(-1)
        prazos, erro = ler_prazos(f.get("prazos", ""))
        limite, erro_limite = None, None
        if (f.get("limite_credito") or "").strip():
            t = f.get("limite_credito").replace("R$", "").strip()
            try:
                limite = Decimal(t.replace(".", "").replace(",", ".") if "," in t else t).quantize(Decimal("0.01"))
                if not limite.is_finite() or not 0 <= limite < 1_000_000_000:
                    raise InvalidOperation
            except InvalidOperation:
                erro_limite = "Confira o limite de crédito (ex.: 20.000,00) ou deixe vazio para não ter limite."
        if c.tipo != "pj":
            flash("Condições de empresa valem só para contas com CNPJ.")
        elif not desconto.is_finite() or not 0 <= desconto <= 90:
            flash("O desconto vai de 0 a 90%.")
        elif erro or erro_limite:
            flash(erro or erro_limite)
        else:
            ja_era = c.empresa_aprovada
            c.limite_credito = limite
            c.empresa_aprovada = "aprovada" in f
            c.desconto = desconto.quantize(Decimal("0.01"))
            c.prazos = "; ".join(prazos)
            razao = " ".join(f.get("razao_social", "").split())[:160]
            c.razao_social = razao or c.razao_social
            c.inscricao_estadual = f.get("inscricao_estadual", "").strip()[:30]
            db.session.commit()
            flash(f"Condições de {c.razao_social or c.name} salvas.")
            if c.empresa_aprovada and not ja_era:
                avisos.empresa_aprovada(c)
            return redirect(url_for("admin.cliente", uid=uid))
    pedidos = Order.query.filter_by(user_id=c.id).order_by(Order.id.desc()).limit(20).all()
    return render_template("admin/cliente.html", c=c, pedidos=pedidos, em_aberto=credito.em_aberto(c),
                           disponivel=credito.disponivel(c))


@admin.post("/clientes/<int:uid>/excluir")
@login_required
def excluir_cliente(uid):
    """Apaga a conta do cliente. Os pedidos ficam (com os dados da época da compra), só sem ligação com a conta."""
    c = db.get_or_404(User, uid)
    if c.is_admin:
        abort(404)
    nome = c.razao_social or c.name
    abertos = lgpd.excluir_conta(c)
    if abertos:
        flash(f"{nome} tem {abertos} pedido(s) ou orçamento(s) em aberto. Cancele ou conclua antes de excluir.")
        return redirect(url_for("admin.cliente", uid=uid))
    flash(f"Cliente {nome} excluído. Os pedidos dele continuam em Pedidos.")
    return redirect(url_for("admin.clientes"))


@admin.post("/clientes/<int:uid>/receita")
@login_required
def cliente_receita(uid):
    """Consulta (de novo) o CNPJ na Receita: útil para quem se cadastrou com a API fora do ar."""
    c = db.get_or_404(User, uid)
    if c.is_admin or c.tipo != "pj" or not c.documento:
        abort(404)
    receita.limpar_cache()
    status, dados = receita.consultar_cnpj(c.documento)
    if status == receita.ENCONTRADO:
        c.guardar_receita(dados)
        db.session.commit()
        flash(f"Receita Federal: {dados['razao_social']} está {dados['situacao']}.")
    elif status == receita.NAO_ENCONTRADO:
        c.receita_situacao = "NÃO ENCONTRADO"
        db.session.commit()
        flash("A Receita Federal não encontrou este CNPJ. Não aprove sem conferir.")
    else:
        flash("Não foi possível consultar a Receita agora. Tente de novo em alguns minutos.")
    return redirect(url_for("admin.cliente", uid=uid))
