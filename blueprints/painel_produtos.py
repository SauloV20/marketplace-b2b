"""Produtos do painel: cadastro, categorias, fotos, banners e planilhas."""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import func
from b2b import promocao_ativa, faixas_texto, gravar_faixas, ler_faixas
import avise_me
import fiscal
import banners
import planilha
import produtos
from models import PerfilFiscal, Banner, Category, ImportacaoPendente, Product, ProdutoFoto, agora_utc, db
from blueprints.admin import admin, _baixar, login_required


def ler_produto(f, pid=None):
    """Valida o formulário de produto. Devolve (dados, mensagem_de_erro)."""
    codigo = f.get("codigo", "").strip()[:40] or None
    if codigo and Product.query.filter(Product.codigo == codigo, Product.id != (pid or 0)).first():
        return None, f"Já existe outro produto com o código {codigo}."
    try:
        preco = Decimal(f.get("price", "").replace(",", ".").strip())
        estoque, categoria = int(f.get("stock", "")), int(f.get("category_id", ""))
    except (InvalidOperation, ValueError):
        return None, "Confira preço, estoque e categoria."
    nome, imagem = f.get("name", "").strip(), f.get("image_url", "").strip()
    if not nome or len(nome) > 160:
        return None, "Informe o nome do produto (até 160 caracteres)."
    if not preco.is_finite() or not 0 <= preco < 100_000_000 or estoque < 0:
        return None, "Preço e estoque não podem ser negativos."
    if imagem and (not imagem.startswith(("http://", "https://")) or len(imagem) > 500):
        return None, "A imagem precisa ser um link http:// ou https://."
    if not db.session.get(Category, categoria):
        return None, "Categoria inválida."
    try:  # campos de atacado: opcionais no cadastro rápido
        minimo, multiplo = int(f.get("qtd_minima") or 1), int(f.get("multiplo") or 1)
    except ValueError:
        return None, "Quantidade mínima e múltiplo precisam ser números inteiros."
    if not (1 <= minimo <= 100_000 and 1 <= multiplo <= 100_000):
        return None, "Quantidade mínima e múltiplo vão de 1 a 100.000."
    try:
        acrescimo = Decimal((f.get("acrescimo_fracionado") or "0").replace(",", ".").strip())
        avulso = int(f.get("estoque_avulso") or 0)
    except (InvalidOperation, ValueError):
        return None, "Confira o acréscimo do fracionado e as unidades avulsas."
    if not acrescimo.is_finite() or not 0 <= acrescimo <= 500:
        return None, "O acréscimo do fracionado vai de 0 a 500%."
    if not 0 <= avulso <= estoque:
        return None, "As unidades avulsas fazem parte do estoque: vão de 0 até o estoque total."
    try:
        peso = Decimal((f.get("peso_kg") or "0").replace(",", ".").strip())
    except InvalidOperation:
        return None, "Confira o peso (em kg, ex.: 0,350)."
    if not peso.is_finite() or not 0 <= peso < 100_000:
        return None, "O peso vai de 0 a 100.000 kg."
    medidas = {}
    for campo, rotulo in (("altura_cm", "a altura"), ("largura_cm", "a largura"), ("comprimento_cm", "o comprimento")):
        texto = (f.get(campo) or "").replace(",", ".").strip()
        if not texto:
            medidas[campo] = None
            continue
        try:
            v = Decimal(texto)
        except InvalidOperation:
            return None, f"Confira {rotulo} da embalagem (em cm, ex.: 12,5)."
        if not v.is_finite() or not 0 < v <= 1000:
            return None, f"{rotulo[0].upper()}{rotulo[1:]} da embalagem vai de 0,1 a 1.000 cm (ou deixe em branco)."
        medidas[campo] = v.quantize(Decimal("0.1"))
    try:
        alerta = int(f.get("estoque_alerta") or 0)
    except ValueError:
        return None, "O alerta de estoque é um número inteiro de unidades (0 para não avisar)."
    if not 0 <= alerta <= 1_000_000:
        return None, "O alerta de estoque vai de 0 (não avisa) a 1.000.000 unidades."
    promo, inicio, fim = None, None, None
    if (f.get("preco_promocional") or "").strip():
        try:
            promo = Decimal(f.get("preco_promocional").replace("R$", "").replace(".", "").replace(",", ".").strip()
                            if "," in f.get("preco_promocional") else f.get("preco_promocional").strip())
            inicio = datetime.strptime(f["promo_inicio"], "%Y-%m-%d").date() if f.get("promo_inicio") else None
            fim = datetime.strptime(f["promo_fim"], "%Y-%m-%d").date() if f.get("promo_fim") else None
        except (InvalidOperation, ValueError):
            return None, "Confira o preço promocional (ex.: 149,90) e as datas da promoção."
        if not promo.is_finite() or not 0 < promo < preco:
            return None, "O preço promocional precisa ser maior que zero e menor que o preço normal."
        if inicio and fim and fim < inicio:
            return None, "A promoção termina antes de começar: confira as datas."
        promo = promo.quantize(Decimal("0.01"))
    return dict(codigo=codigo, name=nome, description=f.get("description", "").strip(), price=preco.quantize(Decimal("0.01")),
                stock=estoque, image_url=imagem, category_id=categoria, qtd_minima=minimo, multiplo=multiplo,
                fracionavel="fracionavel" in f and multiplo > 1, acrescimo_fracionado=acrescimo.quantize(Decimal("0.01")),
                estoque_avulso=avulso, peso_kg=peso.quantize(Decimal("0.001")), estoque_alerta=alerta,
                preco_promocional=promo, promo_inicio=inicio, promo_fim=fim, **medidas), None


