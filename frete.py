"""Frete por CEP: tabela própria e, se ligado, a cotação do Melhor Envio (Correios e transportadoras).

O cliente vê todas as opções que valem para o CEP (a regra da tabela e cada serviço do Melhor Envio) e escolhe uma;
sem escolha, vale a mais barata. Estado com entrega própria (formação de preço) tem só a entrega própria, grátis.

Regras cadastradas no painel: por faixa de CEP, por estado (UF) ou padrão (vale para o resto). A regra mais
específica vence: faixa de CEP > estado > padrão; empate fica com a de menor "ordem". Sem regra = frete a combinar.
Valor = valor fixo + valor por kg x peso do carrinho; grátis a partir de um valor de compra, se configurado.
"""
import re
from decimal import ROUND_UP, Decimal, InvalidOperation

from b2b import so_digitos
from models import RegraFrete

CENTAVO = Decimal("0.01")

# Faixas de CEP de cada estado (5 primeiros dígitos), dos Correios
FAIXAS_UF = [
    (1000, 19999, "SP"), (20000, 28999, "RJ"), (29000, 29999, "ES"), (30000, 39999, "MG"),
    (40000, 48999, "BA"), (49000, 49999, "SE"), (50000, 56999, "PE"), (57000, 57999, "AL"),
    (58000, 58999, "PB"), (59000, 59999, "RN"), (60000, 63999, "CE"), (64000, 64999, "PI"),
    (65000, 65999, "MA"), (66000, 68899, "PA"), (68900, 68999, "AP"), (69000, 69299, "AM"),
    (69300, 69399, "RR"), (69400, 69899, "AM"), (69900, 69999, "AC"), (70000, 72799, "DF"),
    (72800, 72999, "GO"), (73000, 73699, "DF"), (73700, 76799, "GO"), (76800, 76999, "RO"),
    (77000, 77999, "TO"), (78000, 78899, "MT"), (79000, 79999, "MS"), (80000, 87999, "PR"),
    (88000, 89999, "SC"), (90000, 99999, "RS"),
]
UFS = sorted({uf for _, _, uf in FAIXAS_UF})


def normalizar_cep(texto):
    d = so_digitos(texto)
    return d if len(d) == 8 and d != "00000000" else None


def formatar_cep(cep):
    return f"{cep[:5]}-{cep[5:]}" if cep and len(cep) == 8 else (cep or "")


def uf_do_cep(cep):
    prefixo = int(cep[:5])
    return next((uf for de, ate, uf in FAIXAS_UF if de <= prefixo <= ate), None)


def ler_ufs(texto):
    """"sp, rj MG" -> "MG,RJ,SP". Devolve (texto, erro)."""
    ufs = sorted({u.upper() for u in re.split(r"[\s,;/]+", texto or "") if u.strip()})
    errados = [u for u in ufs if u not in UFS]
    if errados:
        return None, "Estado inválido: " + ", ".join(errados) + ". Use as siglas, como SP, RJ, MG."
    return ",".join(ufs), None


def peso_das_linhas(lines):
    return sum((Decimal(l["p"].peso_kg or 0) * l["qty"] for l in lines), Decimal(0))


def escolher_regra(cep):
    uf = uf_do_cep(cep)
    melhor = None
    for r in RegraFrete.query.filter_by(ativo=True).order_by(RegraFrete.ordem, RegraFrete.id).all():
        if r.cep_de and r.cep_ate:
            nivel = 3 if r.cep_de <= int(cep) <= r.cep_ate else 0
        elif r.ufs:
            nivel = 2 if uf and uf in r.ufs.split(",") else 0
        else:
            nivel = 1
        if nivel and (melhor is None or nivel > melhor[0]):
            melhor = (nivel, r)
    return melhor[1] if melhor else None


def _pela_tabela(cep, subtotal, peso):
    r = escolher_regra(cep)
    if not r:
        return None
    if r.gratis_acima and subtotal >= r.gratis_acima:
        valor = Decimal(0)
    else:
        kg = Decimal(peso or 0).quantize(Decimal("1"), rounding=ROUND_UP) if r.valor_kg else Decimal(0)
        valor = (Decimal(r.valor or 0) + Decimal(r.valor_kg or 0) * kg).quantize(CENTAVO)
    return {"codigo": "tabela", "nome": "Entrega padrão", "valor": valor, "prazo": r.prazo or "", "regra": r.nome,
            "gratis": valor == 0, "uf": uf_do_cep(cep)}


