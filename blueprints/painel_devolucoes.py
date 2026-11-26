"""Devoluções e trocas pedidas pelos clientes: análise, recebimento dos produtos e reembolso."""
from flask import flash, redirect, render_template, request, url_for

import devolucoes
from b2b import METODOS
from models import Devolucao, db
from blueprints.admin import admin, login_required


@admin.route("/devolucoes", methods=["GET", "POST"])
@login_required
def devolucoes_lista():
    if request.method == "POST":
        erro = devolucoes.salvar_prazo(request.form.get("dias"))
        if erro:
            flash(erro)
        else:
            db.session.commit()
            flash("Prazo para pedir devolução ou troca salvo.")
        return redirect(url_for("admin.devolucoes_lista"))
    filtro = request.args.get("status", "abertas")
    return render_template("admin/devolucoes.html", lista=devolucoes.lista(filtro), filtro=filtro,
                           status=devolucoes.STATUS, tipos=devolucoes.TIPOS, itens=devolucoes.itens,
                           dias=devolucoes.prazo_dias())


@admin.route("/devolucoes/<int:did>", methods=["GET", "POST"])
@login_required
def devolucao(did):
    d = db.get_or_404(Devolucao, did)
    if request.method == "POST":
        f, acao = request.form, request.form.get("acao")
        try:
            if acao == "aprovar":
                devolucoes.aprovar(d, f.get("resposta"))
                flash("Aprovada. O cliente recebeu as instruções por e-mail.")
            elif acao == "recusar":
                devolucoes.recusar(d, f.get("resposta"))
                flash("Recusada. O cliente recebeu o motivo por e-mail.")
            elif acao == "receber":
                devolucoes.receber(d, "estoque" in f)
                flash("Produtos marcados como recebidos.")
            elif acao == "concluir":
                devolucoes.concluir(d, f.get("valor", ""), f.get("forma", ""))
                flash("Concluída. O cliente recebeu o aviso por e-mail.")
        except devolucoes.ErroDevolucao as e:
            db.session.rollback()
            flash(str(e))
        return redirect(url_for("admin.devolucao", did=d.id))
    o = d.order
    return render_template("admin/devolucao.html", d=d, o=o, itens=devolucoes.itens(d), status=devolucoes.STATUS,
                           tipos=devolucoes.TIPOS, formas=devolucoes.FORMAS_REEMBOLSO,
                           sugestao=devolucoes.sugestao_reembolso(d), reembolsado=devolucoes.reembolsado(o),
                           metodos=METODOS, cartao=bool(o.cartao_id and o.cartao_situacao in ("paid", "settled")))