AVISO_LINK = ("A foto deste produto vem de outro site: pode sumir a qualquer momento (o produto fica sem foto) e "
              "usar foto de outra loja pode dar problema de direitos. O ideal é enviar a sua foto e apagar o link.")


@admin.route("/products", methods=["GET", "POST"])
@login_required
def products():
    aberto = False
    if request.method == "POST":  # cadastro completo de uma vez: dados, atacado, fracionamento e fotos
        dados, erro = ler_produto(request.form)
        faixas, erro_faixas = ler_faixas(request.form.get("faixas", ""))
        fotos, erro_fotos = produtos.ler_fotos(request.files.getlist("fotos"), 0, obrigatorio=False)
        if erro or erro_faixas or erro_fotos:
            flash(erro or erro_faixas or erro_fotos)
            aberto = True
        else:
            p = Product(**dados)
            db.session.add(p)
            gravar_faixas(p, faixas)
            db.session.flush()
            for i, (conteudo, tipo) in enumerate(fotos, start=1):
                db.session.add(produtos.nova_foto(p, conteudo, tipo, i))
            db.session.commit()
            flash(f"Produto “{p.name}” cadastrado" + (f" com {len(fotos)} foto(s)." if fotos else ".")
                  + " Ele já está na loja.")
            if p.image_url and not fotos:
                flash(AVISO_LINK)
            return redirect(url_for("admin.products", cat=dados["category_id"]))
    cat = request.args.get("cat", type=int)
    q = request.args.get("q", "").strip()
    query = Product.query
    if cat:
        query = query.filter_by(category_id=cat)
    if q:
        query = query.filter(db.or_(Product.name.ilike(f"%{q}%"), Product.codigo.ilike(f"%{q}%")))
    so_link = request.args.get("foto") == "link"
    com_link = Product.image_url.isnot(None) & (Product.image_url != "")
    if so_link:
        query = query.filter(com_link)
    fotos_de_fora = Product.query.filter(com_link).count()
    contagem = dict(db.session.query(Product.category_id, func.count(Product.id)).group_by(Product.category_id).all())
    return render_template("admin/products.html", products=query.order_by(Product.id.desc()).all(), promo=promocao_ativa,
                           aberto=aberto or request.args.get("novo") == "1", f=request.form,
                           categories=Category.query.order_by(Category.name).all(),
                           cat=cat, q=q, contagem=contagem, total=sum(contagem.values()),
                           so_link=so_link, fotos_de_fora=fotos_de_fora)


@admin.route("/categories", methods=["GET", "POST"])
@login_required
def categories():
    if request.method == "POST":
        nome = " ".join(request.form.get("name", "").split())
        if not nome or len(nome) > 80:
            flash("Informe o nome da categoria (até 80 caracteres).")
        elif Category.query.filter(func.lower(Category.name) == nome.lower()).first():
            flash("Já existe uma categoria com esse nome.")
        else:
            db.session.add(Category(name=nome))
            db.session.commit()
            flash(f"Categoria \u201c{nome}\u201d criada.")
        return redirect(url_for("admin.categories"))
    contagem = dict(db.session.query(Product.category_id, func.count(Product.id)).group_by(Product.category_id).all())
    return render_template("admin/categories.html", categories=Category.query.order_by(Category.name).all(),
                           contagem=contagem)


@admin.route("/categories/<int:cid>/rename", methods=["POST"])
@login_required
def rename_category(cid):
    c = db.get_or_404(Category, cid)
    nome = " ".join(request.form.get("name", "").split())
    outra = Category.query.filter(func.lower(Category.name) == nome.lower(), Category.id != cid).first()
    if not nome or len(nome) > 80:
        flash("Informe o nome da categoria (até 80 caracteres).")
    elif outra:
        flash("Já existe uma categoria com esse nome.")
    else:
        c.name = nome
        db.session.commit()
        flash("Categoria renomeada.")
    return redirect(url_for("admin.categories"))