def opcoes(cep, subtotal, peso, itens=None):
    """Todas as opções de entrega para o CEP, da mais barata para a mais cara. `itens` (melhor_envio.item) liga a
    cotação do Melhor Envio; sem eles, só a tabela própria. Lista vazia = frete a combinar."""
    cep = normalizar_cep(cep)
    if not cep:
        return []
    import formacao
    uf = uf_do_cep(cep)
    if formacao.entrega_propria(uf):  # frota própria: o custo já está no preço dos produtos
        r = escolher_regra(cep)
        return [{"codigo": "propria", "nome": "Entrega própria", "valor": Decimal(0),
                 "prazo": (r.prazo or "") if r else "", "regra": "Entrega própria", "gratis": True, "uf": uf,
                 "propria": True}]
    lista = []
    tabela = _pela_tabela(cep, subtotal, peso)
    if tabela:
        lista.append(tabela)
    if itens:
        import melhor_envio
        for o in melhor_envio.cotar(cep, itens):
            lista.append({**o, "regra": o["nome"], "gratis": o["valor"] == 0, "uf": uf})
    return sorted(lista, key=lambda o: (o["valor"], o["codigo"] != "tabela"))


def escolher(lista, codigo=None):
    """A opção escolhida pelo cliente; sem escolha (ou se ela sumiu), a mais barata. None = a combinar."""
    if not lista:
        return None
    return next((o for o in lista if codigo and o["codigo"] == codigo), lista[0])


def calcular(cep, subtotal, peso, itens=None, codigo=None):
    """A opção escolhida (ou a mais barata): {"codigo", "nome", "valor", "prazo", "regra", "gratis", "uf"},
    ou None (frete a combinar)."""
    return escolher(opcoes(cep, subtotal, peso, itens), codigo)


def servico_do_pedido(calculo):
    """Nome gravado no pedido: o do serviço cotado (ex.: "Correios SEDEX"); na tabela própria, nenhum (como antes)."""
    return calculo["nome"][:80] if calculo and calculo["codigo"].startswith("me:") else ""


def rotulo(o):
    """Texto do frete no pedido: "Correios SEDEX · 3 dias úteis"."""
    partes = [p for p in ((o.frete_servico or "") if o else "", (o.frete_prazo or "") if o else "") if p]
    return " · ".join(partes)


def ler_regra(f):
    """Valida o formulário do painel. Devolve (dados, erro)."""
    nome = " ".join(f.get("nome", "").split())[:80]
    if not nome:
        return None, "Dê um nome para a regra (ex.: Sudeste, Capital SP, Resto do Brasil)."
    ufs, erro = ler_ufs(f.get("ufs", ""))
    if erro:
        return None, erro
    cep_de, cep_ate = normalizar_cep(f.get("cep_de", "")), normalizar_cep(f.get("cep_ate", ""))
    if bool(f.get("cep_de", "").strip()) != bool(f.get("cep_ate", "").strip()) or \
            (f.get("cep_de", "").strip() and not (cep_de and cep_ate)):
        return None, "Para usar faixa de CEP, preencha os dois CEPs (de e até) com 8 números."
    if cep_de and int(cep_de) > int(cep_ate):
        return None, "O CEP inicial precisa ser menor que o final."
    valores = {}
    for campo, rotulo in (("valor", "valor fixo"), ("valor_kg", "valor por kg"), ("gratis_acima", "frete grátis acima de")):
        texto = f.get(campo, "").replace("R$", "").replace(" ", "")
        if "," in texto:
            texto = texto.replace(".", "").replace(",", ".")
        try:
            v = Decimal(texto) if texto else (None if campo == "gratis_acima" else Decimal(0))
        except InvalidOperation:
            return None, f"Confira o campo “{rotulo}”."
        if v is not None and (not v.is_finite() or not 0 <= v < 1_000_000):
            return None, f"O campo “{rotulo}” precisa ser um valor entre 0 e 1.000.000."
        valores[campo] = v.quantize(CENTAVO) if v is not None else None
    try:
        ordem = int(f.get("ordem") or 0)
    except ValueError:
        ordem = 0
    return dict(nome=nome, ufs=ufs if not cep_de else "", cep_de=int(cep_de) if cep_de else None,
                cep_ate=int(cep_ate) if cep_ate else None, prazo=" ".join(f.get("prazo", "").split())[:60],
                ordem=ordem, ativo="ativo" in f, **valores), None
