"""Cupons de desconto, perguntas da ajuda (FAQ), carrinhos abandonados e avaliações de produto."""
from flask import current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import func
import ajuda
import cupons
import produtos
from models import Cupom, Pergunta, Order, db
from blueprints.admin import admin, login_required


# ---------- ajuda (perguntas frequentes) ----------
@admin.route("/ajuda", methods=["GET", "POST"])
@login_required
def ajuda_lista():
    if request.method == "POST":
        acao, f = request.form.get("acao"), request.form
        if acao == "padrao":
            if not Pergunta.query.count():
                for i, (p, r) in enumerate(ajuda.PADRAO, start=1):
                    db.session.add(Pergunta(pergunta=p, resposta=r, ordem=i))
                db.session.commit()
                flash("Perguntas padrão carregadas. Agora você pode editar cada uma.")
        else:
            pergunta = " ".join(f.get("pergunta", "").split())[:200]
            resposta = f.get("resposta", "").strip()[:3000]
            if not pergunta or not resposta:
                flash("Escreva a pergunta e a resposta.")
            else:
                pid = f.get("pid", type=int)
                p = db.get_or_404(Pergunta, pid) if pid else Pergunta(
                    ordem=(db.session.query(db.func.max(Pergunta.ordem)).scalar() or 0) + 1)
                p.pergunta, p.resposta, p.ativo = pergunta, resposta, "ativo" in f or not pid
                db.session.add(p)
                db.session.commit()
                flash("Pergunta salva.")
        return redirect(url_for("admin.ajuda_lista"))
    return render_template("admin/ajuda.html", perguntas=Pergunta.query.order_by(Pergunta.ordem, Pergunta.id).all(),
                           padrao=ajuda.PADRAO, ia=bool(current_app.config.get("ANTHROPIC_API_KEY")))


@admin.post("/ajuda/<int:pid>/mover")
@login_required
def ajuda_mover(pid):
    lista = Pergunta.query.order_by(Pergunta.ordem, Pergunta.id).all()
    i = next(k for k, x in enumerate(lista) if x.id == pid)
    j = i - 1 if request.form.get("para") == "cima" else i + 1
    if 0 <= j < len(lista):
        lista[i], lista[j] = lista[j], lista[i]
        for k, x in enumerate(lista, start=1):
            x.ordem = k
        db.session.commit()
    return redirect(url_for("admin.ajuda_lista"))


@admin.post("/ajuda/<int:pid>/excluir")
@login_required
def ajuda_excluir(pid):
    db.session.delete(db.get_or_404(Pergunta, pid))
    db.session.commit()
    flash("Pergunta excluída.")
    return redirect(url_for("admin.ajuda_lista"))


# ---------- cupons ----------
@admin.route("/cupons", methods=["GET", "POST"])
@login_required
def cupons_lista():
    if request.method == "POST":
        cid = request.form.get("cid", type=int)
        c = db.get_or_404(Cupom, cid) if cid else Cupom()
        dados, erro = cupons.ler(request.form, cid)
        if erro:
            flash(erro)
        elif cid and c.codigo != dados["codigo"] and cupons.usos(c):
            flash(f"O cupom {c.codigo} já foi usado em pedidos: o código não pode mudar. Crie um cupom novo.")
        else:
            for campo, valor in dados.items():
                setattr(c, campo, valor)
            db.session.add(c)
            db.session.commit()
            flash(f"Cupom {c.codigo} salvo.")
        return redirect(url_for("admin.cupons_lista"))
    lista = Cupom.query.order_by(Cupom.ativo.desc(), Cupom.id.desc()).all()
    usos = dict(db.session.query(Order.cupom, func.count(Order.id))
                .filter(Order.cupom != "", Order.status != "cancelado").group_by(Order.cupom).all())
    descontos = dict(db.session.query(Order.cupom, func.coalesce(func.sum(Order.desconto), 0))
                     .filter(Order.cupom != "", Order.status != "cancelado").group_by(Order.cupom).all())
    return render_template("admin/cupons.html", cupons=lista, usos=usos, descontos=descontos, hoje=cupons.hoje(),
                           descricao=cupons.descricao)


@admin.post("/cupons/<int:cid>/excluir")
@login_required
def cupom_excluir(cid):
    c = db.get_or_404(Cupom, cid)
    if cupons.usos(c):  # os pedidos guardam o código; desativar mantém o histórico
        c.ativo = False
        flash(f"O cupom {c.codigo} já foi usado em pedidos, então foi desativado em vez de excluído.")
    else:
        db.session.delete(c)
        flash(f"Cupom {c.codigo} excluído.")
    db.session.commit()
    return redirect(url_for("admin.cupons_lista"))


# ---------- carrinhos abandonados ----------
@admin.route("/carrinhos", methods=["GET", "POST"])
@login_required
def carrinhos():
    import carrinho_abandonado
    if request.method == "POST":
        if request.form.get("acao") == "enviar":
            r = carrinho_abandonado.enviar_lembretes()
            flash("Lembretes desligados: ligue antes de enviar." if r.get("desligado") else
                  f"{r['enviados']} lembrete(s) enviado(s).")
        else:
            erro = carrinho_abandonado.salvar_configuracao("ligado" in request.form, request.form.get("horas"))
            if erro:
                flash(erro)
            else:
                db.session.commit()
                flash("Configuração dos lembretes salva.")
        return redirect(url_for("admin.carrinhos"))
    return render_template("admin/carrinhos.html", ligado=carrinho_abandonado.ligado(),
                           horas=carrinho_abandonado.horas(), janela=carrinho_abandonado.JANELA_DIAS,
                           tarefa=bool(current_app.config.get("TAREFAS_TOKEN")), **carrinho_abandonado.resumo())


# ---------- avaliações de produto ----------
@admin.route("/avaliacoes", methods=["GET", "POST"])
@login_required
def avaliacoes_lista():
    import avaliacoes
    from models import Avaliacao
    if request.method == "POST":
        a = db.get_or_404(Avaliacao, request.form.get("aid", type=int) or 0)
        if request.form.get("acao") == "excluir":
            db.session.delete(a)
            db.session.commit()
            flash("Avaliação excluída.")
        else:
            flash(avaliacoes.moderar(a, request.form.get("acao", ""), request.form.get("resposta")))
        return redirect(url_for("admin.avaliacoes_lista", status=request.args.get("status", "pendente")))
    filtro = request.args.get("status", "pendente")
    return render_template("admin/avaliacoes.html", lista=avaliacoes.lista(filtro), filtro=filtro,
                           status=avaliacoes.STATUS, link_produto=produtos.link)


# ---------- métricas (Google Analytics) ----------
@admin.route("/metricas", methods=["GET", "POST"])
@login_required
def metricas():
    import analytics
    if request.method == "POST":
        erro = analytics.salvar_id(request.form.get("ga_id"))
        if erro:
            flash(erro)
        else:
            db.session.commit()
            flash("Google Analytics " + ("ligado." if analytics.medicao_id() else "desligado."))
        return redirect(url_for("admin.metricas"))
    return render_template("admin/metricas.html", ga_id=analytics.medicao_id())