@admin.route("/categories/<int:cid>/delete", methods=["POST"])
@login_required
def delete_category(cid):
    c = db.get_or_404(Category, cid)
    if Product.query.filter_by(category_id=cid).count():
        flash("Essa categoria ainda tem produtos. Mova ou remova os produtos antes de excluir.")
    else:
        db.session.delete(c)
        db.session.commit()
        flash("Categoria excluída.")
    return redirect(url_for("admin.categories"))


@admin.route("/products/<int:pid>/edit", methods=["GET", "POST"])
@login_required
def edit_product(pid):
    p = db.get_or_404(Product, pid)
    texto_faixas = faixas_texto(p)
    if request.method == "POST":
        dados, erro = ler_produto(request.form, pid)
        texto_faixas = request.form.get("faixas", "")
        faixas, erro_faixas = ler_faixas(texto_faixas)
        fiscais, erro_fiscal = fiscal.ler_fiscal_produto(request.form) if "ncm" in request.form else ({}, None)
        if erro or erro_faixas or erro_fiscal:
            flash(erro or erro_faixas or erro_fiscal)
        else:
            dados.update(fiscais)
            if p.preco_auto:  # preço calculado: sai do custo (Formação de preço), não do formulário
                dados.pop("price", None)
            for campo, valor in dados.items():
                setattr(p, campo, valor)
            gravar_faixas(p, faixas)
            p.active = "active" in request.form
            db.session.commit()
            avisados = avise_me.disparar()
            flash("Produto atualizado." + (f" {avisados} cliente(s) avisado(s) de que ele voltou ao estoque."
                                           if avisados else ""))
            if p.image_url:
                flash(AVISO_LINK if not p.fotos else
                      "Este produto já tem foto enviada e ainda mostra também a foto do link de outro site. "
                      "Apague o link (URL da imagem) para usar só as suas fotos.")
            import margem
            for aviso in margem.do_produto(p):
                flash("Atenção à margem: " + aviso)
            return redirect(url_for("admin.products"))
    return render_template("admin/product_edit.html", p=p, faixas=texto_faixas, esperando=avise_me.esperando(p.id),
                           promo_ativa=promocao_ativa(p), origens=fiscal.ORIGENS,
                           perfis=PerfilFiscal.query.order_by(PerfilFiscal.nome).all(),
                           categories=Category.query.order_by(Category.name).all())


@admin.route("/products/<int:pid>/toggle", methods=["POST"])
@login_required
def toggle(pid):
    product = db.get_or_404(Product, pid)
    product.active = not product.active
    db.session.commit()
    avise_me.disparar()
    flash("Produto ativado." if product.active else "Produto desativado.")
    return redirect(url_for("admin.products"))


@admin.get("/planilhas")
@login_required
def planilhas():
    return render_template("admin/planilhas.html", colunas=planilha.COLUNAS)


@admin.get("/planilhas/produtos.xlsx")
@login_required
def exportar_produtos():
    return _baixar(planilha.exportar_produtos(), f"produtos-marketplace-{agora_utc():%Y%m%d}.xlsx")


def _previa(imp):
    linhas = planilha.ler_arquivo(imp.conteudo, imp.nome_arquivo)
    return planilha.validar(linhas)


@admin.post("/planilhas/importar")
@login_required
def importar_planilha():
    arquivo = request.files.get("arquivo")
    if not arquivo or not arquivo.filename:
        flash("Escolha o arquivo da planilha.")
        return redirect(url_for("admin.planilhas"))
    ImportacaoPendente.query.filter(ImportacaoPendente.criado_em < agora_utc() - timedelta(days=1)).delete()
    imp = ImportacaoPendente(nome_arquivo=arquivo.filename[:200], conteudo=arquivo.read(),
                             desativar_ausentes="desativar_ausentes" in request.form)
    try:
        alteracoes, erros = _previa(imp)
    except planilha.ErroPlanilha as e:
        db.session.rollback()
        flash(str(e))
        return redirect(url_for("admin.planilhas"))
    db.session.add(imp)
    db.session.commit()
    return render_template("admin/planilha_previa.html", imp=imp, alteracoes=alteracoes, erros=erros,
                           novos=sum(a["acao"] == "novo" for a in alteracoes),
                           atualizar=sum(a["acao"] == "atualizar" for a in alteracoes))


