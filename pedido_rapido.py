"""Pedido rápido: o comprador cola uma lista "código quantidade" (uma por linha, também colada do Excel) e tudo vai
para o carrinho de uma vez. A quantidade segue as regras do produto (mínimo, múltiplo, estoque)."""
import re

from b2b import ajustar_qtd, disponivel, preco_fracionado, preco_unitario, separar_fracionado
from models import Product

MAX_LINHAS = 200


def _numero(texto):
    """"50", "1.200" (milhar), "1.200,00", "50,0" -> inteiro positivo; outra coisa -> None."""
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", texto) or re.fullmatch(r"[\d.]+,\d+", texto):
        t = texto.replace(".", "").replace(",", ".")
    else:
        t = texto.replace(",", ".")
    try:
        valor = float(t)
    except ValueError:
        return None
    return int(valor) if valor.is_integer() and valor > 0 else None


def separar(linha):
    """"DISCO-7 50" / "DISCO-7;50" / "DISCO-7<TAB>50" / "Disco de Desbaste 7 pol 50" -> ("DISCO-7", 50).
    Sem quantidade no fim, conta 1."""
    partes = [p for p in re.split(r"[\t;]+", linha.strip()) if p.strip()]
    if len(partes) >= 2 and _numero(partes[-1].strip()) is not None:
        return " ".join(p.strip() for p in partes[:-1]), _numero(partes[-1].strip())
    palavras = linha.split()
    if len(palavras) >= 2 and _numero(palavras[-1]) is not None:
        return " ".join(palavras[:-1]).rstrip(",;"), _numero(palavras[-1])
    return " ".join(palavras).rstrip(",;"), 1


def interpretar(texto, cliente):
    """Devolve (linhas, erro). Cada linha: entrada, produto, pedida, qtd, unit, sub, aviso, ok."""
    brutas = [l for l in (texto or "").splitlines() if l.strip()]
    if not brutas:
        return [], "Cole pelo menos uma linha com o código e a quantidade."
    if len(brutas) > MAX_LINHAS:
        return [], f"Cole no máximo {MAX_LINHAS} linhas de cada vez."
    ativos = Product.query.filter_by(active=True).all()
    por_codigo = {p.codigo.strip().lower(): p for p in ativos if p.codigo}
    por_nome = {p.name.strip().lower(): p for p in ativos}
    pedidos, linhas = {}, []
    for bruta in brutas:
        entrada, pedida = separar(bruta)
        chave = entrada.strip().lower()
        p = por_codigo.get(chave) or por_nome.get(chave)
        if not p:
            linhas.append({"entrada": bruta.strip()[:120], "ok": False, "aviso": "Código não encontrado."})
            continue
        if p.id in pedidos:  # mesmo produto em duas linhas: soma
            pedidos[p.id]["pedida"] += pedida
            continue
        pedidos[p.id] = linha = {"entrada": bruta.strip()[:120], "produto": p, "pedida": pedida}
        linhas.append(linha)
    for linha in linhas:
        p = linha.get("produto")
        if not p:
            continue
        if not disponivel(p):
            linha.update(ok=False, aviso="Indisponível no momento.")
            continue
        qtd = ajustar_qtd(p, linha["pedida"])
        fechadas, frac = separar_fracionado(p, qtd)
        unit = preco_unitario(p, qtd, cliente)
        unit_frac = preco_fracionado(p, qtd, cliente) if frac else unit
        aviso = ""
        if qtd != linha["pedida"]:
            if qtd < linha["pedida"]:
                aviso = f"Só há {qtd} un disponíveis."
            elif p.multiplo > 1 and not p.fracionavel:
                aviso = f"Ajustado para {qtd} un (vendido em múltiplos de {p.multiplo})."
            else:
                aviso = f"Ajustado para {qtd} un (pedido mínimo de {p.qtd_minima})."
        elif frac:
            aviso = f"{frac} un fora da embalagem (fracionadas)."
        linha.update(ok=True, qtd=qtd, unit=unit, sub=unit * fechadas + unit_frac * frac, aviso=aviso)
    return linhas, None
