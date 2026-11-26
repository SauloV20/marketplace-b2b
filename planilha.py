"""Importar e exportar produtos (e exportar pedidos) em Excel (.xlsx) ou CSV.

Colunas reconhecidas (o nome pode ter acento, maiúsculas ou espaços):
  codigo, nome*, categoria*, preco*, estoque*, descricao, imagem, qtd_minima, multiplo,
  fracionavel, acrescimo_fracionado, faixas, ativo, peso_kg,
  preco_promocional, promo_inicio, promo_fim, ncm, cest,
  altura_cm, largura_cm, comprimento_cm                   (* obrigatórias para produto novo)
Para atualizar, a linha é ligada ao produto pelo código; sem código, pelo nome exato.
Células vazias num produto que já existe mantêm o valor atual. Para tirar a promoção, escreva "remover"
(ou 0) em preco_promocional; para tirar as medidas da embalagem (volta a caixa padrão do frete), "remover" na medida.
"""
import csv
import io
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from openpyxl import Workbook, load_workbook

from b2b import faixas_texto, gravar_faixas, ler_faixas
from formato import ler_numero
from models import Category, Order, Product, db

COLUNAS = ["codigo", "nome", "categoria", "preco", "estoque", "descricao", "imagem", "qtd_minima", "multiplo",
           "fracionavel", "acrescimo_fracionado", "faixas", "ativo", "peso_kg",
           "preco_promocional", "promo_inicio", "promo_fim", "ncm", "cest", "altura_cm", "largura_cm", "comprimento_cm"]
MEDIDAS = ("altura_cm", "largura_cm", "comprimento_cm")  # embalagem de uma unidade, para a cotação do frete
APELIDOS = {
    "sku": "codigo", "cod": "codigo", "referencia": "codigo", "ref": "codigo",
    "produto": "nome", "nome_do_produto": "nome",
    "valor": "preco", "preco_unitario": "preco", "preco_r": "preco",
    "quantidade": "estoque", "qtd": "estoque", "saldo": "estoque",
    "descricao_do_produto": "descricao", "url_da_imagem": "imagem", "imagem_url": "imagem", "image_url": "imagem",
    "minimo": "qtd_minima", "quantidade_minima": "qtd_minima", "qtd_min": "qtd_minima",
    "embalagem": "multiplo", "multiplos": "multiplo", "vende_em_multiplos_de": "multiplo",
    "fraciona": "fracionavel", "permite_fracionar": "fracionavel",
    "acrescimo": "acrescimo_fracionado", "acrescimo_fracionado_": "acrescimo_fracionado",
    "precos_por_quantidade": "faixas", "faixas_de_preco": "faixas", "preco_por_quantidade": "faixas",
    "status": "ativo", "peso": "peso_kg", "peso_kg_": "peso_kg", "peso_unitario": "peso_kg",
    "promocao": "preco_promocional", "preco_promocao": "preco_promocional", "preco_de_promocao": "preco_promocional",
    "preco_promo": "preco_promocional", "oferta": "preco_promocional", "preco_oferta": "preco_promocional",
    "inicio_promocao": "promo_inicio", "promocao_inicio": "promo_inicio", "inicio_da_promocao": "promo_inicio",
    "fim_promocao": "promo_fim", "promocao_fim": "promo_fim", "fim_da_promocao": "promo_fim",
    "promocao_ate": "promo_fim", "oferta_ate": "promo_fim",
    "altura": "altura_cm", "altura_da_embalagem": "altura_cm", "altura_cm_": "altura_cm",
    "largura": "largura_cm", "largura_da_embalagem": "largura_cm", "largura_cm_": "largura_cm",
    "comprimento": "comprimento_cm", "comprimento_da_embalagem": "comprimento_cm", "profundidade": "comprimento_cm",
    "comprimento_cm_": "comprimento_cm",
    "codigo_ncm": "ncm", "classificacao_fiscal": "ncm", "ncm_sh": "ncm", "codigo_cest": "cest",
}
REMOVER = ("remover", "tirar", "sem", "nao", "0", "-")
LIMITE_LINHAS = 5000


class ErroPlanilha(Exception):
    pass


# ---------- leitura ----------
def _chave(nome):
    t = unicodedata.normalize("NFKD", str(nome or "")).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "_", t).strip("_")
    return APELIDOS.get(t, t)


