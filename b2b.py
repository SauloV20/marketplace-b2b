"""Regras de venda para empresas: CPF/CNPJ, preço por quantidade, desconto do cliente, prazos e pedido mínimo."""
import re
from decimal import Decimal, InvalidOperation

from formato import duas_casas, hoje  # noqa: F401  (hoje: usado por vários módulos como b2b.hoje)
from models import Configuracao, FaixaPreco, db

# formas de pagamento da loja (o nome que aparece para o cliente, nos e-mails e no painel)
METODOS = {"pix": "Pix", "boleto": "Boleto à vista", "cartao": "Cartão de crédito", "whatsapp": "WhatsApp",
           "faturado": "Boleto faturado", "orcamento": "Orçamento"}

CENTAVO = Decimal("0.01")


# ---------- CPF / CNPJ ----------
def so_digitos(texto):
    return re.sub(r"\D", "", texto or "")


def _digito(numeros, pesos):
    resto = sum(int(n) * p for n, p in zip(numeros, pesos)) % 11
    return "0" if resto < 2 else str(11 - resto)


def cpf_valido(cpf):
    if len(cpf) != 11 or cpf == cpf[0] * 11 or not cpf.isdigit():
        return False
    d1 = _digito(cpf[:9], range(10, 1, -1))
    return cpf[9:] == d1 + _digito(cpf[:9] + d1, range(11, 1, -1))


def cnpj_valido(cnpj):
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14 or not cnpj.isdigit():
        return False
    pesos = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    d1 = _digito(cnpj[:12], pesos)
    return cnpj[12:] == d1 + _digito(cnpj[:12] + d1, [6] + pesos)


def formatar_documento(doc):
    if len(doc or "") == 11:
        return f"{doc[:3]}.{doc[3:6]}.{doc[6:9]}-{doc[9:]}"
    if len(doc or "") == 14:
        return f"{doc[:2]}.{doc[2:5]}.{doc[5:8]}/{doc[8:12]}-{doc[12:]}"
    return doc or ""


def ler_documento(tipo, texto):
    """Devolve (documento só com dígitos, erro)."""
    doc = so_digitos(texto)
    if tipo == "pj":
        return (doc, None) if cnpj_valido(doc) else (None, "CNPJ inválido. Confira os números.")
    return (doc, None) if cpf_valido(doc) else (None, "CPF inválido. Confira os números.")


