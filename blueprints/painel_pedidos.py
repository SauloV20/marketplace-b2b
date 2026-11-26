"""Pedidos do painel: lista com busca, situação, orçamentos, envio, documentos, edição, impressão e exclusão."""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import func
from b2b import config, ler_prazos
from formato import brl, hora_br, inicio_do_dia
import avisos
import avise_me
import credito
import cupons
import efi
import editar_pedido
import entrega
import equipe_empresa
import historico
import lgpd
import planilha
from blueprints.shop import METODOS
from models import PedidoArquivo, Order, User, agora_utc, db, excluir_pedido, mudar_status
from blueprints.admin import admin, _baixar, login_required


POR_PAGINA = 50


STATUS_PEDIDO = ("orcamento", "aprovacao", "pendente", "pago", "cancelado")


def _data(texto):
    try:
        return datetime.strptime(texto or "", "%Y-%m-%d").date()
    except ValueError:
        return None


def filtrar_pedidos(args):
    """Aplica status, busca e período da lista de pedidos. Devolve (query, filtros usados)."""
    filtro = args.get("status", "") if args.get("status", "") in STATUS_PEDIDO else ""
    q = " ".join(args.get("q", "").split())[:100]
    de, ate = _data(args.get("de")), _data(args.get("ate"))
    query = Order.query
    if filtro:
        query = query.filter(Order.status == filtro)
    if q:
        termo = f"%{q}%"
        condicoes = [Order.customer_name.ilike(termo), Order.razao_social.ilike(termo),
                     Order.customer_email.ilike(termo), Order.rastreio.ilike(termo), Order.cupom.ilike(termo)]
        numero = q.lstrip("#").strip()
        if numero.isdigit() and len(numero) <= 9:
            condicoes.append(Order.id == int(numero))
        digitos = "".join(ch for ch in q if ch.isdigit())
        if len(digitos) >= 3 and len(digitos) == len(q.replace(".", "").replace("-", "").replace("/", "").replace(" ", "")):
            condicoes.append(Order.documento.contains(digitos))  # CPF/CNPJ com ou sem pontuação
        query = query.filter(db.or_(*condicoes))
    # as datas são gravadas em UTC; o período é escolhido no horário de Brasília (UTC-3)
    if de:
        query = query.filter(Order.created_at >= inicio_do_dia(de))
    if ate:
        query = query.filter(Order.created_at < inicio_do_dia(ate + timedelta(days=1)))
    usados = {k: v for k, v in (("status", filtro), ("q", q), ("de", de.isoformat() if de else ""),
                                ("ate", ate.isoformat() if ate else "")) if v}
    return query, usados


