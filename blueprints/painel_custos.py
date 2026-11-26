"""Fornecedores, custo dos produtos e formação de preço (impostos por estado, markup e transporte)."""
from decimal import Decimal

from flask import flash, redirect, render_template, request, url_for

import formacao
import frete
from b2b import config, formatar_documento
from formato import brl, num
from editar_pedido import _achar_produto
from models import Category, Fornecedor, ImpostoUF, Percentual, Product, ProdutoFornecedor, db
from blueprints.admin import admin, login_required

CENTAVO = Decimal("0.01")


def _aviso(alterados, sem_custo):
    partes = []
    if alterados:
        partes.append(f"{alterados} preço(s) de produto recalculado(s)")
    if sem_custo:
        partes.append(f"{len(sem_custo)} produto(s) com preço calculado estão sem preço de compra e mantêm o preço atual")
    return (" " + "; ".join(partes) + ".") if partes else ""


# ---------- fornecedores ----------
@admin.route("/fornecedores", methods=["GET", "POST"])
@login_required
def fornecedores():
    novo = {}
    if request.method == "POST":
        dados, erro = formacao.ler_fornecedor(request.form)
        if not erro:
            f = Fornecedor(**dados)
            db.session.add(f)
            db.session.commit()
            flash(f"Fornecedor “{f.nome}” cadastrado. Agora inclua os produtos e os preços de compra.")
            return redirect(url_for("admin.fornecedor", fid=f.id))
        flash(erro)
        novo = request.form
    lista = Fornecedor.query.order_by(Fornecedor.ativo.desc(), Fornecedor.nome).all()
    return render_template("admin/fornecedores.html", fornecedores=lista, novo=novo, ufs=frete.UFS,
                           transportes=formacao.TRANSPORTE_COMPRA, frete_pct=formacao.frete_compra_pct,
                           fmt_doc=formatar_documento)


@admin.route("/fornecedores/<int:fid>", methods=["GET", "POST"])
@login_required
def fornecedor(fid):
    forn = db.get_or_404(Fornecedor, fid)
    if request.method == "POST":
        acao, f = request.form.get("acao"), request.form
        if acao == "dados":
            dados, erro = formacao.ler_fornecedor(f)
            if erro:
                flash(erro)
            else:
                for campo, valor in dados.items():
                    setattr(forn, campo, valor)
                alterados, sem = formacao.recalcular(formacao.produtos_afetados(fornecedor=forn))
                db.session.commit()
                flash("Fornecedor salvo." + _aviso(alterados, sem))
        elif acao == "incluir":
            p = _achar_produto(f.get("produto"))
            dados, erro = formacao.ler_custo(f)
            if not p:
                flash("Produto não encontrado: use o código, o número (#12) ou o nome exato.")
            elif erro:
                flash(erro)
            else:
                c = next((c for c in forn.precos if c.product_id == p.id), None)
                if c:
                    for campo, valor in dados.items():
                        setattr(c, campo, valor)
                else:
                    c = ProdutoFornecedor(product=p, fornecedor=forn, principal=not p.custos, **dados)
                    db.session.add(c)
                alterados, sem = formacao.recalcular([p.id])
                db.session.commit()
                flash(f"Preço de compra de {p.name} salvo." + _aviso(alterados, sem))
        elif acao == "precos":
            erro = None
            for c in forn.precos:
                dados, erro = formacao.ler_custo(f, f"_{c.id}")
                if erro:
                    erro = f"{c.product.name}: {erro}"
                    break
                for campo, valor in dados.items():
                    setattr(c, campo, valor)
            if erro:
                db.session.rollback()
                flash(erro)
            else:
                alterados, sem = formacao.recalcular(formacao.produtos_afetados(fornecedor=forn))
                db.session.commit()
                flash("Preços de compra salvos." + _aviso(alterados, sem))
        elif acao == "remover":
            c = db.get_or_404(ProdutoFornecedor, f.get("cid", type=int) or 0)
            if c.fornecedor_id != forn.id:
                return redirect(url_for("admin.fornecedor", fid=fid))
            produto = c.product
            pid, nome = produto.id, produto.name
            produto.custos.remove(c)  # sai da lista do produto (e é apagado): o novo cálculo já não o vê
            alterados, sem = formacao.recalcular([pid])
            db.session.commit()
            flash(f"{nome} saiu da lista deste fornecedor." + _aviso(alterados, sem))
        return redirect(url_for("admin.fornecedor", fid=fid))
    itens = sorted(forn.precos, key=lambda c: c.product.name.lower())
    return render_template("admin/fornecedor.html", f=forn, itens=itens, custo_de=formacao.custo_de,
                           fonte=formacao.fonte, ufs=frete.UFS, transportes=formacao.TRANSPORTE_COMPRA,
                           frete_pct=formacao.frete_compra_pct, fmt_doc=formatar_documento)


