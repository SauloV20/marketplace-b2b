"""Compras do painel: sugestão de reposição, pedidos de compra ao fornecedor e recebimento da mercadoria."""
from urllib.parse import quote

from flask import flash, redirect, render_template, request, url_for

import avise_me
import compras
import estoque
from editar_pedido import _achar_produto
from models import Compra, Fornecedor, Product, agora_utc, db
from blueprints.admin import admin, login_required


@admin.route("/compras", methods=["GET", "POST"])
@login_required
def compras_lista():
    if request.method == "POST":
        forn = db.session.get(Fornecedor, request.form.get("fornecedor_id", type=int) or 0)
        if not forn:
            flash("Escolha o fornecedor.")
            return redirect(url_for("admin.compras_lista"))
        itens = []
        if request.form.get("acao") == "sugestao":  # cria o pedido com as quantidades sugeridas (ajustáveis)
            for chave, valor in request.form.items():
                if chave.startswith("qtd_") and chave[4:].isdigit():
                    try:
                        qtd = int(valor or 0)
                    except ValueError:
                        qtd = 0
                    p = db.session.get(Product, int(chave[4:]))
                    if p and 0 < qtd <= 10_000_000:
                        itens.append((p, qtd))
            if not itens:
                flash("Nenhuma quantidade para comprar.")
                return redirect(url_for("admin.compras_lista"))
        c = compras.nova(forn, itens, estoque.autor_atual())
        db.session.commit()
        flash(f"Pedido de compra #{c.id} criado para {forn.nome}. Confira e envie ao fornecedor.")
        return redirect(url_for("admin.compra", cid=c.id))
    status = request.args.get("status", "abertas")
    q = Compra.query
    if status == "abertas":
        q = q.filter(Compra.status.in_(compras.ABERTAS))
    elif status in compras.STATUS:
        q = q.filter(Compra.status == status)
    lista = q.order_by(Compra.id.desc()).limit(200).all()
    try:
        cobertura = min(max(int(request.args.get("meses") or 1), 1), 6)
    except ValueError:
        cobertura = 1
    return render_template("admin/compras.html", lista=lista, status=status, situacoes=compras.STATUS,
                           sugestao=compras.sugestao(cobertura), cobertura=cobertura, totais=compras.totais,
                           fornecedores=Fornecedor.query.filter_by(ativo=True).order_by(Fornecedor.nome).all())


@admin.route("/compras/<int:cid>", methods=["GET", "POST"])
@login_required
def compra(cid):
    c = db.get_or_404(Compra, cid)
    if request.method == "POST":
        acao, f = request.form.get("acao"), request.form
        if acao in ("salvar", "enviar", "incluir") and c.status not in compras.ABERTAS:
            flash("Esta compra já foi recebida ou cancelada.")
        elif acao in ("salvar", "enviar"):
            erro = compras.ler_rascunho(c, f)
            if erro:
                db.session.rollback()
                flash(erro)
            elif acao == "enviar" and not c.itens:
                db.session.rollback()
                flash("Inclua pelo menos um produto antes de enviar.")
            else:
                if acao == "enviar":
                    c.status, c.enviado_em = "enviado", agora_utc()
                db.session.commit()
                flash("Pedido de compra marcado como enviado ao fornecedor." if acao == "enviar" else "Compra salva.")
        elif acao == "incluir":
            p = _achar_produto(f.get("produto"))
            try:
                qtd = int(f.get("qtd") or 0)
            except ValueError:
                qtd = 0
            if not p:
                flash("Produto não encontrado: use o código, o número (#12) ou o nome exato.")
            elif not 0 < qtd <= 10_000_000:
                flash("Informe a quantidade.")
            else:
                compras.incluir(c, p, qtd)
                db.session.commit()
                flash(f"{p.name} incluído.")
        elif acao == "receber":
            ok, msg, _ = compras.receber(c, f, estoque.autor_atual())
            if ok:
                db.session.commit()
                avisados = avise_me.disparar()
                msg += (f" {avisados} cliente(s) avisado(s) de que o produto voltou." if avisados else "")
            flash(msg)
        elif acao == "cancelar" and c.status in compras.ABERTAS:
            c.status = "cancelado"
            db.session.commit()
            flash(f"Compra #{c.id} cancelada.")
        elif acao == "excluir" and c.status in ("rascunho", "cancelado"):
            db.session.delete(c)
            db.session.commit()
            flash("Pedido de compra excluído.")
            return redirect(url_for("admin.compras_lista"))
        return redirect(url_for("admin.compra", cid=cid))
    texto = compras.texto_para_fornecedor(c)
    fone = "".join(ch for ch in (c.fornecedor.telefone or "") if ch.isdigit())
    if fone and len(fone) <= 11:
        fone = "55" + fone
    pedidos = compras.em_compra(exceto=c.id)
    return render_template("admin/compra.html", c=c, situacoes=compras.STATUS, t=compras.totais(c),
                           recebido=compras.totais(c, recebido=True) if c.status == "recebido" else None,
                           whatsapp=f"https://wa.me/{fone}?text={quote(texto)}" if fone else "",
                           email=(f"mailto:{c.fornecedor.email}?subject={quote(f'Pedido de compra #{c.id}')}"
                                  f"&body={quote(texto)}") if c.fornecedor.email else "",
                           texto=texto, pedidos=pedidos, contas=_contas(c))