def ler_arquivo(conteudo, nome_arquivo):
    """Devolve [(número da linha na planilha, {coluna: valor})]."""
    if nome_arquivo.lower().endswith(".xlsx"):
        try:
            wb = load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
        except Exception as e:
            raise ErroPlanilha("Não foi possível abrir o arquivo do Excel. Salve como .xlsx e tente de novo.") from e
        linhas = list(wb.active.iter_rows(values_only=True))
    elif nome_arquivo.lower().endswith((".csv", ".txt")):
        for codificacao in ("utf-8-sig", "cp1252"):
            try:
                texto = conteudo.decode(codificacao)
                break
            except UnicodeDecodeError:
                continue
        primeira = texto.split("\n", 1)[0]
        separador = ";" if primeira.count(";") >= primeira.count(",") else ","
        linhas = list(csv.reader(io.StringIO(texto), delimiter=separador))
    else:
        raise ErroPlanilha("Envie um arquivo .xlsx (Excel) ou .csv.")
    linhas = [l for l in linhas if any(str(c).strip() for c in l if c is not None)]
    if not linhas:
        raise ErroPlanilha("A planilha está vazia.")
    cabecalho = [_chave(c) for c in linhas[0]]
    if "nome" not in cabecalho and "codigo" not in cabecalho:
        raise ErroPlanilha("Não encontrei a coluna “nome” (ou “codigo”) na primeira linha. "
                           "Baixe o modelo para ver o formato.")
    if len(linhas) - 1 > LIMITE_LINHAS:
        raise ErroPlanilha(f"A planilha tem mais de {LIMITE_LINHAS} produtos. Divida em arquivos menores.")
    resultado = []
    for n, linha in enumerate(linhas[1:], start=2):
        d = {}
        for col, valor in zip(cabecalho, linha):
            if col in COLUNAS:
                d[col] = valor.strip() if isinstance(valor, str) else valor
        resultado.append((n, d))
    return resultado


# ---------- conversões ----------
def _vazio(v):
    return v is None or (isinstance(v, str) and not v.strip())


def _inteiro(v):
    d = ler_numero(v)
    if d != d.to_integral_value():
        raise ValueError
    return int(d)


