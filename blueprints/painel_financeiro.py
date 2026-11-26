"""Financeiro do painel: contas a receber e a pagar, fluxo de caixa e lucro."""
from datetime import date, timedelta

from flask import flash, redirect, render_template, request, url_for

import avisos
import financeiro
import historico
import lucro
import relatorios
from b2b import config, hoje, salvar_config
from models import Lancamento, mudar_status, db
from blueprints.admin import admin, _baixar, login_required


def _datas(padrao_de, padrao_ate):
    try:
        de = date.fromisoformat(request.args["de"]) if request.args.get("de") else padrao_de
        ate = date.fromisoformat(request.args["ate"]) if request.args.get("ate") else padrao_ate
    except ValueError:
        de, ate = padrao_de, padrao_ate
    return (ate, de) if de > ate else (de, ate)


@admin.route("/financeiro", methods=["GET", "POST"])
@login_required
def financeiro_pagina():
    if request.method == "POST":
        f = request.form
        if f.get("acao") == "boletos_config":
            import boleto_efi
            multa, erro = formacao_pct(f.get("boleto_multa"), "a multa", 2)
            juros, erro2 = formacao_pct(f.get("boleto_juros"), "os juros", 10)
            if erro or erro2:
                flash(erro or erro2)
            else:
                import cartao_efi
                boleto_efi.salvar_config_boleto(multa, juros, "boleto_auto" in f)
                cartao_efi.salvar_config_cartao("cartao_efi" in f)
                db.session.commit()
                flash("Configuração do boleto e do cartão salva.")
            return redirect(url_for("admin.financeiro_pagina", tipo="receber"))
        if f.get("acao") == "boletos_atualizar":
            import boleto_efi
            import cartao_efi
            pagos, erros = boleto_efi.atualizar_abertos()
            cartoes, erros_cartao = cartao_efi.atualizar_abertos() if cartao_efi.ligado() else (0, [])
            erros += erros_cartao
            flash(f"{pagos} boleto(s) e {cartoes} cartão(ões) pago(s) baixado(s)." + (" " + erros[0] if erros else ""))
            return redirect(url_for("admin.financeiro_pagina", tipo="receber"))
        if f.get("acao") == "config":
            try:
                dias = int(f.get("bloqueio_atraso_dias") or 0)
            except ValueError:
                dias = -1
            if not 0 <= dias <= 365:
                flash("Informe de 0 a 365 dias (0 = não bloqueia).")
            else:
                salvar_config("bloqueio_atraso_dias", dias)
                db.session.commit()
                flash("Boleto faturado " + (f"bloqueado para quem tem parcela vencida há mais de {dias} dias."
                                            if dias else "sem bloqueio por atraso."))
            return redirect(url_for("admin.financeiro_pagina", tipo="receber"))
        dados, repetir, erro = financeiro.ler_lancamento(f)
        if erro:
            flash(erro)
        else:
            criados = financeiro.criar(dados, repetir, pago="pago" in f)
            db.session.commit()
            flash(f"Conta “{dados['descricao']}” lançada" + (f" em {len(criados)} meses." if len(criados) > 1 else "."))
        return redirect(url_for("admin.financeiro_pagina", tipo=f.get("tipo", "pagar")))
    financeiro.sincronizar()  # pedidos antigos e os que mudaram por fora ganham as parcelas certas
    tipo = request.args.get("tipo") if request.args.get("tipo") in financeiro.CATEGORIAS else "receber"
    situacao = request.args.get("situacao") if request.args.get("situacao") in ("abertas", "vencidas", "pagas") \
        else "abertas"
    de, ate = _datas(hoje().replace(day=1), hoje())
    import boleto_efi
    multa, juros = boleto_efi.multa_juros()
    return render_template("admin/financeiro.html", tipo=tipo, situacao=situacao, de=de, ate=ate,
                           boleto={"ligado": boleto_efi.ligado(), "multa": multa, "juros": juros,
                                   "auto": boleto_efi.automatico(), "cartao": config("cartao_efi", "0") == "1"},
                           lista=financeiro.lista(tipo, situacao, de, ate), resumo=financeiro.resumo(),
                           descricao=financeiro.descricao, categorias=financeiro.CATEGORIAS, formas=financeiro.FORMAS,
                           hoje=hoje(), bloqueio=config("bloqueio_atraso_dias", "0") or "0")