@admin.post("/planilhas/confirmar/<int:iid>")
@login_required
def confirmar_planilha(iid):
    imp = db.get_or_404(ImportacaoPendente, iid)
    alteracoes, erros = _previa(imp)  # confere de novo: o catálogo pode ter mudado desde a prévia
    if erros:
        flash("A planilha tem erros. Corrija e envie de novo.")
        return redirect(url_for("admin.planilhas"))
    novos, atualizados, desativados, criadas = planilha.aplicar(alteracoes, imp.desativar_ausentes)
    db.session.delete(imp)
    db.session.commit()
    partes = [f"{novos} produto(s) novo(s)", f"{atualizados} atualizado(s)"]
    if desativados:
        partes.append(f"{desativados} desativado(s)")
    if criadas:
        partes.append(f"{criadas} categoria(s) criada(s)")
    avisados = avise_me.disparar()
    if avisados:
        partes.append(f"{avisados} cliente(s) avisado(s) de produtos que voltaram")
    flash("Planilha importada: " + ", ".join(partes) + ".")
    return redirect(url_for("admin.products"))


# ---------- banners ----------
@admin.get("/banners")
@login_required
def banners_lista():
    return render_template("admin/banners.html", banners=Banner.query.order_by(Banner.ordem, Banner.id).all(),
                           cores=banners.CORES)


@admin.route("/banners/novo", methods=["GET", "POST"])
@admin.route("/banners/<int:bid>", methods=["GET", "POST"])
@login_required
def banner_editar(bid=None):
    b = db.get_or_404(Banner, bid) if bid else None
    if request.method == "POST":
        dados, imagem, erro = banners.ler_formulario(request.form, request.files.get("imagem"), b)
        if erro:
            flash(erro)
        else:
            if not b:
                b = Banner()
                db.session.add(b)
            for campo, valor in dados.items():
                setattr(b, campo, valor)
            if imagem:
                b.imagem, b.imagem_tipo, b.versao = imagem[0], imagem[1], (b.versao or 0) + 1
            elif "remover_imagem" in request.form:
                b.imagem, b.imagem_tipo = None, ""
            db.session.commit()
            flash("Banner salvo." + ("" if b.ativo else " Ele está desativado: não aparece na loja."))
            return redirect(url_for("admin.banners_lista"))
    produtos = Product.query.filter_by(active=True).order_by(Product.name).all()
    return render_template("admin/banner_form.html", b=b, produtos=produtos, cores=banners.CORES,
                           sugestao_ordem=(db.session.query(db.func.max(Banner.ordem)).scalar() or 0) + 1)


@admin.post("/banners/<int:bid>/ativar")
@login_required
def banner_ativar(bid):
    b = db.get_or_404(Banner, bid)
    b.ativo = not b.ativo
    db.session.commit()
    flash("Banner ativado." if b.ativo else "Banner desativado.")
    return redirect(url_for("admin.banners_lista"))


@admin.post("/banners/<int:bid>/mover")
@login_required
def banner_mover(bid):
    lista = Banner.query.order_by(Banner.ordem, Banner.id).all()
    i = next(k for k, x in enumerate(lista) if x.id == bid)
    j = i - 1 if request.form.get("para") == "cima" else i + 1
    if 0 <= j < len(lista):
        lista[i], lista[j] = lista[j], lista[i]
        for k, x in enumerate(lista, start=1):
            x.ordem = k
        db.session.commit()
    return redirect(url_for("admin.banners_lista"))


@admin.post("/banners/<int:bid>/excluir")
@login_required
def banner_excluir(bid):
    db.session.delete(db.get_or_404(Banner, bid))
    db.session.commit()
    flash("Banner excluído.")
    return redirect(url_for("admin.banners_lista"))


# ---------- fotos do produto ----------
@admin.post("/products/<int:pid>/fotos")
@login_required
def fotos_enviar(pid):
    p = db.get_or_404(Product, pid)
    novas, erro = produtos.ler_fotos(request.files.getlist("fotos"), len(p.fotos))
    if erro:
        flash(erro)
    else:
        proxima = max([f.ordem for f in p.fotos], default=0) + 1
        for i, (dados, tipo) in enumerate(novas):
            db.session.add(produtos.nova_foto(p, dados, tipo, proxima + i))
        db.session.commit()
        flash(f"{len(novas)} foto(s) enviada(s).")
    return redirect(url_for("admin.edit_product", pid=pid))


def _foto_do_produto(pid, fid):
    f = db.get_or_404(ProdutoFoto, fid)
    if f.product_id != pid:
        abort(404)
    return f


@admin.post("/products/<int:pid>/fotos/<int:fid>/principal")
@login_required
def foto_principal(pid, fid):
    f = _foto_do_produto(pid, fid)
    f.ordem = min([x.ordem for x in db.session.get(Product, pid).fotos], default=0) - 1
    db.session.commit()
    flash("Foto principal trocada.")
    return redirect(url_for("admin.edit_product", pid=pid))


@admin.post("/products/<int:pid>/fotos/<int:fid>/excluir")
@login_required
def foto_excluir(pid, fid):
    db.session.delete(_foto_do_produto(pid, fid))
    db.session.commit()
    flash("Foto removida.")
    return redirect(url_for("admin.edit_product", pid=pid))