# ---------- quantidade e preço ----------
def ajustar_qtd(p, qtd, estoque=None):
    """Leva a quantidade para a regra do produto (mínimo e múltiplo) sem passar do estoque. 0 = não dá para vender.

    Produto fracionável vende qualquer quantidade: o que não fecha uma embalagem sai fracionado.
    `estoque` troca o estoque da conta (ex.: ao alterar um pedido, o que ele já segura também está livre para ele)."""
    estoque = p.stock if estoque is None else estoque
    mult, minimo = 1 if p.fracionavel else max(p.multiplo or 1, 1), max(p.qtd_minima or 1, 1)
    q = max(int(qtd), minimo)
    q = -(-q // mult) * mult  # arredonda para cima até o múltiplo
    if q > estoque:
        q = estoque // mult * mult
    return q if q >= minimo else 0


def disponivel(p):
    return p.active and ajustar_qtd(p, 1) > 0


def tem_condicoes_de_empresa(cliente):
    return bool(cliente and cliente.tipo == "pj" and cliente.empresa_aprovada)


def promocao_ativa(p, dia=None):
    """A promoção do produto vale hoje? (preço promocional menor que o de tabela, dentro do período)"""
    if not p.preco_promocional or p.preco_promocional >= p.price:
        return False
    dia = dia or hoje()
    return not ((p.promo_inicio and dia < p.promo_inicio) or (p.promo_fim and dia > p.promo_fim))


def preco_unitario(p, qtd, cliente=None, uf=None):
    """Preço da faixa de quantidade e, para empresa aprovada, com o desconto dela.

    Em promoção, vale o menor entre esse preço e o promocional: os descontos não se somam.
    Produto com preço calculado (formação de preço) muda com o estado de entrega: `uf`, ou o do cliente
    (CEP do carrinho ou endereço principal) quando não informado; "" = estado da loja."""
    preco = p.price
    for f in sorted(p.faixas, key=lambda f: f.qtd_minima):
        if qtd >= f.qtd_minima:
            preco = f.preco
    if tem_condicoes_de_empresa(cliente) and cliente.desconto:
        preco = preco * (100 - Decimal(cliente.desconto)) / 100
    if promocao_ativa(p):
        preco = min(Decimal(preco), p.preco_promocional)
    if p.preco_auto:
        import formacao
        preco = formacao.preco_no_estado(p, preco, formacao.uf_do_cliente(cliente) if uf is None else uf)
    return Decimal(preco).quantize(CENTAVO)


def separar_fracionado(p, qtd):
    """(em embalagens fechadas, fracionadas). Ex.: caixa de 25, pedido de 57 -> (50, 7)."""
    m = max(p.multiplo or 1, 1)
    if not p.fracionavel or m == 1:
        return qtd, 0
    return qtd - qtd % m, qtd % m


def preco_fracionado(p, qtd, cliente=None, uf=None):
    """Preço de cada unidade fracionada: o da faixa (com desconto da empresa) mais o acréscimo do produto."""
    base = preco_unitario(p, qtd, cliente, uf)
    return (base * (100 + Decimal(p.acrescimo_fracionado or 0)) / 100).quantize(CENTAVO)


def ler_faixas(texto):
    """Lê linhas "10 = 16,50" (a partir de 10 unidades, R$ 16,50). Devolve ([(qtd, preço)], erro)."""
    faixas = []
    for linha in (texto or "").splitlines():
        if not linha.strip():
            continue
        partes = linha.split("=")
        try:
            qtd, preco = int(partes[0].strip()), Decimal(partes[1].strip().replace(".", "").replace(",", "."))
        except (IndexError, ValueError, InvalidOperation):
            return None, f"Faixa inválida: “{linha.strip()}”. Use o formato 10 = 16,50."
        if len(partes) != 2 or qtd < 2 or not preco.is_finite() or not 0 < preco < 100_000_000:
            return None, f"Faixa inválida: “{linha.strip()}”. A quantidade começa em 2 e o preço deve ser positivo."
        faixas.append((qtd, preco.quantize(CENTAVO)))
    if len({q for q, _ in faixas}) != len(faixas):
        return None, "Há duas faixas com a mesma quantidade."
    return sorted(faixas), None


def faixas_texto(p):
    return "\n".join(f"{f.qtd_minima} = {duas_casas(f.preco)}" for f in sorted(p.faixas, key=lambda f: f.qtd_minima))


def gravar_faixas(p, faixas):
    p.faixas.clear()
    for qtd, preco in faixas:
        p.faixas.append(FaixaPreco(qtd_minima=qtd, preco=preco))


# ---------- prazos de pagamento ----------
def ler_prazos(texto):
    """"28; 30/60/90" -> ["28", "30/60/90"]. Devolve (lista, erro)."""
    prazos = []
    for parte in re.split(r"[;,\n]", texto or ""):
        p = re.sub(r"\s+", "", parte)
        if not p:
            continue
        dias = p.split("/")
        if not all(d.isdigit() and 0 < int(d) <= 365 for d in dias) or len(dias) > 12:
            return None, f"Prazo inválido: “{parte.strip()}”. Use dias separados por barra, como 30/60/90."
        prazos.append("/".join(str(int(d)) for d in dias))
    return list(dict.fromkeys(prazos)), None


def prazos_do_cliente(cliente):
    if not tem_condicoes_de_empresa(cliente):
        return []
    return ler_prazos(cliente.prazos)[0] or []


# ---------- configurações da loja ----------
def config(chave, padrao=""):
    c = db.session.get(Configuracao, chave)
    return c.valor if c else padrao


def data_config(chave):
    """Data e hora guardada na configuração (texto ISO), ou None."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(config(chave, ""))
    except ValueError:
        return None


def salvar_config(chave, valor):
    c = db.session.get(Configuracao, chave) or Configuracao(chave=chave)
    c.valor = str(valor)
    db.session.add(c)


def pedido_minimo():
    try:
        return Decimal(config("pedido_minimo", "0"))
    except InvalidOperation:
        return Decimal(0)