@admin.post("/fornecedores/<int:fid>/excluir")
@login_required
def fornecedor_excluir(fid):
    forn = db.get_or_404(Fornecedor, fid)
    from models import Compra, Lancamento
    if Compra.query.filter_by(fornecedor_id=fid).first() or Lancamento.query.filter_by(fornecedor_id=fid).first():
        flash("Este fornecedor tem compras ou contas registradas: desmarque “Ativo” em vez de excluir.")
        return redirect(url_for("admin.fornecedor", fid=fid))
    nome, afetados = forn.nome, formacao.produtos_afetados(fornecedor=forn)
    db.session.delete(forn)
    alterados, sem = formacao.recalcular(afetados)
    db.session.commit()
    flash(f"Fornecedor “{nome}” excluído com os preços de compra dele." + _aviso(alterados, sem))
    return redirect(url_for("admin.fornecedores"))


# ---------- custo e preço de um produto ----------
@admin.route("/produtos/<int:pid>/custo", methods=["GET", "POST"])
@login_required
def produto_custo(pid):
    p = db.get_or_404(Product, pid)
    if request.method == "POST":
        acao, f = request.form.get("acao"), request.form
        if acao == "percentual_incluir":
            dados, erro = formacao.ler_percentual(f, produto_fixo=p)
            if not erro and any(formacao.chave_nome(x.nome) == formacao.chave_nome(dados["nome"]) and x.base == dados["base"]
                                and (x.uf or "") == dados["uf"] for x in p.percentuais):
                erro = f"{dados['nome']} já está cadastrado neste produto: tire o antigo antes de incluir outro."
            if not erro:
                db.session.add(Percentual(**dados))
                erro = formacao.conferir_limite([p.id])
            if erro:
                db.session.rollback()
                flash(erro)
            else:
                alterados, sem = formacao.recalcular([p.id])
                db.session.commit()
                flash(f"{dados['nome']} ({num(dados['valor'])}%) incluído neste produto." + _aviso(alterados, sem))
            return redirect(url_for("admin.produto_custo", pid=pid) + "#percentuais")
        if acao == "percentual_remover":
            x = db.get_or_404(Percentual, f.get("xid", type=int) or 0)
            if x.product_id == p.id:
                nome = x.nome
                db.session.delete(x)
                alterados, sem = formacao.recalcular([p.id])
                db.session.commit()
                flash(f"{nome} saiu deste produto." + _aviso(alterados, sem))
            return redirect(url_for("admin.produto_custo", pid=pid) + "#percentuais")
        if acao == "incluir":
            forn = db.session.get(Fornecedor, f.get("fornecedor_id", type=int) or 0)
            dados, erro = formacao.ler_custo(f)
            if not forn:
                flash("Escolha o fornecedor.")
            elif erro:
                flash(erro)
            elif any(c.fornecedor_id == forn.id for c in p.custos):
                flash(f"{forn.nome} já está na lista: altere o preço na tabela.")
            else:
                p.custos.append(ProdutoFornecedor(fornecedor=forn, principal=not p.custos, **dados))
                alterados, sem = formacao.recalcular([p.id])
                db.session.commit()
                flash(f"Preço de compra de {forn.nome} incluído." + _aviso(alterados, sem))
        elif acao == "remover":
            c = db.get_or_404(ProdutoFornecedor, f.get("cid", type=int) or 0)
            if c.product_id == p.id:
                nome = c.fornecedor.nome
                p.custos.remove(c)
                alterados, sem = formacao.recalcular([p.id])
                db.session.commit()
                flash(f"{nome} saiu da lista de fornecedores deste produto." + _aviso(alterados, sem))
        else:
            erro = None
            for c in p.custos:
                dados, erro = formacao.ler_custo(f, f"_{c.id}")
                if erro:
                    erro = f"{c.fornecedor.nome}: {erro}"
                    break
                for campo, valor in dados.items():
                    setattr(c, campo, valor)
            principal = f.get("principal", type=int)
            for c in p.custos:
                c.principal = c.id == principal
            markup, erro_markup = formacao.ler_markup(f.get("markup", ""), "o markup do produto")
            erro = erro or erro_markup
            auto = "preco_auto" in f
            if not erro and auto and not formacao.fonte(p):
                erro = "Para o preço sair do custo, inclua pelo menos um fornecedor ativo com preço de compra."
            if erro:
                db.session.rollback()
                flash(erro)
            else:
                p.markup, p.preco_auto = markup, auto
                formacao.recalcular([p.id])
                db.session.commit()
                flash("Custo e preço salvos." + (f" Preço de tabela ({config('loja_uf') or 'estado da loja'}): "
                                                 f"{brl(p.price)}." if auto else ""))
        return redirect(url_for("admin.produto_custo", pid=pid))
    calc = formacao.calcular(p)
    por_estado = []
    if calc:
        for uf in frete.UFS:
            r = formacao.calcular(p, uf)
            por_estado.append({"uf": uf, "preco": r["preco"] if r else None, "venda": formacao.venda_pct(uf),
                               "propria": formacao.entrega_propria(uf)})
    usados = {c.fornecedor_id for c in p.custos}
    livres = Fornecedor.query.filter(Fornecedor.ativo.is_(True)).order_by(Fornecedor.nome).all()
    loja_uf = formacao.parametros()["loja"]
    vigentes = formacao.percentuais(p, None, "venda") + formacao.percentuais(p, None, "custo")
    vigentes_ids = {x["id"] for x in vigentes}
    proprios = sorted(p.percentuais, key=lambda x: (x.nome.casefold(), x.uf or ""))
    return render_template("admin/produto_custo.html", p=p, calc=calc, por_estado=por_estado,
                           vigentes=vigentes, vigentes_ids=vigentes_ids, proprios=proprios, bases=formacao.BASES,
                           escopos=formacao.ESCOPOS, ufs=frete.UFS, loja_uf=loja_uf,
                           livres=[x for x in livres if x.id not in usados], custo_de=formacao.custo_de,
                           fonte=formacao.fonte(p), markup=formacao.markup(p), loja=formacao.parametros()["loja"])


