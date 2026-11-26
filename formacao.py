"""Formação de preço: custo do fornecedor, impostos do estado de entrega, transporte e markup.

    preço = custo × (1 + markup) ÷ (1 − impostos e despesas da venda)

custo   = compra no fornecedor principal + IPI + ICMS-ST + frete da compra − créditos de impostos da compra
          (frete da compra: % cobrado pelo fornecedor ou pela transportadora, ou a viagem do veículo próprio
          dividida pelo valor que cada viagem traz)
markup  = o do produto, senão o da categoria, senão o padrão da loja: o ganho sobre o custo, já sem os impostos
venda   = ICMS + adicional (DIFAL, FCP) do estado de entrega + impostos federais (ou o DAS do Simples)
          + despesas da venda (comissão, taxa de cartão) + custo da entrega, se o estado é atendido com frota própria
          + percentuais cadastrados na base "venda" que valem para o produto

Percentuais (tabela `percentual`): a loja cadastra cada imposto ou despesa com nome e %, para todos os produtos, uma
categoria ou um produto, e opcionalmente só para um estado. Base "venda" = % do preço (entra no divisor); base
"custo" = % do preço de compra (soma no custo). Com o mesmo nome em mais de um nível, vale o mais específico.

O preço de tabela (Product.price) de um produto com preço calculado é o preço para o estado da loja. Para outro
estado, a loja multiplica o preço (já com faixa, desconto da empresa e promoção) pelo fator
(1 − venda no estado da loja) ÷ (1 − venda no estado de entrega). Produto com preço manual não muda: o mesmo preço
vale para todo o Brasil.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from flask import g, has_app_context, has_request_context, request, session

import frete
from b2b import cnpj_valido, config, cpf_valido, salvar_config, so_digitos
from formato import decimal_ou, ler_numero
from models import Category, ImpostoUF, Percentual, Product, db

CENTAVO = Decimal("0.01")
CEM = Decimal(100)
MAX_VENDA = Decimal(90)  # impostos e despesas da venda somados (%): perto de 100% o preço explode
MAX_MARKUP = Decimal(1000)
SUL_SUDESTE = {"SP", "RJ", "MG", "PR", "SC", "RS"}  # daqui para N, NE, CO e ES o ICMS interestadual é 7%

TRANSPORTE_COMPRA = {"fornecedor": "O fornecedor entrega", "transportadora": "Transportadora paga pela loja",
                     "proprio": "Buscamos com veículo próprio"}
BASES = {"venda": "Sobre o preço de venda", "custo": "Sobre o preço de compra"}
ESCOPOS = {"todos": "Todos os produtos", "categoria": "Uma categoria", "produto": "Um produto"}
NIVEL = {"todos": 0, "categoria": 1, "produto": 2}
ENTREGA_VENDA = {"transportadora": "Transportadora (frete à parte)", "propria": "Frota própria (custo no preço)"}


# ---------- leitura de números do formulário ----------
def ler_pct(texto, rotulo, maximo=CEM, vazio=Decimal(0)):
    """"12,5" -> Decimal("12.50"). Vazio devolve `vazio`. Devolve (valor, erro)."""
    if not (texto or "").strip():
        return vazio, None
    try:
        v = ler_numero(texto)
    except InvalidOperation:
        return None, f"Confira {rotulo}: use um número, como 12 ou 4,5."
    if not 0 <= v <= maximo:
        return None, f"{rotulo[0].upper()}{rotulo[1:]} vai de 0 a {maximo}%."
    return v.quantize(CENTAVO), None


def ler_valor(texto, rotulo, maximo=Decimal(100_000_000), casas=CENTAVO):
    if not (texto or "").strip():
        return Decimal(0), None
    try:
        v = ler_numero(texto)
    except InvalidOperation:
        return None, f"Confira {rotulo}: use um valor como 16,50."
    if not 0 <= v < maximo:
        return None, f"{rotulo[0].upper()}{rotulo[1:]} precisa ficar entre 0 e {maximo:,.0f}.".replace(",", ".")
    return v.quantize(casas), None


# ---------- parâmetros da loja (lidos uma vez por requisição) ----------
def parametros():
    if has_app_context() and "_formacao" in g:
        return g._formacao
    p = {"loja": config("loja_uf", ""), "federais": decimal_ou(config("imp_federais")),
         "despesas": decimal_ou(config("despesas_venda")), "markup": decimal_ou(config("markup_padrao")),
         "ufs": {r.uf: {"icms": r.icms, "adicional": r.adicional, "entrega": r.entrega,
                        "custo_entrega": Decimal(r.custo_entrega or 0)} for r in ImpostoUF.query.all()},
         "percentuais": [{"id": x.id, "nome": x.nome, "chave": chave_nome(x.nome), "base": x.base,
                          "valor": Decimal(x.valor or 0), "escopo": x.escopo, "categoria": x.category_id,
                          "produto": x.product_id, "uf": x.uf or ""}
                         for x in Percentual.query.filter(Percentual.ativo.is_(True)).all()]}
    if has_app_context():
        g._formacao = p
    return p


def limpar_cache():
    if has_app_context():
        g.pop("_formacao", None)
        g.pop("_uf_cliente", None)


def _venda(linha, loja, federais, despesas):
    """Soma (em %) dos impostos e despesas da venda para um estado. Sem alíquota própria, vale a da loja."""
    icms = linha.get("icms") if linha.get("icms") is not None else (loja.get("icms") or 0)
    adicional = linha.get("adicional") if linha.get("adicional") is not None else (loja.get("adicional") or 0)
    entrega = linha.get("custo_entrega") or 0 if linha.get("entrega") == "propria" else 0
    return Decimal(federais) + Decimal(despesas) + Decimal(icms) + Decimal(adicional) + Decimal(entrega)


def chave_nome(nome):
    return " ".join((nome or "").split()).casefold()


def percentuais(produto=None, uf=None, base="venda", lista=None):
    """Percentuais cadastrados que valem para `produto` (None = só os de todos os produtos) entregando em `uf`
    (vazio = estado da loja). Com o mesmo nome em mais de um nível, fica o mais específico."""
    lista = parametros()["percentuais"] if lista is None else lista
    uf = uf or parametros()["loja"]
    escolhidos = {}
    for x in lista:
        if x["base"] != base:
            continue
        if x["escopo"] == "categoria" and not (produto is not None and produto.category_id == x["categoria"]):
            continue
        if x["escopo"] == "produto" and not (produto is not None and produto.id == x["produto"]):
            continue
        if x["uf"] and x["uf"] != uf:
            continue
        peso = (NIVEL.get(x["escopo"], 0), 1 if x["uf"] else 0)
        atual = escolhidos.get(x["chave"])
        if atual is None or peso > atual[0]:
            escolhidos[x["chave"]] = (peso, x)
    return sorted((x for _, x in escolhidos.values()), key=lambda x: x["nome"].casefold())


def soma_percentuais(produto=None, uf=None, base="venda", lista=None):
    return sum((x["valor"] for x in percentuais(produto, uf, base, lista)), Decimal(0))


def venda_pct(uf=None, produto=None, lista=None):
    """Impostos e despesas da venda (%) para entregar no estado `uf` (vazio = estado da loja), com os percentuais
    cadastrados que valem para `produto` (sem produto: só os de todos os produtos)."""
    p = parametros()
    base = _venda(p["ufs"].get(uf or p["loja"]) or {}, p["ufs"].get(p["loja"]) or {}, p["federais"], p["despesas"])
    return base + soma_percentuais(produto, uf, "venda", lista)


def fator(uf, produto=None):
    """Quanto o preço de tabela (estado da loja) muda para entregar em `uf`."""
    p = parametros()
    if not uf or not p["loja"] or uf == p["loja"]:
        return Decimal(1)
    casa, destino = venda_pct(p["loja"], produto), venda_pct(uf, produto)
    if casa >= CEM or destino >= CEM:
        return Decimal(1)
    return (CEM - casa) / (CEM - destino)


def entrega_propria(uf):
    return bool(uf) and (parametros()["ufs"].get(uf) or {}).get("entrega") == "propria"


def uf_do_cliente(cliente=None):
    """Estado de entrega do cliente: o CEP informado no carrinho, senão o do endereço principal."""
    cep = None
    if has_request_context() and request.blueprint != "admin":
        cep = frete.normalizar_cep(session.get("cep", ""))
    chave = (getattr(cliente, "id", None), cep)
    if has_app_context() and g.get("_uf_cliente", (None,))[0] == chave:
        return g._uf_cliente[1]
    if not cep and getattr(cliente, "id", None):
        from minha_conta import padrao
        e = padrao(cliente)
        cep = frete.normalizar_cep(e.cep) if e else None
    uf = frete.uf_do_cep(cep) if cep else None
    if has_app_context():
        g._uf_cliente = (chave, uf)
    return uf


def uf_do_pedido(o):
    cep = frete.normalizar_cep(o.cep or "")
    return frete.uf_do_cep(cep) if cep else ""


def preco_no_estado(p, preco, uf):
    """Preço de um produto com preço calculado para entregar em `uf` (manual: não muda)."""
    if not p.preco_auto:
        return preco
    return Decimal(preco) * fator(uf, p)


# ---------- custo ----------
def frete_compra_pct(f):
    if f.transporte == "proprio":
        return Decimal(f.custo_viagem or 0) * CEM / Decimal(f.compra_viagem) if f.compra_viagem else Decimal(0)
    return Decimal(f.frete_pct or 0)


def custo_de(c):
    """Custo de uma unidade comprada no fornecedor do item `c` (ProdutoFornecedor), parte por parte."""
    compra, f = Decimal(c.preco_compra), c.fornecedor
    extras = [{**x, "reais": compra * x["valor"] / CEM} for x in percentuais(c.product, base="custo")]
    partes = {"compra": compra, "ipi": compra * Decimal(c.ipi_pct or 0) / CEM,
              "st": compra * Decimal(c.st_pct or 0) / CEM, "frete": compra * frete_compra_pct(f) / CEM,
              "credito": compra * Decimal(f.credito_pct or 0) / CEM,
              "percentuais": sum((x["reais"] for x in extras), Decimal(0))}
    partes["total"] = (partes["compra"] + partes["ipi"] + partes["st"] + partes["frete"] + partes["percentuais"]
                       - partes["credito"])
    return {**partes, "percentuais_custo": extras, "fornecedor": f, "item": c}


def fonte(p):
    """O preço de compra usado no cálculo: o do fornecedor principal; sem principal, o de menor custo."""
    validos = [c for c in p.custos if c.fornecedor.ativo and c.preco_compra and c.preco_compra > 0]
    if not validos:
        return None
    return next((c for c in validos if c.principal), None) or min(validos, key=lambda c: custo_de(c)["total"])


def markup(p):
    if p.markup is not None:
        return Decimal(p.markup), "do produto"
    if p.category and p.category.markup is not None:
        return Decimal(p.category.markup), f"da categoria {p.category.name}"
    return parametros()["markup"], "padrão da loja"


def calcular(p, uf=None):
    """Composição do preço de `p` para entregar em `uf` (vazio = estado da loja). None sem preço de compra."""
    c = fonte(p)
    if not c:
        return None
    custo = custo_de(c)
    m, origem = markup(p)
    venda = venda_pct(uf, p)
    if venda >= CEM:
        return None
    preco = (custo["total"] * (CEM + m) / (CEM - venda)).quantize(CENTAVO, ROUND_HALF_UP)
    impostos = (preco * venda / CEM).quantize(CENTAVO)
    lucro = preco - impostos - custo["total"].quantize(CENTAVO)
    return {**custo, "markup": m, "markup_origem": origem, "venda_pct": venda, "impostos": impostos,
            "preco": preco, "lucro": lucro, "margem": (lucro * CEM / preco) if preco else Decimal(0),
            "uf": uf or parametros()["loja"],
            "percentuais_venda": [{**x, "reais": (preco * x["valor"] / CEM).quantize(CENTAVO)}
                                  for x in percentuais(p, uf, "venda")]}


def recalcular(ids=None):
    """Atualiza o preço de tabela dos produtos com preço calculado (as faixas acompanham na mesma proporção).

    Devolve (quantos mudaram, produtos sem preço de compra). Quem chama grava (commit)."""
    limpar_cache()
    db.session.flush()
    q = Product.query.filter(Product.preco_auto.is_(True))
    if ids is not None:
        ids = [i for i in ids if i]
        if not ids:
            return 0, []
        q = q.filter(Product.id.in_(ids))
    alterados, sem_custo = 0, []
    for p in q.all():
        r = calcular(p)
        if not r or not 0 < r["preco"] < 100_000_000:
            sem_custo.append(p)
            continue
        if r["preco"] != p.price:
            if p.price and p.price > 0:
                proporcao = r["preco"] / Decimal(p.price)
                for f in p.faixas:
                    f.preco = (Decimal(f.preco) * proporcao).quantize(CENTAVO, ROUND_HALF_UP)
            p.price = r["preco"]
            alterados += 1
    return alterados, sem_custo


def produtos_afetados(fornecedor=None, categoria=None, percentual=None):
    if percentual is not None:
        if percentual.escopo == "produto":
            return [percentual.product_id]
        if percentual.escopo == "categoria":
            return [p.id for p in Product.query.filter_by(category_id=percentual.category_id).all()]
        return None
    if fornecedor is not None:
        return [c.product_id for c in fornecedor.precos]
    if categoria is not None:
        return [p.id for p in categoria.products]
    return None


# ---------- formulários do painel ----------
def ler_fornecedor(f):
    """Valida o cadastro do fornecedor. Devolve (dados, erro)."""
    nome = " ".join(f.get("nome", "").split())[:160]
    if not nome:
        return None, "Informe o nome do fornecedor."
    doc = so_digitos(f.get("documento", ""))
    if doc and not ((len(doc) == 14 and cnpj_valido(doc)) or (len(doc) == 11 and cpf_valido(doc))):
        return None, "CNPJ (ou CPF) do fornecedor inválido. Confira os números ou deixe em branco."
    uf = f.get("uf", "").strip().upper()
    if uf and uf not in frete.UFS:
        return None, "Estado do fornecedor inválido."
    email = f.get("email", "").strip()[:120]
    if email and "@" not in email:
        return None, "E-mail do fornecedor inválido."
    transporte = f.get("transporte", "fornecedor")
    if transporte not in TRANSPORTE_COMPRA:
        return None, "Escolha como a mercadoria chega."
    dados = {}
    for campo, rotulo, leitor in (
            ("frete_pct", "o frete da compra", lambda t, r: ler_pct(t, r)),
            ("credito_pct", "os créditos de impostos", lambda t, r: ler_pct(t, r)),
            ("custo_viagem", "o custo da viagem", lambda t, r: ler_valor(t, r, Decimal(1_000_000))),
            ("compra_viagem", "o valor trazido por viagem", lambda t, r: ler_valor(t, r))):
        dados[campo], erro = leitor(f.get(campo, ""), rotulo)
        if erro:
            return None, erro
    if transporte == "proprio" and dados["custo_viagem"] and not dados["compra_viagem"]:
        return None, "Com veículo próprio, informe quanto cada viagem traz em mercadoria (para dividir o custo)."
    return {"nome": nome, "documento": doc, "uf": uf, "contato": " ".join(f.get("contato", "").split())[:120],
            "telefone": f.get("telefone", "").strip()[:30], "email": email,
            "observacoes": f.get("observacoes", "").strip()[:2000], "transporte": transporte,
            "ativo": "ativo" in f, **dados}, None


def ler_custo(f, sufixo=""):
    """Preço de compra, IPI, ST e código do fornecedor. Devolve (dados, erro)."""
    preco, erro = ler_valor(f.get(f"preco_compra{sufixo}", ""), "o preço de compra", casas=Decimal("0.0001"))
    if erro:
        return None, erro
    if not preco:
        return None, "Informe o preço de compra (maior que zero)."
    ipi, erro = ler_pct(f.get(f"ipi_pct{sufixo}", ""), "o IPI")
    if erro:
        return None, erro
    st, erro = ler_pct(f.get(f"st_pct{sufixo}", ""), "o ICMS-ST")
    if erro:
        return None, erro
    return {"preco_compra": preco, "ipi_pct": ipi, "st_pct": st,
            "codigo": f.get(f"codigo{sufixo}", "").strip()[:60]}, None


def ler_markup(texto, rotulo="o markup"):
    """Vazio = None (usa o da categoria ou o padrão)."""
    return ler_pct(texto, rotulo, MAX_MARKUP, vazio=None)


def ler_config(f):
    """Valida a tela de formação de preço. Devolve (dados, erro)."""
    loja = f.get("loja_uf", "").strip().upper()
    if loja and loja not in frete.UFS:
        return None, "Escolha o estado da loja."
    dados = {"loja_uf": loja}
    for campo, rotulo, maximo in (("imp_federais", "os impostos federais", CEM),
                                  ("despesas_venda", "as despesas da venda", CEM),
                                  ("markup_padrao", "o markup padrão", MAX_MARKUP)):
        dados[campo], erro = ler_pct(f.get(campo, ""), rotulo, maximo)
        if erro:
            return None, erro
    ufs = {}
    for uf in frete.UFS:
        icms, erro = ler_pct(f.get(f"icms_{uf}", ""), f"o ICMS de {uf}", vazio=None)
        adicional, erro2 = ler_pct(f.get(f"adicional_{uf}", ""), f"o adicional de {uf}", vazio=None)
        custo, erro3 = ler_pct(f.get(f"custo_entrega_{uf}", ""), f"o custo da entrega própria em {uf}")
        if erro or erro2 or erro3:
            return None, erro or erro2 or erro3
        entrega = f.get(f"entrega_{uf}", "transportadora")
        if entrega not in ENTREGA_VENDA:
            entrega = "transportadora"
        ufs[uf] = {"icms": icms, "adicional": adicional, "entrega": entrega, "custo_entrega": custo}
    loja_linha = ufs.get(loja, {})
    for uf, linha in ufs.items():
        total = _venda(linha, loja_linha, dados["imp_federais"], dados["despesas_venda"]) \
            + soma_percentuais(None, uf or None, "venda")
        if total >= MAX_VENDA:
            return None, (f"Em {uf}, impostos e despesas da venda somam {total}%. Acima de {MAX_VENDA}% o preço "
                          "não fecha: confira as porcentagens.")
    dados["ufs"] = ufs
    categorias = {}
    for c in Category.query.all():
        categorias[c.id], erro = ler_markup(f.get(f"markup_cat_{c.id}", ""), f"o markup de {c.name}")
        if erro:
            return None, erro
    dados["categorias"] = categorias
    return dados, None


def gravar_config(dados):
    for chave in ("loja_uf", "imp_federais", "despesas_venda", "markup_padrao"):
        salvar_config(chave, dados[chave])
    existentes = {r.uf: r for r in ImpostoUF.query.all()}
    for uf, linha in dados["ufs"].items():
        vazio = (linha["icms"] is None and linha["adicional"] is None and linha["entrega"] == "transportadora"
                 and not linha["custo_entrega"])
        r = existentes.get(uf)
        if vazio:
            if r:
                db.session.delete(r)
            continue
        r = r or ImpostoUF(uf=uf)
        for campo, valor in linha.items():
            setattr(r, campo, valor)
        db.session.add(r)
    for c in Category.query.all():
        c.markup = dados["categorias"].get(c.id)
    db.session.flush()
    limpar_cache()


def icms_interestadual(origem, destino):
    """Alíquota de ICMS entre estados: 7% do Sul/Sudeste (menos ES) para N, NE, CO e ES; 12% no resto."""
    return Decimal(7) if origem in SUL_SUDESTE and destino not in SUL_SUDESTE else Decimal(12)


def linhas_da_tela():
    """Os 27 estados com o que está gravado (para a tela de formação de preço)."""
    p = parametros()
    return [{"uf": uf, **(p["ufs"].get(uf) or {"icms": None, "adicional": None, "entrega": "transportadora",
                                               "custo_entrega": Decimal(0)}),
             "venda": venda_pct(uf), "fator": fator(uf)} for uf in frete.UFS]


# ---------- percentuais cadastrados pela loja ----------
def _como_dict(x):
    return {"id": x.id, "nome": x.nome, "chave": chave_nome(x.nome), "base": x.base, "valor": Decimal(x.valor or 0),
            "escopo": x.escopo, "categoria": x.category_id, "produto": x.product_id, "uf": x.uf or ""}


def ler_percentual(f, produto_fixo=None):
    """Valida o formulário de um percentual. `produto_fixo` = cadastro feito na tela de custo do produto.
    Devolve (dados, erro)."""
    nome = " ".join(f.get("nome", "").split())[:60]
    if not nome:
        return None, "Dê um nome ao percentual (ex.: PIS, COFINS, Comissão)."
    base = f.get("base", "venda")
    if base not in BASES:
        return None, "Escolha se o percentual é sobre o preço de venda ou sobre o preço de compra."
    valor, erro = ler_pct(f.get("valor", ""), f"o percentual de {nome}", vazio=None)
    if erro:
        return None, erro
    if valor is None:
        return None, f"Informe o percentual de {nome} (use 0 para zerar um percentual geral neste produto)."
    escopo = "produto" if produto_fixo is not None else f.get("escopo", "todos")
    if escopo not in ESCOPOS:
        return None, "Escolha para quais produtos o percentual vale."
    dados = {"nome": nome, "base": base, "valor": valor, "escopo": escopo, "category_id": None, "product_id": None,
             "ativo": produto_fixo is not None or "ativo" in f, "observacao": " ".join(f.get("observacao", "").split())[:200]}
    if escopo == "categoria":
        c = db.session.get(Category, f.get("category_id", type=int) or 0)
        if not c:
            return None, "Escolha a categoria."
        dados["category_id"] = c.id
    elif escopo == "produto":
        p = produto_fixo
        if p is None:
            from editar_pedido import _achar_produto
            p = _achar_produto(f.get("produto", ""))
        if not p:
            return None, "Produto não encontrado: digite o código ou o nome exato."
        dados["product_id"] = p.id
    uf = f.get("uf", "").strip().upper()
    if uf and uf not in frete.UFS:
        return None, "Estado inválido."
    if uf and base == "custo":
        return None, "Percentual sobre o preço de compra vale para todos os estados: deixe o estado em branco."
    dados["uf"] = uf
    return dados, None


def conferir_limite(ids=None):
    """Depois de mudar os percentuais (antes do commit): algum produto passa de MAX_VENDA% de impostos e despesas
    da venda em algum estado? Devolve a mensagem de erro ou None."""
    db.session.flush()
    limpar_cache()
    lista = [_como_dict(x) for x in Percentual.query.filter(Percentual.ativo.is_(True)).all()]
    q = Product.query
    if ids is not None:
        q = q.filter(Product.id.in_([i for i in ids if i] or [0]))
    produtos = [None] + q.all()
    for uf in frete.UFS:
        for p in produtos:
            total = venda_pct(uf, p, lista)
            if total >= MAX_VENDA:
                onde = f"{p.name}, entrega em {uf}" if p else f"entrega em {uf}"
                return (f"Com esse percentual, impostos e despesas da venda somam {total}% ({onde}). "
                        f"Acima de {MAX_VENDA}% o preço não fecha: confira as porcentagens.")
    return None