def _data(v):
    """Data da célula: data do Excel, 31/12/2026, 31/12/26 ou 2026-12-31."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    t = str(v).strip().split(" ")[0]
    for formato in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(t, formato).date()
        except ValueError:
            continue
    raise ValueError


def _br(d):
    return d.strftime("%d/%m/%Y") if d else ""


def _sim_nao(v):
    t = _chave(v)
    if t in ("sim", "s", "1", "true", "x", "yes", "ativo", "verdadeiro"):
        return True
    if t in ("nao", "n", "0", "false", "no", "inativo", "falso"):
        return False
    raise ValueError


# ---------- validação ----------
def validar(linhas):
    """Confere cada linha. Devolve (alterações, erros).

    alterações = [{"linha", "acao": "novo"|"atualizar", "pid", "dados": {...}}]; erros = [(linha, mensagem)]."""
    por_codigo = {p.codigo: p for p in Product.query.filter(Product.codigo.isnot(None), Product.codigo != "").all()}
    por_nome = {}
    for p in Product.query.all():
        por_nome.setdefault(p.name.strip().lower(), p)
    alteracoes, erros, vistos = [], [], set()
    for n, d in linhas:
        problemas = []
        codigo = "" if _vazio(d.get("codigo")) else str(d["codigo"]).strip()[:40]
        if isinstance(d.get("codigo"), float) and d["codigo"].is_integer():
            codigo = str(int(d["codigo"]))  # Excel transforma 123 em 123.0
        nome = "" if _vazio(d.get("nome")) else " ".join(str(d["nome"]).split())[:160]
        existente = por_codigo.get(codigo) if codigo else None
        if not existente and nome:
            existente = por_nome.get(nome.lower())
        chave_linha = codigo or nome.lower()
        if not chave_linha:
            erros.append((n, "Informe o nome ou o código do produto."))
            continue
        if chave_linha in vistos:
            erros.append((n, f"Produto repetido na planilha ({codigo or nome})."))
            continue
        vistos.add(chave_linha)
        dados = {}
        if codigo:
            dados["codigo"] = codigo
        if nome:
            dados["name"] = nome
        if not _vazio(d.get("categoria")):
            dados["categoria"] = " ".join(str(d["categoria"]).split())[:80]
        for col, campo, conv in (("preco", "price", ler_numero), ("estoque", "stock", _inteiro),
                                 ("qtd_minima", "qtd_minima", _inteiro), ("multiplo", "multiplo", _inteiro),
                                 ("acrescimo_fracionado", "acrescimo_fracionado", ler_numero),
                                 ("peso_kg", "peso_kg", ler_numero),
                                 ("fracionavel", "fracionavel", _sim_nao), ("ativo", "active", _sim_nao)):
            if _vazio(d.get(col)):
                continue
            try:
                dados[campo] = conv(d[col])
            except (ValueError, InvalidOperation, ArithmeticError):
                problemas.append(f"valor inválido em “{col}”: {d[col]}")
        for col, tamanho, nome_col in (("ncm", 8, "NCM"), ("cest", 7, "CEST")):  # dados fiscais da NF-e
            if not _vazio(d.get(col)):
                v = d[col]
                if isinstance(v, (int, float)) and not isinstance(v, bool):  # o Excel tira o zero da frente
                    numeros = str(int(v)).zfill(tamanho)
                else:
                    numeros = "".join(ch for ch in str(v) if ch.isdigit())
                if len(numeros) != tamanho:
                    problemas.append(f"o {nome_col} tem {tamanho} números: {d[col]}")
                dados[col] = numeros
        for col in MEDIDAS:  # medidas da embalagem (cm); "remover" volta para a caixa padrão do frete
            v = d.get(col)
            if _vazio(v):
                continue
            if (isinstance(v, str) and _chave(v) in REMOVER) or (isinstance(v, (int, float)) and v == 0):
                dados[col] = None
                continue
            try:
                medida = ler_numero(v)
            except (ValueError, InvalidOperation, ArithmeticError):
                problemas.append(f"valor inválido em “{col}”: {v}")
                continue
            if not medida.is_finite() or not 0 < medida <= 1000:
                problemas.append(f"“{col}” vai de 0,1 a 1.000 cm")
                continue
            dados[col] = medida.quantize(Decimal("0.1"))
        if not _vazio(d.get("descricao")):
            dados["description"] = str(d["descricao"]).strip()
        if not _vazio(d.get("imagem")):
            imagem = str(d["imagem"]).strip()
            if not imagem.startswith(("http://", "https://")) or len(imagem) > 500:
                problemas.append("a imagem precisa ser um link http:// ou https://")
            dados["image_url"] = imagem
        # promoção com data: "remover" (ou 0) em preco_promocional tira a promoção
        valor_promo = d.get("preco_promocional")
        remover_promo = not _vazio(valor_promo) and (_chave(valor_promo) in REMOVER or (
            isinstance(valor_promo, (int, float)) and valor_promo == 0))
        if remover_promo:
            dados.update(preco_promocional=None, promo_inicio=None, promo_fim=None)
        else:
            if not _vazio(d.get("preco_promocional")):
                try:
                    dados["preco_promocional"] = ler_numero(d["preco_promocional"])
                except (ValueError, InvalidOperation, ArithmeticError):
                    problemas.append(f"valor inválido em “preco_promocional”: {d['preco_promocional']}")
            for col in ("promo_inicio", "promo_fim"):
                if not _vazio(d.get(col)):
                    try:
                        dados[col] = _data(d[col])
                    except ValueError:
                        problemas.append(f"data inválida em “{col}”: {d[col]} (use 31/12/2026)")
        if not _vazio(d.get("faixas")):
            faixas, erro = ler_faixas(str(d["faixas"]).replace(";", "\n"))
            if erro:
                problemas.append(erro)
            dados["faixas"] = faixas
        # regras de valores (as mesmas do cadastro pelo painel)
        if "price" in dados and (not dados["price"].is_finite() or not 0 <= dados["price"] < 100_000_000):
            problemas.append("o preço precisa ser um valor entre 0 e 100.000.000")
        if dados.get("stock", 0) < 0:
            problemas.append("o estoque não pode ser negativo")
        for campo in ("qtd_minima", "multiplo"):
            if campo in dados and not 1 <= dados[campo] <= 100_000:
                problemas.append(f"“{campo}” vai de 1 a 100.000")
        if "peso_kg" in dados and not 0 <= dados["peso_kg"] < 100_000:
            problemas.append("o peso vai de 0 a 100.000 kg")
        if "acrescimo_fracionado" in dados and not 0 <= dados["acrescimo_fracionado"] <= 500:
            problemas.append("o acréscimo do fracionado vai de 0 a 500%")
        if dados.get("preco_promocional") is not None:
            preco_normal = dados.get("price", existente.price if existente else None)
            promo = dados["preco_promocional"]
            if not promo.is_finite() or promo <= 0 or (preco_normal is not None and promo >= preco_normal):
                problemas.append("o preço promocional precisa ser maior que zero e menor que o preço normal")
        elif ("promo_inicio" in dados or "promo_fim" in dados) and not remover_promo and not (
                existente and existente.preco_promocional):
            problemas.append("para a promoção, informe também o preco_promocional")
        inicio = dados.get("promo_inicio", existente.promo_inicio if existente and not remover_promo else None)
        fim = dados.get("promo_fim", existente.promo_fim if existente and not remover_promo else None)
        if inicio and fim and fim < inicio:
            problemas.append("a promoção termina antes de começar (promo_fim antes de promo_inicio)")
        if not existente:
            faltam = [c for c, campo in (("nome", "name"), ("categoria", "categoria"), ("preco", "price"),
                                         ("estoque", "stock")) if campo not in dados]
            if faltam:
                problemas.append("produto novo precisa de " + ", ".join(faltam))
        if codigo and existente and existente.codigo and existente.codigo != codigo:
            problemas.append(f"o nome já pertence ao produto de código {existente.codigo}")
        if problemas:
            erros.append((n, "; ".join(problemas)))
        else:
            alteracoes.append({"linha": n, "acao": "atualizar" if existente else "novo",
                               "pid": existente.id if existente else None, "dados": dados,
                               "nome": nome or existente.name})
    return alteracoes, erros


def aplicar(alteracoes, desativar_ausentes=False):
    """Grava as alterações validadas. Devolve (novos, atualizados, desativados, categorias criadas)."""
    categorias = {c.name.lower(): c for c in Category.query.all()}
    criadas, novos, atualizados, tocados = 0, 0, 0, set()
    for a in alteracoes:
        dados = dict(a["dados"])
        faixas = dados.pop("faixas", None)
        nome_cat = dados.pop("categoria", None)
        if nome_cat:
            cat = categorias.get(nome_cat.lower())
            if not cat:
                cat = Category(name=nome_cat)
                db.session.add(cat)
                db.session.flush()
                categorias[nome_cat.lower()] = cat
                criadas += 1
            dados["category_id"] = cat.id
        if a["acao"] == "novo":
            p = Product(**{"qtd_minima": 1, "multiplo": 1, "active": True, **dados})
            db.session.add(p)
            novos += 1
        else:
            p = db.session.get(Product, a["pid"])
            if p.preco_auto:  # preço calculado pelo custo (Formação de preço): a coluna de preço não vale
                dados.pop("price", None)
            for campo, valor in dados.items():
                setattr(p, campo, valor)
            atualizados += 1
        if faixas is not None:
            gravar_faixas(p, faixas)
        p.fracionavel = bool(p.fracionavel and (p.multiplo or 1) > 1)
        p.estoque_avulso = min(p.estoque_avulso or 0, p.stock or 0)
        if "price" in dados:
            p.price = dados["price"].quantize(Decimal("0.01"))
        if dados.get("preco_promocional") is not None:
            p.preco_promocional = dados["preco_promocional"].quantize(Decimal("0.01"))
        db.session.flush()
        tocados.add(p.id)
    desativados = 0
    if desativar_ausentes:
        for p in Product.query.filter(Product.active.is_(True)).all():
            if p.id not in tocados:
                p.active = False
                desativados += 1
    db.session.commit()
    return novos, atualizados, desativados, criadas


# ---------- exportação ----------
def arquivo_xlsx(wb, larguras=None):
    """Acabamento comum das planilhas exportadas e o .xlsx em bytes: cabeçalho em negrito e fixo, valores com duas
    casas e colunas na largura do conteúdo, ou nas `larguras` dadas ({"título da aba": [larguras]}).

    Segurança: o openpyxl grava como fórmula todo texto que começa com "=". Como nomes, endereços e descrições vêm
    de clientes, uma célula assim viraria fórmula executável no Excel de quem baixa (ex.: =HYPERLINK para um site
    falso). Por isso toda fórmula volta a ser texto: as planilhas da loja não usam fórmulas."""
    from openpyxl.styles import Font
    for aba in wb.worksheets:
        for linha in aba.iter_rows():
            for c in linha:
                if c.data_type == "f":
                    c.data_type = "s"
        aba.freeze_panes = "A2"
        for c in aba[1]:
            c.font = Font(bold=True)
        fixas = (larguras or {}).get(aba.title)
        for coluna in aba.columns:
            n = coluna[0].column
            largura = fixas[n - 1] if fixas and n <= len(fixas) else \
                min(max(max(len(str(c.value or "")) for c in coluna) + 2, 10), 50)
            aba.column_dimensions[coluna[0].column_letter].width = largura
        for linha in aba.iter_rows(min_row=2):
            for c in linha:
                if isinstance(c.value, float):
                    c.number_format = "#,##0.00"
    saida = io.BytesIO()
    wb.save(saida)
    return saida.getvalue()


def _xlsx(cabecalho, linhas, larguras=None):
    wb = Workbook()
    ws = wb.active
    ws.append(cabecalho)
    for linha in linhas:
        ws.append(linha)
    return arquivo_xlsx(wb, {ws.title: larguras} if larguras else None)


def _sn(v):
    return "sim" if v else "não"


def exportar_produtos():
    produtos = Product.query.order_by(Product.id).all()
    linhas = [[p.codigo or "", p.name, p.category.name, float(p.price), p.stock, p.description or "",
               p.image_url or "", p.qtd_minima, p.multiplo, _sn(p.fracionavel), float(p.acrescimo_fracionado or 0),
               faixas_texto(p).replace("\n", "; "), _sn(p.active), float(p.peso_kg or 0),
               float(p.preco_promocional) if p.preco_promocional else "", _br(p.promo_inicio), _br(p.promo_fim),
               p.ncm or "", p.cest or "", *[float(getattr(p, c)) if getattr(p, c) is not None else "" for c in MEDIDAS]]
              for p in produtos]
    if not linhas:  # modelo com um exemplo
        linhas = [["DISCO-7", "Disco de Desbaste 7 pol", "Abrasivos", 18.9, 100, "Caixa com 10", "", 1, 10, "sim",
                   10, "50 = 16,90; 100 = 14,90", "sim", 0.35, 16.9, "01/12/2026", "31/12/2026", "68042211", "", 5, 18, 18]]
    return _xlsx(COLUNAS, linhas, [14, 40, 20, 10, 10, 30, 30, 11, 10, 12, 20, 28, 8, 9, 18, 13, 13, 11, 10, 10, 11, 15])


def exportar_pedidos():
    pedidos = Order.query.order_by(Order.id.desc()).all()
    cab = ["pedido", "data", "status", "cliente", "documento", "razao_social", "email", "telefone", "endereco",
           "pagamento", "prazo", "cep", "frete", "cupom", "desconto", "total", "envio", "transportadora", "rastreio", "pagador_confere", "itens"]
    linhas = [[o.id, o.created_at.strftime("%d/%m/%Y %H:%M") if o.created_at else "", o.status, o.customer_name,
               o.documento or "", o.razao_social or "", o.customer_email, o.customer_phone, o.address,
               o.payment_method, o.prazo or "", o.cep or "", "a combinar" if o.frete_combinar else float(o.frete or 0),
               o.cupom or "", float(o.desconto or 0), float(o.total), o.envio or "", o.transportadora or "", o.rastreio or "",
               "" if o.pagador_confere is None else _sn(o.pagador_confere),
               "; ".join(f"{i.quantity}x {i.product.name}{' (fracionado)' if i.fracionado else ''}" for i in o.items)]
              for o in pedidos]
    return _xlsx(cab, linhas, [8, 16, 11, 28, 16, 30, 28, 16, 40, 11, 10, 10, 11, 12, 11, 11, 10, 16, 18, 14, 60])
