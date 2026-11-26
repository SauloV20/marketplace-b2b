"""Estoque do painel: posição, inventário (contagem), histórico de movimentos (kardex) e ajuste de cada produto."""
from decimal import Decimal

from flask import flash, redirect, render_template, request, url_for

import avise_me
import estoque
from models import Category, Product, db
from blueprints.admin import admin, login_required


@admin.route("/estoque", methods=["GET", "POST"])
@login_required
def estoque_lista():
    q, cat, filtro = request.args.get("q", "").strip(), request.args.get("cat", type=int), request.args.get("filtro", "")
    query = Product.query
    if q:
        query = query.filter(db.or_(Product.name.ilike(f"%{q}%"), Product.codigo.ilike(f"%{q}%")))
    if cat:
        query = query.filter_by(category_id=cat)
    produtos = query.order_by(Product.name).all()
    if filtro == "baixo":
        produtos = [p for p in produtos if p.estoque_alerta and p.stock <= p.estoque_alerta]
    elif filtro == "zerado":
        produtos = [p for p in produtos if p.stock <= 0]
    if request.method == "POST":  # inventário: grava só o que foi contado e está diferente
        contados, erro = estoque.ler_inventario(request.form, produtos)
        if erro:
            flash(erro)
        elif not contados:
            flash("Nenhuma diferença: o que foi contado é igual ao estoque do sistema.")
        else:
            obs = " ".join((request.form.get("obs") or "").split())[:200] or "Contagem de inventário"
            for p, contado in contados.items():
                estoque.ajustar(p, contado, "inventario", obs)
            db.session.commit()
            avisados = avise_me.disparar()
            flash(f"Inventário gravado: {len(contados)} produto(s) acertado(s)."
                  + (f" {avisados} cliente(s) avisado(s) de que o produto voltou." if avisados else ""))
        return redirect(url_for("admin.estoque_lista", **request.args))
    custos = {p.id: estoque.custo_atual(p) for p in produtos}
    valor = sum((Decimal(max(p.stock, 0)) * custos[p.id] for p in produtos if custos[p.id] is not None), Decimal(0))
    return render_template("admin/estoque.html", produtos=produtos, q=q, cat=cat, filtro=filtro, custos=custos,
                           valor=valor, ultimos=estoque.ultimos_movimentos([p.id for p in produtos]),
                           categorias=Category.query.order_by(Category.name).all())


@admin.route("/estoque/<int:pid>", methods=["GET", "POST"])
@login_required
def estoque_produto(pid):
    p = db.get_or_404(Product, pid)
    if request.method == "POST":
        modo, f = request.form.get("modo"), request.form
        try:
            qtd = int(f.get("quantidade") or "")
        except ValueError:
            qtd = -1
        novo = {"entrada": p.stock + qtd, "saida": p.stock - qtd, "definir": qtd}.get(modo)
        if novo is None or not 0 <= qtd <= 10_000_000:
            flash("Informe a quantidade (número inteiro) e se é entrada, saída ou o estoque certo.")
        elif novo < 0:
            flash(f"A saída de {qtd} un passa do estoque atual ({p.stock} un).")
        elif novo == p.stock:
            flash("O estoque não mudou.")
        else:
            motivo = f.get("motivo") if f.get("motivo") in estoque.MOTIVOS_AJUSTE else "Outro"
            obs = " ".join((f.get("obs") or "").split())
            estoque.ajustar(p, novo, "ajuste", motivo + (f" · {obs}" if obs else ""))
            db.session.commit()
            avise_me.disparar()
            flash(f"Estoque de {p.name}: agora {novo} un.")
        return redirect(url_for("admin.estoque_produto", pid=pid))
    return render_template("admin/estoque_produto.html", p=p, movimentos=estoque.historico(pid),
                           tipos=estoque.TIPOS, motivos=estoque.MOTIVOS_AJUSTE, custo=estoque.custo_atual(p))