# ---------- formação de preço: impostos por estado, despesas, markup e entrega ----------
def _valores_da_tela(categorias):
    p = formacao.parametros()
    v = {"loja_uf": p["loja"], "imp_federais": num(p["federais"]), "despesas_venda": num(p["despesas"]),
         "markup_padrao": num(p["markup"])}
    for uf in frete.UFS:
        linha = p["ufs"].get(uf) or {}
        v[f"icms_{uf}"], v[f"adicional_{uf}"] = num(linha.get("icms")), num(linha.get("adicional"))
        v[f"entrega_{uf}"] = linha.get("entrega") or "transportadora"
        v[f"custo_entrega_{uf}"] = num(linha.get("custo_entrega")) if linha.get("custo_entrega") else ""
    for c in categorias:
        v[f"markup_cat_{c.id}"] = num(c.markup)
    return v


@admin.route("/formacao-de-preco", methods=["GET", "POST"])
@login_required
def formacao_preco():
    categorias = Category.query.order_by(Category.name).all()
    valores = None
    if request.method == "POST":
        if request.form.get("acao") == "margem":
            import margem
            minima, erro = formacao.ler_pct(request.form.get("margem_minima", ""), "a margem mínima")
            if erro:
                flash(erro)
            else:
                margem.salvar(request.form.get("margem_modo", "avisar"), minima)
                db.session.commit()
                flash("Proteção de margem salva.")
            return redirect(url_for("admin.formacao_preco") + "#margem")
        if request.form.get("acao") == "icms_interestadual":
            loja = config("loja_uf", "")
            if not loja:
                flash("Escolha e salve o estado da loja primeiro.")
            else:
                existentes = {r.uf: r for r in ImpostoUF.query.all()}
                for uf in frete.UFS:
                    if uf != loja:
                        r = existentes.get(uf) or ImpostoUF(uf=uf, entrega="transportadora", custo_entrega=0)
                        r.icms = formacao.icms_interestadual(loja, uf)
                        db.session.add(r)
                alterados, sem = formacao.recalcular()
                db.session.commit()
                flash(f"ICMS interestadual preenchido a partir de {loja}: "
                      + ("7% para Norte, Nordeste, Centro-Oeste e ES; 12% para Sul e Sudeste. "
                         if loja in formacao.SUL_SUDESTE else "12% para todos os estados. ")
                      + "Confira (importados pagam 4%)." + _aviso(alterados, sem))
            return redirect(url_for("admin.formacao_preco"))
        dados, erro = formacao.ler_config(request.form)
        if not erro:
            formacao.gravar_config(dados)
            alterados, sem = formacao.recalcular()
            db.session.commit()
            flash("Formação de preço salva." + _aviso(alterados, sem))
            return redirect(url_for("admin.formacao_preco"))
        flash(erro)
        valores = request.form
    automaticos = Product.query.filter(Product.preco_auto.is_(True)).order_by(Product.name).all()
    sem_custo = [p for p in automaticos if not formacao.fonte(p)]
    markup = formacao.parametros()["markup"]
    linhas = formacao.linhas_da_tela()
    for linha in linhas:  # exemplo para ter noção: R$ 100,00 de custo com o markup padrão
        linha["exemplo"] = (Decimal(100) * (100 + markup) / (100 - linha["venda"])).quantize(CENTAVO) \
            if linha["venda"] < 100 else None
    import margem
    return render_template("admin/formacao.html", v=valores or _valores_da_tela(categorias), linhas=linhas,
                           margem_modo=margem.modo(), margem_minima=margem.minimo(), margem_modos=margem.MODOS,
                           categorias=categorias, ufs=frete.UFS, entregas=formacao.ENTREGA_VENDA,
                           automaticos=automaticos, sem_custo=sem_custo, loja=formacao.parametros()["loja"],
                           fornecedores=Fornecedor.query.count())


