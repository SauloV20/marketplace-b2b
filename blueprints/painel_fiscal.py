"""Nota fiscal (NF-e) no painel: configuração da empresa e das regras fiscais, emissão a partir do pedido,
consulta, cancelamento, carta de correção, inutilização e os XMLs do mês para o contador."""
from datetime import date, timedelta

from flask import Response, flash, jsonify, redirect, render_template, request, url_for

import estoque
import fiscal
import frete
import nfe_provedores as prov
from b2b import hoje
from models import NotaEvento, NotaFiscal, Order, PerfilFiscal, db
from blueprints.admin import admin, login_required


@admin.route("/notas")
@login_required
def notas_lista():
    status = request.args.get("status", "")
    q = NotaFiscal.query
    if status in fiscal.STATUS:
        q = q.filter(NotaFiscal.status == status)
    notas = q.order_by(NotaFiscal.id.desc()).limit(300).all()
    inicio = hoje().replace(day=1)
    mes_passado = inicio - timedelta(days=1)
    e = fiscal.emitente()
    return render_template("admin/notas.html", notas=notas, status=status, situacoes=fiscal.STATUS, e=e,
                           faltam=fiscal.configurada(), ambientes=fiscal.AMBIENTES,
                           inutilizacoes=NotaEvento.query.filter_by(tipo="inutilizacao").order_by(NotaEvento.id.desc())
                           .limit(20).all(), mes_passado=mes_passado.replace(day=1), fim_mes_passado=mes_passado)


@admin.post("/notas/inutilizar")
@login_required
def notas_inutilizar():
    try:
        inicio, fim = int(request.form.get("inicio") or 0), int(request.form.get("fim") or request.form.get("inicio") or 0)
    except ValueError:
        inicio = fim = 0
    _, msg = fiscal.inutilizar(inicio, fim, request.form.get("justificativa", ""), estoque.autor_atual())
    flash(msg)
    return redirect(url_for("admin.notas_lista"))


@admin.get("/notas/xmls")
@login_required
def notas_xmls():
    try:
        de = date.fromisoformat(request.args.get("de", ""))
        ate = date.fromisoformat(request.args.get("ate", ""))
    except ValueError:
        flash("Escolha o período.")
        return redirect(url_for("admin.notas_lista"))
    conteudo, n = fiscal.xmls_do_periodo(min(de, ate), max(de, ate))
    return Response(conteudo, mimetype="application/zip", headers={
        "Content-Disposition": f"attachment; filename=nfe-xml-{de:%Y%m%d}-{ate:%Y%m%d}.zip", "Cache-Control": "no-store"})


@admin.route("/notas/configuracao", methods=["GET", "POST"])
@login_required
def fiscal_config():
    valores = None
    if request.method == "POST":
        acao, f = request.form.get("acao"), request.form
        if acao == "empresa":
            dados, erro = fiscal.ler_config(f)
            if erro:
                flash(erro)
                valores = f
            else:
                producao = dados["ambiente"] == "1" and fiscal.emitente()["ambiente"] != "1"
                fiscal.gravar_config(dados)
                db.session.commit()
                flash("Dados fiscais salvos." + (" ATENÇÃO: agora as notas são emitidas em PRODUÇÃO, com valor fiscal."
                                                 if producao else ""))
                return redirect(url_for("admin.fiscal_config"))
        elif acao == "perfil_padrao":
            if not fiscal.perfil_padrao():
                fiscal.criar_perfil_padrao()
                db.session.commit()
                flash("Regra fiscal padrão criada. Confira cada campo com o seu contador.")
            return redirect(url_for("admin.fiscal_config") + "#regras")
        elif acao == "perfil":
            dados, erro = fiscal.ler_perfil(f)
            if erro:
                flash(erro)
            else:
                pid = f.get("pid", type=int)
                perfil = db.get_or_404(PerfilFiscal, pid) if pid else PerfilFiscal()
                for campo, valor in dados.items():
                    setattr(perfil, campo, valor)
                if perfil.padrao:
                    PerfilFiscal.query.filter(PerfilFiscal.id != (perfil.id or 0)).update({"padrao": False})
                elif not fiscal.perfil_padrao() or (fiscal.perfil_padrao().id == perfil.id):
                    perfil.padrao = True  # sempre há uma regra padrão
                db.session.add(perfil)
                db.session.commit()
                flash(f"Regra fiscal “{perfil.nome}” salva.")
            return redirect(url_for("admin.fiscal_config") + "#regras")
        elif acao == "perfil_excluir":
            perfil = db.get_or_404(PerfilFiscal, f.get("pid", type=int) or 0)
            from models import Product
            if perfil.padrao:
                flash("A regra padrão não pode ser excluída (marque outra como padrão antes).")
            else:
                Product.query.filter_by(perfil_fiscal_id=perfil.id).update({"perfil_fiscal_id": None})
                db.session.delete(perfil)
                db.session.commit()
                flash("Regra fiscal excluída (os produtos dela passam a usar a padrão).")
            return redirect(url_for("admin.fiscal_config") + "#regras")
        elif acao == "status":
            ok, msg = fiscal.status_sefaz()
            flash(msg)
            return redirect(url_for("admin.fiscal_config"))
    p = prov.provedor()
    configurado, falta = p.configurado()
    certificado = p.validade_certificado() if configurado and isinstance(p, prov.ProvedorSefaz) else None
    from models import Product
    sem_ncm = Product.query.filter(Product.active.is_(True), db.or_(Product.ncm == "", Product.ncm.is_(None))).count()
    return render_template("admin/fiscal_config.html", e=valores or fiscal.emitente(), crts=fiscal.CRT,
                           ambientes=fiscal.AMBIENTES, ufs=frete.UFS, perfis=PerfilFiscal.query.order_by(
                               PerfilFiscal.padrao.desc(), PerfilFiscal.nome).all(), situacoes=fiscal.SITUACOES,
                           ipi=fiscal.IPI_CST, pis=fiscal.PIS_CST, provedor=p.nome, configurado=configurado,
                           falta=falta, certificado=certificado, sem_ncm=sem_ncm, hoje=hoje())


