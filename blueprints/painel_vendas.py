"""Vendas e relatórios do painel: o resumo de vendas e o relatório com Excel."""
from datetime import datetime, timedelta

from flask import flash, render_template, request
from sqlalchemy import func
import armazenamento
import dois_fatores
from email_utils import provedor_email
import relatorios
from blueprints.shop import METODOS
from models import Order, OrderItem, Product, User, agora_utc, db
from blueprints.admin import admin, _baixar, login_required


@admin.route("/")
@login_required
def dashboard():
    since = agora_utc() - timedelta(days=29)
    day = func.date(Order.created_at)
    pago = Order.status == "pago"
    daily = (db.session.query(day, func.sum(Order.total)).filter(Order.created_at >= since, pago)
             .group_by(day).order_by(day).all())
    top = (db.session.query(Product.name, func.sum(OrderItem.quantity))
           .join(OrderItem, OrderItem.product_id == Product.id)
           .join(Order, Order.id == OrderItem.order_id).filter(pago)
           .group_by(Product.id, Product.name)
           .order_by(func.sum(OrderItem.quantity).desc()).limit(5).all())
    kpis = {
        "revenue": db.session.query(func.coalesce(func.sum(Order.total), 0))
        .filter(pago).scalar(),
        "orders": Order.query.filter(pago).count(),
        "pending": Order.query.filter_by(status="pendente").count(),
        "products": Product.query.filter_by(active=True).count(),
        "orcamentos": Order.query.filter_by(status="orcamento").count(),
        "empresas": User.query.filter_by(is_admin=False, tipo="pj", empresa_aprovada=False, empresa_id=None).count(),
    }
    pct = armazenamento.percentual()
    if pct is not None and pct >= 80:
        flash(f"Atenção: o banco de dados está com {pct}% do espaço do plano ocupado. Veja em Backup e espaço "
              "o que mais ocupa e comprima as fotos.")
    if dois_fatores.desligado_pela_variavel():
        flash("Atenção: a verificação em duas etapas está desligada pela variável ADMIN_2FA_DESLIGADO no Render. "
              "Depois de resolver o acesso, apague essa variável.")
    if not provedor_email():
        flash("Atenção: nenhum serviço de e-mail está configurado, então os clientes não recebem a confirmação "
              "de cadastro nem a recuperação de senha. Veja em Minha conta > E-mail do site.")
    from models import Configuracao
    ultimo = db.session.get(Configuracao, "ultimo_backup_em")
    lembrar = not ultimo or (agora_utc() - datetime.fromisoformat(ultimo.valor)).days >= 7
    estoque_baixo = (Product.query.filter(Product.active.is_(True), Product.estoque_alerta > 0,
                                          Product.stock <= Product.estoque_alerta)
                     .order_by(Product.stock, Product.name).all())
    return render_template("admin/dashboard.html", kpis=kpis, lembrar_backup=lembrar, estoque_baixo=estoque_baixo,
                           days=[d.strftime("%d/%m") if hasattr(d, "strftime") else str(d) for d, _ in daily],
                           totals=[float(t) for _, t in daily],
                           top_names=[n for n, _ in top], top_qty=[int(q) for _, q in top])


# ---------- relatório de vendas ----------
def _relatorio():
    de, ate, atalho = relatorios.periodo(request.args)
    situacao = request.args.get("situacao", "vendas")
    situacao = situacao if situacao in relatorios.SITUACOES else "vendas"
    args = {"situacao": situacao, **({"periodo": atalho} if atalho else {"de": de.isoformat(), "ate": ate.isoformat()})}
    return relatorios.montar(de, ate, situacao), atalho, args


@admin.get("/relatorios")
@login_required
def relatorios_pagina():
    r, atalho, args = _relatorio()
    return render_template("admin/relatorios.html", r=r, atalho=atalho, args=args, periodos=relatorios.PERIODOS,
                           situacoes=relatorios.SITUACOES, metodos=METODOS, nome=relatorios.nome_cliente,
                           dias_inativo=relatorios.DIAS_INATIVO)


@admin.get("/relatorios.xlsx")
@login_required
def relatorios_planilha():
    r, _, _ = _relatorio()
    return _baixar(relatorios.planilha(r, METODOS), f"vendas-{r['de']:%Y-%m-%d}-a-{r['ate']:%Y-%m-%d}.xlsx")
