import json
import secrets
import time
from decimal import Decimal
from functools import wraps
from urllib.parse import quote

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)

from formato import brl
from b2b import (METODOS, ajustar_qtd, disponivel, formatar_documento, hoje, pedido_minimo, prazos_do_cliente, preco_fracionado,
                 promocao_ativa,
                 preco_unitario, separar_fracionado, tem_condicoes_de_empresa)
from models import (Banner, Category, Order, OrderItem, Product, ProdutoFoto, User, baixar_estoque, db,
                    mudar_status)
import avisos
import historico
import equipe_empresa
import analytics
import avise_me
import credito
import cupons
import ajuda
import financeiro
import formacao
import frete
import atendente
import banners
import pedido_rapido
import produtos
import seo
import efi
import lgpd
import minha_conta
from pix import pix_payload, pix_qr_svg

shop = Blueprint("shop", __name__)


def cliente_atual():
    user = db.session.get(User, session.get("cliente_id") or 0)
    if not user or user.is_admin:
        return None
    if session.get("membro_id"):  # usuário da equipe: se saiu da empresa, a sessão não vale mais
        membro = db.session.get(User, session["membro_id"])
        if not membro or membro.empresa_id != user.id:
            return None
    return user


def membro_atual(cliente=None):
    """Quem da equipe da empresa está comprando (None = o próprio titular)."""
    cliente = cliente or cliente_atual()
    m = db.session.get(User, session.get("membro_id") or 0) if cliente and session.get("membro_id") else None
    return m if m and m.empresa_id == cliente.id else None