@admin.post("/financeiro/<int:lid>")
@login_required
def lancamento(lid):
    l = db.get_or_404(Lancamento, lid)
    acao, f = request.form.get("acao"), request.form
    voltar = url_for("admin.financeiro_pagina", tipo=l.tipo, situacao=f.get("situacao", "abertas"))
    o = l.order
    if acao == "baixar" and l.pago_em is None:
        erro = financeiro.baixar(l, f)
        if erro:
            flash(erro)
            return redirect(voltar)
        db.session.commit()
        msg = ("Recebimento" if l.tipo == "receber" else "Pagamento") + " registrado."
        if o and o.status == "pendente" and all(x.pago_em for x in o.lancamentos):
            ok, erro = mudar_status(o, "pago")  # a última parcela paga quita o pedido
            if ok:
                historico.registrar(o.id, "Pagamento confirmado pelo financeiro (todas as parcelas recebidas)")
                avisos.pagamento_confirmado(o)
                msg += f" Todas as parcelas recebidas: o pedido #{o.id} passou para pago."
        flash(msg)
    elif acao == "estornar" and l.pago_em is not None:
        l.pago_em, l.valor_pago = None, None
        db.session.commit()
        msg = "Baixa desfeita: a conta voltou para em aberto."
        if o and o.status == "pago":
            ok, _ = mudar_status(o, "pendente")
            if ok:
                historico.registrar(o.id, "Voltou para aguardando pagamento (parcela estornada no financeiro)")
                msg += f" O pedido #{o.id} voltou para aguardando pagamento."
        flash(msg)
    elif acao in ("boleto", "boleto_consultar"):
        import boleto_efi
        try:
            msg = boleto_efi.gerar(l) if acao == "boleto" else boleto_efi.consultar(l)
            if acao == "boleto" and o:
                boleto_efi.enviar_ao_cliente(o)
                msg += " O cliente recebeu o link por e-mail."
        except boleto_efi.ErroBoleto as e:
            db.session.rollback()
            msg = str(e)
        flash(msg)
    elif acao == "editar" and l.pago_em is None:
        try:
            vencimento = date.fromisoformat(f.get("vencimento") or "")
            valor = financeiro._valor(f.get("valor")) if not o and (f.get("valor") or "").strip() else l.valor
        except (ValueError, ArithmeticError):
            flash("Confira o vencimento e o valor.")
            return redirect(voltar)
        if not 0 < valor < 100_000_000:
            flash("O valor precisa ser maior que zero.")
            return redirect(voltar)
        l.vencimento, l.valor = vencimento, valor
        db.session.commit()
        flash("Conta alterada.")
    elif acao == "excluir" and not o:
        db.session.delete(l)
        db.session.commit()
        flash("Conta excluída.")
    elif acao == "excluir":
        flash("As parcelas de pedidos acompanham o pedido: cancele ou exclua o pedido em Pedidos.")
    return redirect(voltar)


@admin.post("/orders/<int:oid>/boletos")
@login_required
def pedido_boletos(oid):
    import boleto_efi
    from models import Order
    o = db.get_or_404(Order, oid)
    financeiro.sincronizar()
    feitos, erros = boleto_efi.gerar_do_pedido(o)
    flash((f"{feitos} boleto(s) gerado(s) e enviado(s) ao cliente." if feitos else "Nenhum boleto gerado.")
          + (" " + erros[0] if erros else ""))
    return redirect(url_for("admin.pedido", oid=oid) + "#boletos")


def formacao_pct(texto, rotulo, maximo):
    import formacao
    from decimal import Decimal
    return formacao.ler_pct(texto, rotulo, Decimal(maximo))


@admin.get("/fluxo-de-caixa")
@login_required
def fluxo_caixa():
    financeiro.sincronizar()
    inicio = hoje().replace(day=1)
    fim = (inicio + timedelta(days=62)).replace(day=1) - timedelta(days=1)  # fim do mês que vem
    de, ate = _datas(inicio, fim)
    linhas, totais = financeiro.fluxo(de, ate)
    vencidas = Lancamento.query.filter(Lancamento.pago_em.is_(None), Lancamento.vencimento < hoje()).count()
    return render_template("admin/fluxo.html", linhas=linhas, totais=totais, de=de, ate=ate, hoje=hoje(),
                           vencidas=vencidas)


def _lucro():
    de, ate, atalho = relatorios.periodo(request.args)
    situacao = request.args.get("situacao") if request.args.get("situacao") in relatorios.SITUACOES else "vendas"
    args = {"situacao": situacao, **({"periodo": atalho} if atalho else {"de": de.isoformat(), "ate": ate.isoformat()})}
    return lucro.relatorio(de, ate, situacao), atalho, args


@admin.get("/lucro")
@login_required
def lucro_pagina():
    r, atalho, args = _lucro()
    return render_template("admin/lucro.html", r=r, atalho=atalho, args=args, periodos=relatorios.PERIODOS,
                           situacoes=relatorios.SITUACOES, aba=request.args.get("aba", "produto"))


@admin.get("/lucro.xlsx")
@login_required
def lucro_planilha():
    r, _, _ = _lucro()
    return _baixar(lucro.planilha(r), f"lucro-{r['de']:%Y%m%d}-{r['ate']:%Y%m%d}.xlsx")


@admin.post("/lucro/preencher")
@login_required
def lucro_preencher():
    de, ate, _ = relatorios.periodo(request.form)
    n = lucro.preencher_custos(de, ate)
    db.session.commit()
    flash(f"{n} item(ns) de pedidos antigos receberam o custo de hoje." if n else
          "Nenhum item sem custo que tenha custo cadastrado agora (inclua o preço de compra nos fornecedores).")
    return redirect(url_for("admin.lucro_pagina", **{k: v for k, v in request.form.items() if k != "_csrf"}))