def _contas(c):
    from models import Lancamento
    return Lancamento.query.filter_by(compra_id=c.id).order_by(Lancamento.vencimento).all()


@admin.get("/compras/<int:cid>/imprimir")
@login_required
def compra_imprimir(cid):
    c = db.get_or_404(Compra, cid)
    return render_template("admin/compra_imprimir.html", c=c, t=compras.totais(c))


# ---------- notas de compra (NF-e dos fornecedores para o CNPJ da loja) ----------
@admin.route("/notas-de-compra", methods=["GET", "POST"])
@login_required
def notas_compra():
    import notas_compra as nc
    if request.method == "POST":
        if request.form.get("acao") == "buscar":
            _, msg = nc.buscar()
            flash(msg)
        else:
            arquivo = request.files.get("arquivo")
            try:
                nr = nc.importar_arquivo(arquivo.read(nc.MAX_ARQUIVO + 1) if arquivo else b"")
            except nc.ErroNota as e:
                flash(str(e))
            else:
                flash(f"NF-e {nr.numero} de {nr.nome_emitente} importada.")
                return redirect(url_for("admin.nota_compra", rid=nr.id))
        return redirect(url_for("admin.notas_compra"))
    from models import NotaRecebida
    filtro = request.args.get("filtro", "pendentes")
    q = NotaRecebida.query
    if filtro == "pendentes":
        q = q.filter(NotaRecebida.compra_id.is_(None), NotaRecebida.situacao == "autorizada")
    notas = q.order_by(NotaRecebida.emitida_em.is_(None), NotaRecebida.emitida_em.desc(), NotaRecebida.id.desc()).limit(300).all()
    import fiscal
    return render_template("admin/notas_compra.html", notas=notas, filtro=filtro, estado=nc.estado(),
                           cnpj=fiscal.emitente().get("cnpj", ""))


@admin.route("/notas-de-compra/<int:rid>", methods=["GET", "POST"])
@login_required
def nota_compra(rid):
    import notas_compra as nc
    from models import NotaRecebida
    nr = db.get_or_404(NotaRecebida, rid)
    if request.method == "POST":
        if request.form.get("acao") == "ciencia":
            _, msg = nc.ciencia(nr)
            flash(msg)
        else:
            c, erro = nc.lancar(nr, request.form, estoque.autor_atual())
            if erro:
                flash(erro)
            else:
                flash(f"Compra #{c.id} criada a partir da NF-e {nr.numero}. Confira e use “Receber mercadoria”.")
                return redirect(url_for("admin.compra", cid=c.id) + "#receber")
        return redirect(url_for("admin.nota_compra", rid=rid))
    dados = itens = None
    if nr.tipo == "completa":
        dados = nc.ler_xml(nr.xml)
        itens = nc.sugestoes(nr, dados)
    produtos = Product.query.order_by(Product.name).all() if itens else []
    return render_template("admin/nota_compra.html", nr=nr, d=dados, itens=itens, produtos=produtos)


@admin.get("/notas-de-compra/<int:rid>/xml")
@login_required
def nota_compra_xml(rid):
    from flask import Response
    from models import NotaRecebida
    nr = db.get_or_404(NotaRecebida, rid)
    if not nr.xml:
        flash("O XML desta nota ainda não foi baixado.")
        return redirect(url_for("admin.nota_compra", rid=rid))
    return Response(nr.xml, mimetype="application/xml", headers={
        "Content-Disposition": f"attachment; filename=nfe-compra-{nr.chave}.xml", "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store"})


@admin.get("/notas-de-compra/<int:rid>/danfe")
@login_required
def nota_compra_danfe(rid):
    from flask import Response
    import fiscal
    from models import NotaRecebida
    nr = db.get_or_404(NotaRecebida, rid)
    try:
        pdf = fiscal.danfe_pdf(nr.xml)
    except Exception as e:
        flash(f"Não foi possível gerar o DANFE ({e.__class__.__name__}).")
        return redirect(url_for("admin.nota_compra", rid=rid))
    return Response(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": f"inline; filename=danfe-compra-{nr.numero}.pdf", "Cache-Control": "no-store"})