def precisa_entrar(view):
    """Preços e carrinho só para quem entrou na conta."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        g.cliente = cliente_atual()
        if not g.cliente:
            flash("Entre na sua conta (ou crie uma, com CPF ou CNPJ) para ver preços e comprar.")
            return redirect(url_for("account.entrar"))
        return view(*args, **kwargs)
    return wrapper


def cart_lines(cliente):
    """Itens do carrinho (guardado na sessão) já ajustados ao estoque, ao mínimo e ao múltiplo de cada produto."""
    cart = session.get("cart", {})
    if not cart:
        return [], Decimal(0)
    prods = {p.id: p for p in Product.query.filter(
        Product.id.in_([int(i) for i in cart]), Product.active.is_(True)).all()}
    lines, total = [], Decimal(0)
    for pid, qty in cart.items():
        p = prods.get(int(pid))
        qty = ajustar_qtd(p, qty) if p else 0
        if not qty:
            continue
        unit = preco_unitario(p, qty, cliente)
        fechadas, frac = separar_fracionado(p, qty)  # o que não fecha uma embalagem sai fracionado, com acréscimo
        unit_frac = preco_fracionado(p, qty, cliente) if frac else unit
        sub = unit * fechadas + unit_frac * frac
        lines.append({"p": p, "qty": qty, "fechadas": fechadas, "frac": frac, "unit": unit, "unit_frac": unit_frac,
                      "sub": sub})
        total += sub
    return lines, total


def tabela_no_estado(p, cliente):
    """Preço de tabela que o cliente vê riscado ("de"): com preço calculado, o do estado de entrega dele."""
    if not p.preco_auto:
        return p.price
    return Decimal(formacao.preco_no_estado(p, p.price, formacao.uf_do_cliente(cliente))).quantize(Decimal("0.01"))


def uf_dos_precos(lines, cliente):
    """Estado de entrega usado nos preços calculados do carrinho (para avisar o cliente), ou None."""
    if not formacao.parametros()["loja"] or not any(l["p"].preco_auto for l in lines):
        return None
    return formacao.uf_do_cliente(cliente) or formacao.parametros()["loja"]


@shop.app_context_processor
def inject_cart():
    cliente = cliente_atual()
    return {"cart_count": sum(int(q) for q in session.get("cart", {}).values()) if cliente else 0,
            "cliente_logado": cliente, "fmt_doc": formatar_documento,
            "categorias_menu": Category.query.order_by(Category.name).all() if request.blueprint != "admin" else [],
            "link_produto": produtos.link, "foto_produto": produtos.foto_principal,
            "nao_indexar": seo.nao_indexar(), "site_base": seo.base(),
            "google_verificacao": current_app.config.get("GOOGLE_SITE_VERIFICATION", ""),
            "atendente_ligado": atendente.ligado() and request.blueprint != "admin",
            "perguntas_ajuda": ajuda.perguntas() if request.blueprint != "admin" and not atendente.ligado() else [],
            "formatar_ajuda": ajuda.formatar,
            "whatsapp_loja": current_app.config.get("WHATSAPP_NUMBER", ""),
            "ga_id": analytics.id_da_pagina(),
            "empresa_loja": lgpd.dados_empresa() if request.blueprint != "admin" else {},
            "em_oferta": promocao_ativa, "tabela_no_estado": lambda p: tabela_no_estado(p, cliente), "membro_logado": membro_atual(cliente) if cliente else None, "ha_ofertas": ha_ofertas() if request.blueprint != "admin" else False}


def ha_ofertas():
    """Existe produto em promoção hoje? (mostra "Ofertas" no menu da loja)"""
    dia = hoje()
    return db.session.query(Product.id).filter(
        Product.active.is_(True), Product.preco_promocional.isnot(None), Product.preco_promocional < Product.price,
        db.or_(Product.promo_inicio.is_(None), Product.promo_inicio <= dia),
        db.or_(Product.promo_fim.is_(None), Product.promo_fim >= dia)).first() is not None


ORDENS = {"": "Mais recentes", "nome": "Nome (A a Z)", "menor": "Menor preço", "maior": "Maior preço"}


def _reais(texto):
    t = (texto or "").replace("R$", "").strip()
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        v = Decimal(t)
    except Exception:
        return None
    return v if v.is_finite() and 0 <= v < 100_000_000 else None


@shop.route("/")
def index():
    q = " ".join(request.args.get("q", "").split())[:100]
    cat = request.args.get("cat", type=int)
    cliente = cliente_atual()
    ordem = request.args.get("ordem", "")
    if ordem not in ORDENS or (ordem in ("menor", "maior") and not cliente):  # preço só para quem entrou
        ordem = ""
    so_disponiveis = request.args.get("disp") == "1"
    so_ofertas = request.args.get("ofertas") == "1"
    preco_min = _reais(request.args.get("min")) if cliente else None
    preco_max = _reais(request.args.get("max")) if cliente else None
    query = Product.query.filter_by(active=True)
    if q:
        query = query.filter(db.or_(Product.name.ilike(f"%{q}%"), Product.codigo.ilike(f"%{q}%")))
    if cat:
        query = query.filter_by(category_id=cat)
    produtos = query.order_by(Product.created_at.desc(), Product.id.desc()).all()
    if so_disponiveis:
        produtos = [p for p in produtos if disponivel(p)]
    if so_ofertas:
        produtos = [p for p in produtos if promocao_ativa(p)]
    # o preço só sai do servidor para quem entrou: visitante não vê nem no código da página
    precos = {p.id: preco_unitario(p, max(p.qtd_minima, 1), cliente) for p in produtos} if cliente else {}
    if preco_min is not None:
        produtos = [p for p in produtos if precos[p.id] >= preco_min]
    if preco_max is not None:
        produtos = [p for p in produtos if precos[p.id] <= preco_max]
    if ordem == "nome":
        produtos.sort(key=lambda p: p.name.casefold())
    elif ordem in ("menor", "maior"):
        produtos.sort(key=lambda p: precos[p.id], reverse=ordem == "maior")
    faixas = ({p.id: [(f.qtd_minima, preco_unitario(p, f.qtd_minima, cliente)) for f in p.faixas] for p in produtos}
              if cliente else {})
    filtros = {k: v for k, v in (("ordem", ordem), ("disp", "1" if so_disponiveis else ""),
                                 ("min", request.args.get("min", "") if preco_min is not None else ""),
                                 ("max", request.args.get("max", "") if preco_max is not None else "")) if v}
    slides = []
    if so_ofertas:
        filtros["ofertas"] = "1"
    if not q and not cat and not filtros:
        ativos = Banner.query.filter_by(ativo=True).order_by(Banner.ordem, Banner.id).limit(10).all()
        slides = banners.montar(ativos, cliente, lambda p: preco_unitario(p, max(p.qtd_minima, 1), cliente))
    ordens = {k: v for k, v in ORDENS.items() if cliente or k not in ("menor", "maior")}
    return render_template("shop/index.html", q=q, cat=cat, products=produtos, precos=precos, faixas=faixas,
                           disponivel=disponivel, categories=Category.query.order_by(Category.name).all(),
                           empresa=tem_condicoes_de_empresa(cliente), slides=slides, filtros=filtros,
                           ordem=ordem, ordens=ordens, so_disponiveis=so_disponiveis, so_ofertas=so_ofertas)


@shop.get("/busca/sugestoes")
def busca_sugestoes():
    """Sugestões da caixa de busca (sem preço: visitante também usa). Nome, código e categoria, sem acento."""
    q = " ".join(request.args.get("q", "").split())[:60]
    if len(q) < 2:
        return jsonify(itens=[])
    termo = produtos.sem_acento(q)
    candidatos = (Product.query.filter_by(active=True)
                  .filter(db.or_(Product.name.ilike(f"%{q}%"), Product.codigo.ilike(f"%{q}%"))).limit(40).all())
    if len(candidatos) < 8:  # sem acento ("solda" acha "Sólda", "mascara" acha "Máscara") e pela categoria
        vistos = {p.id for p in candidatos}
        categorias = {c.id: produtos.sem_acento(c.name) for c in Category.query.all()}
        for p in Product.query.filter_by(active=True).all():
            if p.id not in vistos and (termo in produtos.sem_acento(p.name) or
                                       termo in produtos.sem_acento(p.codigo or "") or
                                       termo in categorias.get(p.category_id, "")):
                candidatos.append(p)
                if len(candidatos) >= 40:
                    break

    def relevancia(p):
        nome, cod = produtos.sem_acento(p.name), produtos.sem_acento(p.codigo or "")
        return (cod != termo, not cod.startswith(termo), not nome.startswith(termo),
                not any(w.startswith(termo) for w in nome.split()), not disponivel(p), nome)

    itens = [{"nome": p.name, "codigo": p.codigo or "", "categoria": p.category.name, "link": produtos.link(p),
              "foto": produtos.foto_principal(p) or "", "oferta": promocao_ativa(p)}
             for p in sorted(candidatos, key=relevancia)[:8]]
    resp = jsonify(itens=itens)
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


@shop.get("/banner/<int:bid>/imagem")
def banner_imagem(bid):
    b = db.get_or_404(Banner, bid)
    if not b.imagem:
        abort(404)
    resp = current_app.response_class(b.imagem, mimetype=b.imagem_tipo or "image/png")
    resp.headers["Cache-Control"] = "public, max-age=604800"  # o link muda (?v=) quando a imagem muda
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ---------- Página do produto ----------
@shop.get("/produto/<int:pid>")
@shop.get("/produto/<int:pid>-<nome>")
def produto(pid, nome=None):
    p = db.get_or_404(Product, pid)
    if not p.active:
        abort(404)
    if nome != produtos.slug(p.name):  # um link só por produto (bom para o Google e para compartilhar)
        return redirect(produtos.link(p), code=301)
    cliente = cliente_atual()
    minimo = ajustar_qtd(p, 1) or max(p.qtd_minima or 1, 1)
    preco, tabela, precos_rel = None, [], {}
    relacionados = produtos.relacionados(p)
    if cliente:  # o preço só sai do servidor para quem entrou
        preco = preco_unitario(p, minimo, cliente)
        inicios = [minimo] + [f.qtd_minima for f in p.faixas if f.qtd_minima > minimo]
        for i, de in enumerate(inicios):
            valor = preco_unitario(p, de, cliente)
            off = int(round((1 - valor / preco) * 100)) if preco else 0
            tabela.append((de, inicios[i + 1] - 1 if i + 1 < len(inicios) else None, valor, off))
        precos_rel = {r.id: preco_unitario(r, max(r.qtd_minima, 1), cliente) for r in relacionados}
    lista_fotos = produtos.fotos(p)
    import avaliacoes
    pessoa = (membro_atual(cliente) or cliente) if cliente else None
    nota_media, total_notas = avaliacoes.media(p.id)
    return render_template("shop/produto.html", p=p, preco=preco, tabela=tabela, minimo=minimo,
                           avaliacoes=avaliacoes.publicadas(p.id), nota_media=nota_media, total_notas=total_notas,
                           pode_avaliar=avaliacoes.comprou(cliente, p.id), minha_avaliacao=avaliacoes.da_pessoa(pessoa, p.id),
                           passo=1 if p.fracionavel else max(p.multiplo or 1, 1), pode=disponivel(p),
                           lista_fotos=lista_fotos, relacionados=relacionados, precos_rel=precos_rel,
                           empresa=tem_condicoes_de_empresa(cliente), canonico=produtos.link(p, externo=True),
                           aviso_ativo=bool(avise_me.pendente(cliente, p)),
                           listas_cliente=cliente.listas if cliente else [],
                           foto_og=(seo.base() + lista_fotos[0]) if lista_fotos and
                           lista_fotos[0].startswith("/") else (lista_fotos[0] if lista_fotos else None))


@shop.post("/produto/<int:pid>/avaliar")
@precisa_entrar
def avaliar(pid):
    import avaliacoes
    p = db.get_or_404(Product, pid)
    try:
        avaliacoes.salvar(g.cliente, membro_atual(g.cliente) or g.cliente, p, request.form.get("nota"),
                          request.form.get("comentario"))
        flash("Obrigado! Sua avaliação aparece aqui depois de aprovada pelo Marketplace B2B.")
    except avaliacoes.ErroAvaliacao as e:
        flash(str(e))
    return redirect(produtos.link(p) + "#avaliacoes")


@shop.post("/produto/<int:pid>/avise-me")
@precisa_entrar
def avise_me_alternar(pid):
    p = db.get_or_404(Product, pid)
    if not p.active:
        abort(404)
    if avise_me.alternar(g.cliente, p):
        flash(f"Combinado: avisamos por e-mail ({g.cliente.email}) quando {p.name} voltar ao estoque.")
    else:
        flash("Aviso cancelado.")
    voltar = request.form.get("voltar", "")
    return redirect(voltar if voltar == url_for("account.conta") else produtos.link(p))


@shop.get("/produto/foto/<int:fid>")
def produto_foto(fid):
    f = db.get_or_404(ProdutoFoto, fid)
    resp = current_app.response_class(f.dados, mimetype=f.tipo)
    resp.headers["Cache-Control"] = "public, max-age=604800"  # o link muda (?v=) quando a foto muda
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ---------- Atendente virtual (IA) ----------
@shop.post("/atendente")
def atendente_responder():
    if not atendente.ligado():
        return jsonify(ok=False, msg="O atendimento virtual está desligado."), 404
    try:
        historico = json.loads(request.form.get("historico") or "[]")
    except ValueError:
        historico = []
    if not isinstance(historico, list) or not historico or len(str(historico[-1].get("content", ""))) > \
            atendente.MAX_MENSAGEM:
        return jsonify(ok=False, msg=f"Escreva sua pergunta em até {atendente.MAX_MENSAGEM} caracteres."), 400
    session.setdefault("chat_id", secrets.token_hex(8))
    if not atendente.pode_conversar(request.remote_addr, session["chat_id"]):
        return jsonify(ok=False, msg="Você enviou muitas mensagens seguidas. Tente de novo mais tarde ou fale "
                                      "com a nossa equipe."), 429
    try:
        texto = atendente.responder(historico, cliente_atual())
    except ValueError:
        return jsonify(ok=False, msg="Escreva sua pergunta."), 400
    except atendente.AtendenteIndisponivel:
        return jsonify(ok=False, msg="O atendimento virtual está indisponível agora. Tente de novo em instantes "
                                      "ou fale com a nossa equipe."), 503
    return jsonify(ok=True, resposta=texto)


# ---------- Google ----------
@shop.get("/robots.txt")
def robots_txt():
    return current_app.response_class(seo.robots(), mimetype="text/plain")


@shop.get("/sitemap.xml")
def sitemap_xml():
    resp = current_app.response_class(seo.sitemap(), mimetype="application/xml")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# ---------- Páginas legais (LGPD) ----------
@shop.get("/privacidade")
def privacidade():
    return render_template("shop/privacidade.html", e=lgpd.dados_empresa(), operadores=lgpd.operadores(),
                           versao=lgpd.VERSAO_TERMOS, ga_loja=bool(analytics.medicao_id()))


@shop.get("/termos")
def termos():
    return render_template("shop/termos.html", e=lgpd.dados_empresa(), versao=lgpd.VERSAO_TERMOS,
                           sms=bool(current_app.config.get("TWILIO_VERIFY_SID")))


# ---------- Carrinho ----------
@shop.post("/carrinho/add")
def cart_add():
    if not cliente_atual():
        return jsonify(ok=False, login=url_for("account.entrar"),
                       msg="Entre na sua conta para comprar."), 401
    pid = request.form.get("pid", type=int)
    qty = max(request.form.get("qty", 1, type=int), 1)
    p = db.session.get(Product, pid) if pid else None
    if not p or not disponivel(p):
        return jsonify(ok=False, msg="Produto indisponível."), 400
    cart = dict(session.get("cart", {}))
    nova = ajustar_qtd(p, int(cart.get(str(p.id), 0)) + qty)
    if not nova or nova == int(cart.get(str(p.id), 0)):
        return jsonify(ok=False, msg="Não há estoque para adicionar mais deste produto."), 400
    cart[str(p.id)] = nova
    session["cart"] = cart
    regra = ""
    if p.multiplo > 1:
        regra = (f" (embalagem com {p.multiplo}; fora dela sai fracionado)" if p.fracionavel
                 else f" (vendido em múltiplos de {p.multiplo})")
    return jsonify(ok=True, count=sum(cart.values()), msg=f"{p.name}: {nova} un no carrinho{regra}.")


def opcoes_frete(cep, total, lines):
    """Opções de entrega do carrinho para o CEP (tabela própria e Melhor Envio), da mais barata para a mais cara."""
    if not cep or not lines:
        return []
    import melhor_envio
    return frete.opcoes(cep, total, frete.peso_das_linhas(lines), melhor_envio.itens_das_linhas(lines))


@shop.get("/carrinho")
@precisa_entrar
def cart():
    lines, total = cart_lines(g.cliente)
    cep = session.get("cep", "")
    opcoes = opcoes_frete(cep, total, lines)
    calculo = frete.escolher(opcoes, session.get("frete_opcao"))
    cupom, desconto, erro_cupom = cupons.do_carrinho(session, g.cliente, total, lines)
    return render_template("shop/cart.html", lines=lines, total=total, minimo=pedido_minimo(), cep=cep,
                           uf_precos=uf_dos_precos(lines, g.cliente),
                           calculo=calculo, opcoes=opcoes, fmt_cep=frete.formatar_cep, cupom=cupom,
                           desconto=desconto, erro_cupom=erro_cupom, codigo_cupom=session.get("cupom", ""),
                           descricao_cupom=cupons.descricao)


@shop.post("/carrinho/cupom")
@precisa_entrar
def cart_cupom():
    if request.form.get("acao") == "remover":
        session.pop("cupom", None)
        return redirect(url_for("shop.cart"))
    lines, total = cart_lines(g.cliente)
    cupom, _, erro = cupons.validar(request.form.get("codigo", ""), g.cliente, total, lines)
    if cupom:
        session["cupom"] = cupom.codigo
        flash(f"Cupom {cupom.codigo} aplicado: {cupons.descricao(cupom)}.")
    else:
        flash(erro)
    return redirect(url_for("shop.cart"))


@shop.post("/carrinho/cep")
@precisa_entrar
def cart_cep():
    cep = frete.normalizar_cep(request.form.get("cep", ""))
    if cep:
        session["cep"] = cep
    else:
        flash("Informe um CEP válido, com 8 números.")
    return redirect(url_for("shop.cart"))


@shop.get("/carrinho/frete")
@precisa_entrar
def cart_frete():
    """Usado pela página de finalização quando o cliente muda o CEP.

    Se o estado muda o preço dos produtos (formação de preço), devolve também o resumo dos itens refeito."""
    cep = frete.normalizar_cep(request.args.get("cep", ""))
    if not cep:
        return jsonify(ok=False, msg="CEP inválido.")
    antes = cart_lines(g.cliente)[1]
    session["cep"] = cep
    lines, total = cart_lines(g.cliente)
    opcoes = opcoes_frete(cep, total, lines)
    c = frete.escolher(opcoes, request.args.get("opcao") or session.get("frete_opcao"))
    valor = c["valor"] if c else Decimal(0)
    cupom, desconto, _ = cupons.do_carrinho(session, g.cliente, total, lines)
    resumo = render_template("shop/_resumo_itens.html", lines=lines, total=total, cupom=cupom, desconto=desconto,
                             uf_precos=uf_dos_precos(lines, g.cliente)) if total != antes else ""
    escolha = render_template("shop/_frete_opcoes.html", opcoes=opcoes, calculo=c, base=total - desconto)
    return jsonify(ok=True, combinar=c is None, valor=brl(valor), gratis=bool(c and c["gratis"]),
                   prazo=c["prazo"] if c else "", total=brl(total - desconto + valor), resumo=resumo,
                   opcoes=escolha, codigo=c["codigo"] if c else "")


def _mudar_quantidade(cart, pid, qty, p):
    """Troca a quantidade de um item do carrinho (0 tira). Devolve o aviso de ajuste, se houve."""
    if qty <= 0 or not p:
        cart.pop(str(pid), None)
        return None
    if str(pid) not in cart:
        return None
    nova = ajustar_qtd(p, qty)
    aviso = None
    if nova != qty:
        multiplo = "" if p.fracionavel else f"múltiplos de {p.multiplo}, "
        aviso = (f"{p.name}: quantidade ajustada para {nova} un (mínimo {p.qtd_minima}, {multiplo}"
                 f"estoque {p.stock})." if nova else f"{p.name} saiu do carrinho: sem estoque suficiente.")
    if nova:
        cart[str(pid)] = nova
    else:
        cart.pop(str(pid), None)
    return aviso


@shop.post("/carrinho/update")
@precisa_entrar
def cart_update():
    """Um item só (usado pelo botão Remover)."""
    pid, qty = request.form.get("pid", type=int), request.form.get("qty", 0, type=int)
    cart = dict(session.get("cart", {}))
    aviso = _mudar_quantidade(cart, pid, qty, db.session.get(Product, pid) if pid else None)
    if aviso:
        flash(aviso)
    session["cart"] = cart
    return redirect(url_for("shop.cart"))


@shop.post("/carrinho/esvaziar")
@precisa_entrar
def cart_esvaziar():
    session["cart"] = {}
    session.pop("cupom", None)
    flash("Carrinho esvaziado.")
    return redirect(url_for("shop.cart"))


@shop.post("/carrinho/atualizar")
@precisa_entrar
def cart_atualizar():
    """Todas as quantidades de uma vez (botão Atualizar carrinho)."""
    cart = dict(session.get("cart", {}))
    prods = {p.id: p for p in Product.query.filter(Product.id.in_([int(i) for i in cart]))} if cart else {}
    avisos, mudou = [], False
    for pid in list(cart):
        qty = request.form.get(f"qty_{pid}", type=int)
        if qty is None or qty == int(cart[pid]):
            continue
        mudou = True
        aviso = _mudar_quantidade(cart, int(pid), qty, prods.get(int(pid)))
        if aviso:
            avisos.append(aviso)
    session["cart"] = cart
    for aviso in avisos:
        flash(aviso)
    if mudou and not avisos:
        flash("Carrinho atualizado.")
    return redirect(url_for("shop.cart"))


# ---------- Pedido rápido por código ----------
@shop.route("/pedido-rapido", methods=["GET", "POST"])
@precisa_entrar
def rapido():
    texto, linhas = request.form.get("lista", ""), []
    if request.form.get("codigo"):  # caixa "Adicionar por código" do carrinho: um produto por vez
        texto = f"{request.form['codigo'].strip()}	{request.form.get('qtd', '1').strip() or '1'}"
    if request.method == "POST":
        linhas, erro = pedido_rapido.interpretar(texto, g.cliente)
        if erro:
            flash(erro)
        elif request.form.get("acao") == "adicionar":
            cart = dict(session.get("cart", {}))
            adicionados = 0
            for l in linhas:
                if l["ok"]:
                    p = l["produto"]
                    nova = ajustar_qtd(p, int(cart.get(str(p.id), 0)) + l["qtd"])
                    if nova:
                        cart[str(p.id)] = nova
                        adicionados += 1
            session["cart"] = cart
            falhas = sum(1 for l in linhas if not l["ok"])
            flash(f"{adicionados} produto(s) adicionado(s) ao carrinho."
                  + (f" {falhas} linha(s) ficaram de fora: confira abaixo." if falhas else ""))
            if not falhas:
                return redirect(url_for("shop.cart"))
    total = sum((l["sub"] for l in linhas if l.get("ok")), Decimal(0))
    return render_template("shop/pedido_rapido.html", texto=texto, linhas=linhas, total=total,
                           conferido=request.method == "POST")


# ---------- Checkout ----------
def metodos_do_cliente(cliente):
    m = {k: v for k, v in METODOS.items() if k != "faturado"}
    if prazos_do_cliente(cliente):
        m = {"faturado": METODOS["faturado"], **m}
    return m


@shop.route("/checkout", methods=["GET", "POST"])
@precisa_entrar
def checkout():
    cliente = g.cliente
    precos_mudaram = None
    if request.method == "POST":  # o preço calculado muda com o estado: vale o CEP de entrega do pedido
        cep_post = frete.normalizar_cep(request.form.get("cep", ""))
        if cep_post and cep_post != session.get("cep"):
            antes = cart_lines(cliente)[1]
            session["cep"] = cep_post
            if cart_lines(cliente)[1] != antes:
                precos_mudaram = frete.uf_do_cep(cep_post)
    lines, total = cart_lines(cliente)
    if not lines:
        flash("Seu carrinho está vazio.")
        return redirect(url_for("shop.index"))
    if not cliente.documento:
        flash("Informe seu CPF ou CNPJ em Meus dados para finalizar (ele vai na nota fiscal).")
        return redirect(url_for("account.dados"))
    minimo = pedido_minimo()
    if total < minimo:
        flash(f"O pedido mínimo é de {brl(minimo)}. Faltam {brl(minimo - total)}.")
        return redirect(url_for("shop.cart"))
    cupom, desconto, erro_cupom = cupons.do_carrinho(session, cliente, total, lines)
    if erro_cupom:  # o cupom deixou de valer (venceu, esgotou, o carrinho mudou): o cliente decide no carrinho
        flash(erro_cupom + " Remova o cupom ou ajuste o carrinho para continuar.")
        return redirect(url_for("shop.cart"))
    metodos, prazos = metodos_do_cliente(cliente), prazos_do_cliente(cliente)
    cep_form = request.form.get("cep", "") if request.method == "POST" else session.get("cep", "")
    opcoes = opcoes_frete(frete.normalizar_cep(cep_form), total, lines)
    codigo_frete = request.form.get("frete_opcao", "") if request.method == "POST" else session.get("frete_opcao", "")
    calculo = frete.escolher(opcoes, codigo_frete)
    valor_frete = calculo["valor"] if calculo else Decimal(0)
    ctx = dict(lines=lines, total=total, metodos=metodos, prazos=prazos, cliente=cliente, calculo=calculo,
               opcoes=opcoes, base=total - desconto,
               uf_precos=uf_dos_precos(lines, cliente),
               valor_frete=valor_frete, cep_ok=bool(frete.normalizar_cep(cep_form)), cupom=cupom, desconto=desconto,
               credito_livre=credito.disponivel(cliente) if "faturado" in metodos else None,
               faturado_cabe=credito.cabe(cliente, total - desconto + valor_frete) if "faturado" in metodos else True,
               faturado_atraso=credito.bloqueado_por_atraso(cliente) if "faturado" in metodos else False)
    if request.method == "POST":
        f = request.form
        d = {k: " ".join(f.get(k, "").split()) for k in ("name", "email", "phone", "address")}  # uma linha só
        cep = frete.normalizar_cep(f.get("cep", ""))
        metodo, prazo = f.get("payment"), f.get("prazo", "") if f.get("payment") == "faturado" else ""
        if not all(d.values()) or not cep or metodo not in metodos or (metodo == "faturado" and prazo not in prazos):
            flash("Preencha todos os campos (com um CEP válido) e escolha a forma de pagamento"
                  + (" e o prazo." if metodo == "faturado" else "."))
            return render_template("shop/checkout.html", f=f, **ctx)
        if codigo_frete and opcoes and calculo["codigo"] != codigo_frete:  # a opção escolhida sumiu da cotação
            flash("As opções de frete para este CEP mudaram. Escolha a entrega de novo e confirme o pedido.")
            return render_template("shop/checkout.html", f=f, **ctx)
        if precos_mudaram:  # o cliente ainda não viu os preços deste estado: mostra antes de gravar
            flash(f"Os preços foram atualizados para entrega em {precos_mudaram}. Confira o resumo e confirme o pedido.")
            return render_template("shop/checkout.html", f=f, **ctx)
        if metodo == "faturado" and not credito.cabe(cliente, total - desconto + valor_frete):
            flash(credito.mensagem(cliente))
            return render_template("shop/checkout.html", f=f, **ctx)
        session["cep"] = cep
        if calculo:
            session["frete_opcao"] = calculo["codigo"]
        orcamento = metodo == "orcamento"
        membro = membro_atual(cliente)
        aprovacao = equipe_empresa.precisa_aprovacao(membro, total - desconto + valor_frete, metodo)
        order = Order(customer_name=d["name"][:120], customer_email=d["email"][:120],
                      customer_phone=d["phone"][:30], address=d["address"][:300], cep=cep,
                      frete=valor_frete, frete_prazo=(calculo or {}).get("prazo", "")[:60],
                      frete_combinar=calculo is None, frete_servico=frete.servico_do_pedido(calculo),
                      frete_codigo=(calculo or {}).get("codigo", "")[:20],
                      payment_method=metodo, prazo=prazo,
                      status="orcamento" if orcamento else ("aprovacao" if aprovacao else "pendente"),
                      comprador_id=membro.id if membro else None, comprador_nome=membro.name if membro else "",
                      cupom=cupom.codigo if cupom else "", desconto=desconto,
                      total=total - desconto + valor_frete, user_id=cliente.id, documento=cliente.documento,
                      razao_social=cliente.razao_social if cliente.tipo == "pj" else "",
                      inscricao_estadual=cliente.inscricao_estadual if cliente.tipo == "pj" else "")
        for l in lines:
            if l["fechadas"]:
                order.items.append(OrderItem(product_id=l["p"].id, quantity=l["fechadas"], unit_price=l["unit"]))
            if l["frac"]:
                order.items.append(OrderItem(product_id=l["p"].id, quantity=l["frac"], unit_price=l["unit_frac"],
                                             fracionado=True))
        import carrinho_abandonado
        carrinho_abandonado.pedido_fechado(order)
        db.session.add(order)
        db.session.flush()  # número do pedido para o histórico de estoque
        # orçamento não prende estoque: só baixa quando o Marketplace B2B aprovar
        sem_estoque = [] if orcamento or aprovacao else [
            l["p"].name for l in lines if not baixar_estoque(l["p"].id, l["qty"], l["frac"], "venda", order.id)]
        if sem_estoque:  # alguém comprou antes: nada é gravado
            db.session.rollback()
            flash("O estoque mudou enquanto você finalizava: " + ", ".join(sem_estoque)
                  + ". Confira o carrinho e tente de novo.")
            return redirect(url_for("shop.cart"))
        if "guardar_endereco" in f:
            novo = minha_conta.guardar_do_checkout(cliente, d["address"], cep)
            if novo:
                db.session.add(novo)
        financeiro.pedido_mudou(order)  # contas a receber (parcelas do faturado ou o valor à vista)
        cliente.phone = cliente.phone or d["phone"][:30]  # guarda no perfil se ainda estiver vazio
        cliente.address = cliente.address or d["address"][:300]
        db.session.commit()
        session["cart"] = {}
        session.pop("cupom", None)
        session["orders"] = (session.get("orders", []) + [order.id])[-20:]
        if aprovacao:  # pedido do comprador: espera o titular ou um aprovador da empresa (sem prender estoque)
            historico.registrar(order.id, f"Enviado para aprovação da empresa por {membro.name}", autor="cliente",
                                nome=membro.name)
            avisos.pedido_para_aprovar(order, equipe_empresa.aprovadores(cliente))
            flash("Pedido enviado para aprovação da sua empresa. Assim que for aprovado, ele segue para o Marketplace B2B.")
            return redirect(url_for("account.pedido_detalhe", oid=order.id))
        avisos.pedido_recebido(order)
        if metodo in ("boleto", "faturado") and not orcamento:
            import boleto_efi
            if boleto_efi.automatico() and boleto_efi.ligado():
                try:
                    boleto_efi.gerar_do_pedido(order)
                except Exception:  # sem boleto agora, a loja gera pelo painel; o pedido já está gravado
                    db.session.rollback()
                    current_app.logger.exception("Boleto automático do pedido %s", order.id)
        if metodo == "cartao" and not orcamento:
            import cartao_efi
            if cartao_efi.pode_gerar(order):
                try:
                    cartao_efi.gerar_link(order)
                except Exception:  # sem link agora, o cliente gera na página do pedido
                    db.session.rollback()
                    current_app.logger.exception("Link do cartão do pedido %s", order.id)
        if not orcamento:
            avisos.estoque_baixo([(l["p"], l["qty"]) for l in lines])
        return redirect(url_for("shop.pedido", oid=order.id))
    # endereço sugerido: o salvo com o CEP do carrinho; sem CEP no carrinho, o principal
    principal, cep_sessao = minha_conta.padrao(cliente), session.get("cep", "")
    escolhido = next((e for e in cliente.enderecos if e.cep == cep_sessao), None) if cep_sessao else principal
    quem = membro_atual(cliente) or cliente  # usuário da equipe: o contato do pedido é ele
    f = {"name": quem.name, "email": quem.email, "phone": quem.phone or cliente.phone or "",
         "address": escolhido.endereco if escolhido else cliente.address or "",
         "cep": frete.formatar_cep(cep_sessao or (principal.cep if principal else ""))}
    if principal and not cep_sessao:  # o frete já sai calculado para o endereço principal
        opcoes = opcoes_frete(principal.cep, total, lines)
        calculo = frete.escolher(opcoes)
        ctx.update(calculo=calculo, opcoes=opcoes, valor_frete=calculo["valor"] if calculo else Decimal(0),
                   cep_ok=True)
    return render_template("shop/checkout.html", f=f, **ctx)


def pedido_visivel(oid):
    o = db.get_or_404(Order, oid)
    cliente = cliente_atual()
    if oid not in session.get("orders", []) and not (cliente and o.user_id == cliente.id):
        abort(404)
    return o


def cobranca_efi(o):
    """Cria (uma vez) a cobrança Pix na Efí. Devolve o copia e cola, ou None se não der (cai no QR estático)."""
    if not o.pix_txid:
        try:
            o.pix_txid, o.pix_copia_cola = efi.criar_cobranca(o)
            db.session.commit()
        except efi.ErroEfi as e:
            db.session.rollback()
            current_app.logger.error("Pix Efí do pedido %s: %s", o.id, e)
            return None
    return o.pix_copia_cola


_ultima_consulta = {}  # pedido -> quando consultou a Efí (no máximo a cada 5 s)


def conferir_pix(o, forcar=False):
    """Pergunta à Efí se o Pix foi pago. Se foi: marca como pago e confere o pagador com o cadastro.

    Devolve True se o pedido está pago. Levanta efi.ErroEfi se a Efí não responder."""
    if o.status == "pago":
        return True
    if o.status != "pendente" or not o.pix_txid:
        return False
    if not forcar and time.time() - _ultima_consulta.get(o.id, 0) < 5:
        return False
    _ultima_consulta[o.id] = time.time()
    pago, pagador = efi.consultar_cobranca(o.pix_txid)
    if not pago:
        return False
    cliente = db.session.get(User, o.user_id) if o.user_id else None
    nomes = [o.customer_name, o.razao_social] + ([cliente.name, cliente.razao_social] if cliente else [])
    o.pagador_nome, o.pagador_documento = (pagador or {}).get("nome", ""), (pagador or {}).get("documento", "")
    o.pagador_confere = efi.pagador_confere(pagador, o.documento, nomes)
    ok, _ = mudar_status(o, "pago")  # pendente -> pago não mexe no estoque
    if ok:
        avisos.pagamento_confirmado(o)
        historico.registrar(o.id, "Pix confirmado pela Efí" + (
            "" if o.pagador_confere else f" · pago por {o.pagador_nome or 'outra pessoa'} (diferente do cadastro)"),
            autor="sistema")
    if not o.pagador_confere:
        current_app.logger.warning("Pedido %s pago por %s (%s), diferente do cadastro.", o.id, o.pagador_nome,
                                   o.pagador_documento)
    return ok


@shop.get("/pedido/<int:oid>/pix")
def pedido_pix(oid):
    """Chamado pela página do pedido a cada poucos segundos até o Pix cair."""
    o = pedido_visivel(oid)
    try:
        pago = conferir_pix(o)
    except efi.ErroEfi as e:
        current_app.logger.warning("Consulta do Pix %s: %s", oid, e)
        pago = False
    return jsonify(pago=pago or o.status == "pago")


@shop.get("/pedido/<int:oid>")
def pedido(oid):
    o = pedido_visivel(oid)
    cfg = current_app.config
    ctx = {}
    code = cobranca_efi(o) if o.status == "pendente" and o.payment_method == "pix" and efi.ligado() else None
    if code:  # Pix da Efí: confirma sozinho quando cair
        ctx.update(pix_code=code, pix_qr=pix_qr_svg(code), pix_auto=True)
    elif o.status == "pendente" and o.payment_method == "pix" and cfg["PIX_KEY"]:
        code = pix_payload(cfg["PIX_KEY"], cfg["PIX_NAME"], cfg["PIX_CITY"], o.total, f"Marketplace B2B{o.id}")
        ctx.update(pix_code=code, pix_qr=pix_qr_svg(code))
    if o.payment_method in ("whatsapp", "orcamento") and cfg["WHATSAPP_NUMBER"]:
        itens = "\n".join(f"- {i.quantity}x {i.product.name}" for i in o.items)
        tipo = "o orçamento" if o.status == "orcamento" else "o pedido"
        msg = f"Olá! Fiz {tipo} #{o.id} no Marketplace B2B.\n{itens}\nTotal: {brl(o.total)}\nNome: {o.customer_name}"
        ctx["wa_link"] = f"https://wa.me/{cfg['WHATSAPP_NUMBER']}?text={quote(msg)}"
    if o.payment_method == "cartao":
        import cartao_efi
        ctx.update(cartao_ligado=cartao_efi.ligado(), cartao_novo=cartao_efi.pode_gerar(o),
                   cartao_situacao=cartao_efi.SITUACOES.get(o.cartao_situacao or "", ""))
    return render_template("shop/pedido.html", o=o, metodos=METODOS, ga_compra=analytics.compra(o), **ctx)


@shop.post("/pedido/<int:oid>/cartao")
def pedido_cartao(oid):
    """O cliente gera o link de pagamento com cartão (o primeiro, ou outro depois de recusado ou vencido)."""
    import cartao_efi
    o = pedido_visivel(oid)
    try:
        flash(cartao_efi.gerar_link(o, autor="cliente"))
    except cartao_efi.ErroCartao as e:
        db.session.rollback()
        current_app.logger.warning("Link do cartão do pedido %s: %s", oid, e)
        flash("Não foi possível gerar o link de pagamento agora. Tente de novo em alguns minutos ou fale com o Marketplace B2B.")
    return redirect(url_for("shop.pedido", oid=o.id))