# ---------- percentuais: impostos e despesas cadastrados pela loja, por produto, categoria ou para todos ----------
def _valores_percentual(x):
    if x is None:
        return {"base": "venda", "escopo": "todos", "ativo": "on"}
    v = {"nome": x.nome, "base": x.base, "valor": num(x.valor), "escopo": x.escopo, "uf": x.uf or "",
         "category_id": str(x.category_id or ""), "observacao": x.observacao or ""}
    if x.produto:
        v["produto"] = x.produto.codigo or x.produto.name
    if x.ativo:
        v["ativo"] = "on"
    return v


def _tela_percentuais(x=None, valores=None):
    filtro = request.args.get("filtro", "")
    q = Percentual.query
    if filtro in formacao.ESCOPOS:
        q = q.filter(Percentual.escopo == filtro)
    elif filtro in formacao.BASES:
        q = q.filter(Percentual.base == filtro)
    lista = q.all()
    lista.sort(key=lambda r: (formacao.NIVEL.get(r.escopo, 0), r.nome.casefold(),
                              (r.categoria.name if r.categoria else "") + (r.produto.name if r.produto else ""),
                              r.uf or ""))
    return render_template("admin/percentuais.html", lista=lista, x=x, v=valores or _valores_percentual(x),
                           filtro=filtro, bases=formacao.BASES, escopos=formacao.ESCOPOS, ufs=frete.UFS,
                           categorias=Category.query.order_by(Category.name).all(),
                           total_geral=formacao.venda_pct(None), loja=formacao.parametros()["loja"])


@admin.route("/formacao-de-preco/percentuais", methods=["GET", "POST"])
@login_required
def percentuais():
    if request.method == "POST":
        dados, erro = formacao.ler_percentual(request.form)
        if not erro:
            x = Percentual(**dados)
            db.session.add(x)
            erro = formacao.conferir_limite(formacao.produtos_afetados(percentual=x))
        if erro:
            db.session.rollback()
            flash(erro)
            return _tela_percentuais(valores=request.form)
        alterados, sem = formacao.recalcular(formacao.produtos_afetados(percentual=x))
        db.session.commit()
        flash(f"Percentual {x.nome} ({num(x.valor)}%) cadastrado." + _aviso(alterados, sem))
        return redirect(url_for("admin.percentuais"))
    return _tela_percentuais()


@admin.route("/formacao-de-preco/percentuais/<int:xid>", methods=["GET", "POST"])
@login_required
def percentual(xid):
    x = db.get_or_404(Percentual, xid)
    if request.method == "POST":
        acao = request.form.get("acao", "salvar")
        antes = formacao.produtos_afetados(percentual=x)
        if acao == "excluir":
            nome = x.nome
            db.session.delete(x)
            alterados, sem = formacao.recalcular(antes)
            db.session.commit()
            flash(f"Percentual {nome} excluído." + _aviso(alterados, sem))
            return redirect(url_for("admin.percentuais"))
        if acao == "ativar":
            x.ativo = not x.ativo
            erro = formacao.conferir_limite(antes) if x.ativo else None
            if erro:
                db.session.rollback()
                flash(erro)
            else:
                alterados, sem = formacao.recalcular(antes)
                db.session.commit()
                flash(f"Percentual {x.nome} {'ativado' if x.ativo else 'desativado'}." + _aviso(alterados, sem))
            return redirect(request.form.get("voltar") == "lista" and url_for("admin.percentuais")
                            or url_for("admin.percentual", xid=xid))
        dados, erro = formacao.ler_percentual(request.form)
        if not erro:
            for campo, valor in dados.items():
                setattr(x, campo, valor)
            depois = formacao.produtos_afetados(percentual=x)
            afetados = None if antes is None or depois is None else sorted(set(antes) | set(depois))
            erro = formacao.conferir_limite(afetados)
        if erro:
            db.session.rollback()
            flash(erro)
            return _tela_percentuais(x, request.form)
        alterados, sem = formacao.recalcular(afetados)
        db.session.commit()
        flash(f"Percentual {x.nome} salvo." + _aviso(alterados, sem))
        return redirect(url_for("admin.percentuais"))
    return _tela_percentuais(x)