@admin.route("/orders")
@login_required
def orders():
    query, usados = filtrar_pedidos(request.args)
    total = query.count()
    paginas = max((total + POR_PAGINA - 1) // POR_PAGINA, 1)
    pagina = min(max(request.args.get("pagina", 1, type=int), 1), paginas)
    lista = query.order_by(Order.id.desc()).offset((pagina - 1) * POR_PAGINA).limit(POR_PAGINA).all()
    soma = (query.filter(Order.status != "cancelado").with_entities(func.coalesce(func.sum(Order.total), 0)).scalar()
            if total else 0)
    return render_template("admin/orders.html", orders=lista, filtro=usados.get("status", ""), usados=usados,
                           q=usados.get("q", ""), de=usados.get("de", ""), ate=usados.get("ate", ""),
                           total=total, soma=soma, pagina=pagina, paginas=paginas, metodos=METODOS)


def _reais_ou_zero(texto):
    try:
        v = Decimal((texto or "0").replace(",", ".").strip() or "0")
        return v if v.is_finite() else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


def _vendidos(pedido):
    """[(produto, quantidade)] somando as linhas do mesmo produto (fechadas + fracionadas)."""
    soma = {}
    for i in pedido.items:
        soma[i.product_id] = (i.product, soma.get(i.product_id, (None, 0))[1] + i.quantity)
    return list(soma.values())


@admin.get("/orders/imprimir")
@login_required
def orders_imprimir():
    """Romaneio de separação e etiqueta de envio, prontos para imprimir (um ou vários pedidos)."""
    ids = sorted({int(i) for i in request.args.getlist("oid") if i.isdigit() and len(i) <= 9})[:100]
    pedidos = Order.query.filter(Order.id.in_(ids)).order_by(Order.id).all() if ids else []
    if not pedidos:
        flash("Marque os pedidos que quer imprimir.")
        return redirect(url_for("admin.orders"))
    pesos = {o.id: sum((Decimal(i.product.peso_kg or 0) * i.quantity for i in o.items), Decimal(0)) for o in pedidos}
    return render_template("admin/imprimir.html", pedidos=pedidos, pesos=pesos, metodos=METODOS,
                           status=avisos.STATUS, empresa=lgpd.dados_empresa())


@admin.post("/orders/excluir")
@login_required
def orders_excluir():
    """Exclui de vez os pedidos marcados (na lista) ou um só (na página do pedido)."""
    ids = sorted({int(i) for i in request.form.getlist("oid") if i.isdigit()})
    filtro, voltar = request.form.get("filtro", ""), request.form.get("voltar", "")
    if not voltar.startswith(url_for("admin.orders")) or "//" in voltar or "\\" in voltar:  # só volta para a lista
        voltar = url_for("admin.orders", status=filtro) if filtro else url_for("admin.orders")
    if not ids:
        flash("Marque os pedidos que quer excluir.")
        return redirect(voltar)
    excluidos, falhas = [], []
    for o in Order.query.filter(Order.id.in_(ids)).order_by(Order.id).all():
        oid = o.id
        ok, erro = excluir_pedido(o)
        if ok:
            excluidos.append(f"#{oid}")
        else:
            falhas.append(f"#{oid} ({erro})")
    if excluidos:
        flash(("Pedido excluído: " if len(excluidos) == 1 else f"{len(excluidos)} pedidos excluídos: ")
              + ", ".join(excluidos) + ". O estoque dos que estavam em aberto ou pagos voltou.")
    if falhas:
        flash("Não foi possível excluir: " + "; ".join(falhas))
    if excluidos:
        avise_me.disparar()
    return redirect(voltar)


@admin.route("/orders/<int:oid>/status", methods=["POST"])
@login_required
def order_status(oid):
    order = db.get_or_404(Order, oid)
    novo = request.form.get("status")
    if order.status == "orcamento":  # orçamento passa pela revisão (preços, pagamento e prazo)
        return redirect(url_for("admin.orcamento", oid=oid))
    if novo in ("pendente", "pago", "cancelado") and novo != order.status:
        antes = order.status
        ok, erro = mudar_status(order, novo)  # cancelar devolve ao estoque; reativar baixa de novo
        flash(f"Pedido #{oid} marcado como {novo}." if ok else erro)
        if ok:
            historico.registrar(oid, {"pago": "Pagamento confirmado", "cancelado": "Pedido cancelado pela loja",
                                      "pendente": "Voltou para aguardando pagamento"}[novo])
        if ok and antes == "cancelado":
            avisos.estoque_baixo(_vendidos(order))
        if ok and novo == "pago":
            avisos.pagamento_confirmado(order)
        elif ok and novo == "cancelado":
            avisos.pedido_cancelado(order, pela_loja=True)
            avise_me.disparar()
    return redirect(url_for("admin.orders"))


@admin.post("/orders/<int:oid>/pix")
@login_required
def order_pix(oid):
    """Consulta na Efí se o Pix do pedido foi pago (o cliente pode ter fechado a página antes)."""
    from blueprints.shop import conferir_pix
    o = db.get_or_404(Order, oid)
    try:
        pago = conferir_pix(o, forcar=True)
    except efi.ErroEfi as e:
        flash(f"Não foi possível consultar a Efí agora: {e}")
        return redirect(url_for("admin.orders"))
    if not pago:
        flash(f"Pedido #{oid}: o Pix ainda não foi pago.")
    elif o.pagador_confere is False:
        flash(f"Pedido #{oid} pago, mas por {o.pagador_nome or 'outra pessoa'} ({o.pagador_documento or 'sem documento'}): "
              "diferente do cadastro. Confira antes de enviar.")
    else:
        flash(f"Pedido #{oid} pago e o pagador confere com o cadastro.")
    return redirect(url_for("admin.orders"))


@admin.post("/pedidos/<int:oid>/cartao")
@login_required
def order_cartao(oid):
    """Gera o link de pagamento com cartão na Efí, ou consulta se já foi pago (quando o aviso não chegou)."""
    import cartao_efi
    o = db.get_or_404(Order, oid)
    try:
        if request.form.get("acao") == "gerar":
            flash(f"Pedido #{oid}: " + cartao_efi.gerar_link(o, autor="loja"))
        else:
            flash(f"Pedido #{oid}: " + cartao_efi.consultar(o))
    except cartao_efi.ErroCartao as e:
        db.session.rollback()
        flash(f"Pedido #{oid}: {e}")
    return redirect(url_for("admin.orders"))


def _aprovacao_da_empresa(o, metodo):
    """Orçamento pedido por um comprador da empresa cliente: com o valor final acima do limite dele, a empresa
    (titular ou aprovador) aprova antes de virar pedido, como nos pedidos comuns. Devolve (titular, precisa?)."""
    if not o.comprador_id or not o.user_id:
        return None, False
    titular = db.session.get(User, o.user_id)
    membro = db.session.get(User, o.comprador_id)
    if not titular:
        return None, False
    if not membro or membro.empresa_id != titular.id:  # o comprador saiu da equipe: a empresa decide
        return titular, True
    return titular, equipe_empresa.precisa_aprovacao(membro, o.total, metodo)


@admin.route("/orders/<int:oid>/orcamento", methods=["GET", "POST"])
@login_required
def orcamento(oid):
    """Revisa um orçamento: ajusta preços, define pagamento e prazo, e aprova (baixa o estoque) ou recusa."""
    o = db.get_or_404(Order, oid)
    if o.status != "orcamento":
        flash(f"O pedido #{oid} não é mais um orçamento.")
        return redirect(url_for("admin.orders"))
    metodos = {k: v for k, v in METODOS.items() if k != "orcamento"}
    if request.method == "POST":
        f = request.form
        if f.get("acao") == "recusar":
            ok, erro = mudar_status(o, "cancelado")
            flash(f"Orçamento #{oid} recusado." if ok else erro)
            if ok:
                avisos.orcamento_respondido(o, aprovado=False)
                historico.registrar(oid, "Orçamento recusado")
            return redirect(url_for("admin.orders"))
        precos = {}
        try:
            for i in o.items:
                precos[i.id] = Decimal(f.get(f"preco_{i.id}", "").replace(",", ".").strip()).quantize(Decimal("0.01"))
        except InvalidOperation:
            flash("Confira os preços: use números como 16,50.")
            return redirect(url_for("admin.orcamento", oid=oid))
        metodo = f.get("pagamento", "")
        prazos, erro_prazo = ler_prazos(f.get("prazo", ""))
        if any(not v.is_finite() or not 0 <= v < 100_000_000 for v in precos.values()):
            flash("Os preços não podem ser negativos.")
        elif metodo not in metodos:
            flash("Escolha a forma de pagamento.")
        elif metodo == "faturado" and (erro_prazo or len(prazos or []) != 1):
            flash(erro_prazo or "Informe um prazo para o boleto faturado, como 30/60/90.")
        elif metodo == "faturado" and o.user_id and not credito.cabe(
                db.session.get(User, o.user_id),
                sum((precos[i.id] * i.quantity for i in o.items), Decimal(0)) - (o.desconto or 0)
                + _reais_ou_zero(f.get("frete")), exceto=o.id):
            cli = db.session.get(User, o.user_id)
            flash(f"Este orçamento passa do limite de crédito da empresa ({brl(cli.limite_credito)}; "
                  f"disponível {brl(credito.disponivel(cli, exceto=o.id))}). Escolha outra forma de "
                  "pagamento ou aumente o limite em Clientes.")
        elif _margem_barra(o, precos):
            pass
        else:
            for i in o.items:
                i.unit_price = precos[i.id]
            try:
                valor_frete = Decimal((f.get("frete") or "0").replace(",", ".").strip()).quantize(Decimal("0.01"))
            except InvalidOperation:
                valor_frete = Decimal(-1)
            if not valor_frete.is_finite() or not 0 <= valor_frete < 1_000_000:
                flash("Confira o valor do frete.")
                return redirect(url_for("admin.orcamento", oid=oid))
            o.frete, o.frete_combinar = valor_frete, False
            o.frete_prazo = " ".join(f.get("frete_prazo", "").split())[:60]
            o.desconto = cupons.recalcular(o)
            o.total = sum((i.unit_price * i.quantity for i in o.items), Decimal(0)) - o.desconto + valor_frete
            o.payment_method, o.prazo = metodo, prazos[0] if metodo == "faturado" else ""
            db.session.flush()
            titular, aprovar_na_empresa = _aprovacao_da_empresa(o, metodo)
            if aprovar_na_empresa:  # comprador acima do limite: a empresa aprova antes (sem prender estoque)
                ok, erro = mudar_status(o, "aprovacao")
                flash(f"Orçamento #{oid} aprovado pelo Marketplace B2B. Como passa do limite do comprador, agora espera a "
                      "aprovação da empresa do cliente (o estoque só é separado depois)." if ok else erro)
                if ok:
                    avisos.orcamento_respondido(o, aprovado=True, espera_empresa=True)
                    avisos.pedido_para_aprovar(o, equipe_empresa.aprovadores(titular))
                    historico.registrar(oid, f"Orçamento aprovado pelo Marketplace B2B ({METODOS.get(metodo, metodo)}, total "
                                             f"{brl(o.total)}); aguardando aprovação da empresa")
                return redirect(url_for("admin.orders") if ok else url_for("admin.orcamento", oid=oid))
            ok, erro = mudar_status(o, "pendente")  # baixa o estoque agora; se faltar, nada é gravado
            flash(f"Orçamento #{oid} aprovado: virou pedido aguardando pagamento." if ok else erro)
            if ok:
                avisos.orcamento_respondido(o, aprovado=True)
                avisos.estoque_baixo(_vendidos(o))
                historico.registrar(oid, f"Orçamento aprovado: virou pedido ({METODOS.get(metodo, metodo)}"
                                         f"{' em ' + o.prazo + ' dias' if o.prazo else ''}, total {brl(o.total)})")
            return redirect(url_for("admin.orders") if ok else url_for("admin.orcamento", oid=oid))
        return redirect(url_for("admin.orcamento", oid=oid))
    cliente = db.session.get(User, o.user_id) if o.user_id else None
    # pedido por um comprador da equipe: a página avisa que pode passar pela aprovação da empresa
    comprador = db.session.get(User, o.comprador_id) if o.comprador_id else None
    if comprador and comprador.empresa_id == o.user_id and comprador.papel != "comprador":
        comprador = None  # aprovador da empresa: segue direto, como o titular
    return render_template("admin/orcamento.html", o=o, cliente=cliente, metodos=metodos, comprador=comprador)


@admin.get("/planilhas/pedidos.xlsx")
@login_required
def exportar_pedidos():
    return _baixar(planilha.exportar_pedidos(), f"pedidos-marketplace-{agora_utc():%Y%m%d}.xlsx")


# ---------- pedido: envio e documentos ----------
@admin.get("/orders/<int:oid>")
@login_required
def pedido(oid):
    o = db.get_or_404(Order, oid)
    if o.status == "orcamento":
        return redirect(url_for("admin.orcamento", oid=oid))
    cliente = db.session.get(User, o.user_id) if o.user_id else None
    import lucro
    import margem
    import etiquetas
    import melhor_envio
    etiqueta = None
    if melhor_envio.tem_token() and o.status not in ("orcamento", "cancelado") and o.cep:
        etiqueta = {"ativas": etiquetas.ativas(o), "situacoes": etiquetas.SITUACOES, "tipos": etiquetas.TIPOS,
                    "servicos": melhor_envio.SERVICOS, "faltam": etiquetas.remetente()[1],
                    "sandbox": bool(current_app.config.get("MELHOR_ENVIO_SANDBOX")), "canceladas":
                    [e for e in o.etiquetas if e.situacao == "cancelada"], "ultima": _hora_br_texto(config("rastreio_ultima"))}
        if not etiqueta["ativas"]:
            etiqueta["f"] = etiquetas.preparar(o)
            etiqueta["cotacao"] = etiquetas.cotacao(o, o.cep, etiqueta["f"]["servico"])
    return render_template("admin/pedido.html", o=o, cliente=cliente, metodos=METODOS, tipos=entrega.TIPOS,
                           etiqueta=etiqueta,
                           lucro_pedido=lucro.do_pedido(o), avisos_margem=margem.do_pedido(o),
                           bloqueio_edicao=editar_pedido.motivo_bloqueio(o),
                           envio=entrega.ENVIO, max_arquivos=entrega.MAX_POR_PEDIDO)


@admin.post("/orders/<int:oid>/editar")
@login_required
def pedido_editar(oid):
    o = db.get_or_404(Order, oid)
    ok, msg = editar_pedido.aplicar(o, request.form)
    flash(msg)
    if ok and not msg.startswith("Nada mudou"):
        historico.registrar(oid, "Alterado pela loja: " + msg.split(": ", 1)[-1])
    if ok and "avisar" in request.form and not msg.startswith("Nada mudou"):
        avisos.pedido_alterado(db.session.get(Order, oid))
    if ok:
        avise_me.disparar()  # se tirou itens, o estoque voltou
    return redirect(url_for("admin.pedido", oid=oid) + ("" if ok else "#editar"))


def _margem_barra(o, precos):
    """No modo bloquear, o orçamento com item abaixo da margem mínima não é aprovado (o aviso explica)."""
    import formacao
    import margem
    if not margem.bloqueia():
        return False
    itens = [(i.product, precos[i.id], i.quantity, i.custo_unitario) for i in o.items if i.product]
    problemas = margem.problemas_itens(itens, formacao.uf_do_pedido(o) or None, o.desconto)
    if problemas:
        flash("Margem abaixo do mínimo, orçamento não aprovado: " + "; ".join(problemas) + ".")
    return bool(problemas)


@admin.post("/orders/<int:oid>/envio")
@login_required
def pedido_envio(oid):
    o = db.get_or_404(Order, oid)
    acao = request.form.get("acao")
    if o.status in ("orcamento", "cancelado"):
        flash("Orçamentos e pedidos cancelados não têm envio.")
    elif acao == "enviar":
        dados, erro = entrega.ler_envio(request.form)
        if erro:
            flash(erro)
        else:
            primeira_vez = o.envio == ""
            for campo, valor in dados.items():
                setattr(o, campo, valor)
            if primeira_vez:
                o.envio, o.enviado_em = "enviado", agora_utc()
            db.session.commit()
            if primeira_vez or "avisar" in request.form:
                avisos.pedido_enviado(o)
            historico.registrar(oid, (f"Enviado por {o.transportadora}" if primeira_vez else
                                      f"Dados do envio atualizados ({o.transportadora})")
                                + (f" · rastreio {o.rastreio}" if o.rastreio else ""))
            flash(f"Pedido #{oid} marcado como enviado" + (" e cliente avisado por e-mail." if primeira_vez or
                                                          "avisar" in request.form else " (dados de rastreio atualizados)."))
    elif acao == "entregue" and o.envio == "enviado":
        o.envio, o.entregue_em = "entregue", agora_utc()
        db.session.commit()
        avisos.pedido_entregue(o)
        historico.registrar(oid, "Entregue")
        flash(f"Pedido #{oid} marcado como entregue.")
    elif acao == "desfazer" and o.envio:
        if o.envio == "entregue":
            o.envio, o.entregue_em = "enviado", None
        else:
            o.envio, o.enviado_em = "", None
        db.session.commit()
        historico.registrar(oid, "Etapa de entrega desfeita (voltou para " + ("enviado)" if o.envio else "aguardando envio)"))
        flash("Etapa de entrega desfeita (o cliente não é avisado).")
    return redirect(url_for("admin.pedido", oid=oid))


@admin.post("/orders/<int:oid>/etiqueta")
@login_required
def pedido_etiqueta(oid):
    """Etiqueta do Melhor Envio: comprar, continuar (se algo falhou no meio), atualizar o rastreio e cancelar."""
    import etiquetas
    o = db.get_or_404(Order, oid)
    acao = request.form.get("acao")
    autor = historico._nome_do_admin() or "loja"
    if o.status in ("orcamento", "cancelado"):
        ok, msg = False, "Orçamentos e pedidos cancelados não têm etiqueta."
    elif acao == "comprar":
        ok, msg = etiquetas.comprar(o, request.form, autor)
        if ok:
            historico.registrar(oid, f"Etiqueta comprada no Melhor Envio ({ativas_texto(o)})")
    elif acao == "continuar":
        ok, msg = etiquetas.continuar(o)
    elif acao == "rastrear":
        ok, msg = etiquetas.rastrear(o)
    elif acao == "cancelar":
        ok, msg = etiquetas.cancelar(o, request.form.get("eid", type=int), autor)
    else:
        ok, msg = False, "Ação inválida."
    flash(msg)
    return redirect(url_for("admin.pedido", oid=oid) + "#etiqueta")


def _hora_br_texto(texto):
    """"2026-11-24 14:07" (UTC, gravado pela conferência automática) -> "24/11/2026 11:07" (Brasília)."""
    try:
        d = datetime.strptime(texto, "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""
    return hora_br(d)


def ativas_texto(o):
    import etiquetas
    lista = etiquetas.ativas(o)
    total = sum((e.valor for e in lista), Decimal(0))
    return f"{lista[0].servico}, {len(lista)} volume(s), {brl(total)}" if lista else ""


@admin.post("/orders/<int:oid>/arquivos")
@login_required
def pedido_arquivo_enviar(oid):
    o = db.get_or_404(Order, oid)
    dados, erro = entrega.ler_arquivo(request.files.get("arquivo"), request.form.get("tipo", ""), len(o.arquivos))
    if erro:
        flash(erro)
    else:
        a = PedidoArquivo(order_id=o.id, **dados)
        db.session.add(a)
        db.session.commit()
        avisar = "avisar" in request.form and o.status != "cancelado"
        if avisar:
            avisos.documento_disponivel(o, a)
        historico.registrar(oid, f"Anexou {entrega.TIPOS[a.tipo].lower()}: {a.nome}")
        flash(f"{entrega.TIPOS[a.tipo]} anexado ao pedido #{oid}" + (" e cliente avisado por e-mail." if avisar else "."))
    return redirect(url_for("admin.pedido", oid=oid))


def _arquivo_do_pedido(oid, aid):
    a = db.get_or_404(PedidoArquivo, aid)
    if a.order_id != oid:
        abort(404)
    return a


@admin.get("/orders/<int:oid>/arquivos/<int:aid>")
@login_required
def pedido_arquivo_baixar(oid, aid):
    return entrega.baixar(_arquivo_do_pedido(oid, aid))


@admin.post("/orders/<int:oid>/arquivos/<int:aid>/excluir")
@login_required
def pedido_arquivo_excluir(oid, aid):
    a = _arquivo_do_pedido(oid, aid)
    nome = a.nome
    db.session.delete(a)
    db.session.commit()
    historico.registrar(oid, f"Removeu o documento {nome}")
    flash(f"Documento {nome} removido.")
    return redirect(url_for("admin.pedido", oid=oid))