@admin.get("/notas/cep/<cep>")
@login_required
def fiscal_cep(cep):
    """Endereço pelo CEP (para preencher os formulários fiscais)."""
    return jsonify(fiscal.consultar_cep(cep) or {})


@admin.route("/orders/<int:oid>/nfe", methods=["GET", "POST"])
@login_required
def pedido_nfe(oid):
    o = db.get_or_404(Order, oid)
    valida = fiscal.nota_valida(o)
    if valida:
        return redirect(url_for("admin.nota", nid=valida.id))
    if request.method == "POST":
        ok, msg, avisos = fiscal.emitir(o, request.form, estoque.autor_atual())
        for a in avisos:
            flash(a)
        flash(msg)
        nota = fiscal.nota_valida(o) or fiscal.nota_aberta(o)
        if ok and nota:
            return redirect(url_for("admin.nota", nid=nota.id))
        form = request.form
    else:
        form = fiscal.preparar(o)
    dados, avisos, erros = fiscal.montar(o, form)
    return render_template("admin/pedido_nfe.html", o=o, f=form, dados=dados, avisos=avisos, erros=erros,
                           aberta=fiscal.nota_aberta(o), e=fiscal.emitente(), indicadores=fiscal.INDICADOR_IE,
                           fretes=fiscal.MOD_FRETE, ufs=frete.UFS, ambientes=fiscal.AMBIENTES,
                           situacoes=fiscal.SITUACOES)


@admin.route("/notas/<int:nid>", methods=["GET", "POST"])
@login_required
def nota(nid):
    n = db.get_or_404(NotaFiscal, nid)
    if request.method == "POST":
        acao, autor = request.form.get("acao"), estoque.autor_atual()
        if acao == "consultar":
            _, msg = fiscal.consultar(n)
        elif acao == "cancelar":
            _, msg = fiscal.cancelar(n, request.form.get("justificativa", ""), autor)
        elif acao == "correcao":
            _, msg = fiscal.carta_correcao(n, request.form.get("texto", ""), autor)
        elif acao == "email":
            _, msg = fiscal.enviar_ao_cliente(n)
        else:
            msg = "Ação desconhecida."
        flash(msg)
        return redirect(url_for("admin.nota", nid=nid))
    return render_template("admin/nota.html", n=n, situacoes=fiscal.STATUS, ambientes=fiscal.AMBIENTES)


@admin.get("/notas/<int:nid>/xml")
@login_required
def nota_xml(nid):
    n = db.get_or_404(NotaFiscal, nid)
    if not n.xml:
        flash("Esta nota não tem XML guardado.")
        return redirect(url_for("admin.nota", nid=nid))
    return Response(n.xml, mimetype="application/xml", headers={
        "Content-Disposition": f"attachment; filename=nfe-{n.numero}-{n.chave or 'sem-chave'}.xml",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@admin.get("/notas/<int:nid>/danfe")
@login_required
def nota_danfe(nid):
    n = db.get_or_404(NotaFiscal, nid)
    if n.status not in ("autorizada", "cancelada") or not n.xml:
        flash("O DANFE só existe para nota autorizada.")
        return redirect(url_for("admin.nota", nid=nid))
    try:
        pdf = fiscal.danfe_pdf(n.xml)
    except Exception as e:
        flash(f"Não foi possível gerar o DANFE ({e.__class__.__name__}). Baixe o XML.")
        return redirect(url_for("admin.nota", nid=nid))
    return Response(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": f"inline; filename=danfe-nfe-{n.numero}.pdf", "Cache-Control": "no-store"})


@admin.get("/notas/eventos/<int:eid>/xml")
@login_required
def nota_evento_xml(eid):
    ev = db.get_or_404(NotaEvento, eid)
    if not ev.xml:
        flash("Este evento não tem XML (foi recusado).")
        return redirect(url_for("admin.notas_lista"))
    return Response(ev.xml, mimetype="application/xml", headers={
        "Content-Disposition": f"attachment; filename={ev.tipo}-{ev.id}.xml", "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store"})
