"""Formatos brasileiros usados no site todo: dinheiro, números digitados com vírgula, datas no horário de Brasília e
campos de formulário. Um lugar só: antes cada módulo tinha a sua cópia.

As datas e horas são gravadas no banco em UTC; o horário de Brasília é UTC-3 (sem horário de verão desde 2019).
"""
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENTAVO = Decimal("0.01")
FUSO = timedelta(hours=3)  # Brasília = UTC - 3


# ---------- dinheiro e números ----------
def brl(v):
    """1234.5 -> "R$ 1.234,50"."""
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def num(v):
    """Número para mostrar ou pôr num campo: 12.50 -> "12,5" (vazio para None)."""
    return "" if v is None else ("%g" % float(v)).replace(".", ",")


def duas_casas(v):
    """Valor para um campo de formulário: 12.5 -> "12,50" (vazio para None)."""
    return "" if v is None else ("%.2f" % float(v)).replace(".", ",")


def uma_casa(v):
    """Percentual com uma casa: 12.34 -> "12,3" (vazio para None)."""
    return "" if v is None else ("%.1f" % float(v)).replace(".", ",")


def ler_numero(texto, vazio=None, casas=None):
    """Número digitado no jeito brasileiro: "1.234,56", "R$ 18,90", "10%", "4.5" ou um número do Excel.

    Vazio devolve `vazio` (sem ele, é erro). Texto que não é número levanta InvalidOperation (um ArithmeticError).
    `casas` (ex.: CENTAVO) arredonda o resultado."""
    if isinstance(texto, (int, float, Decimal)) and not isinstance(texto, bool):
        valor = Decimal(str(texto))
    else:
        t = str(texto or "").replace("R$", "").replace("%", "").replace(" ", "").strip()
        if not t:
            if vazio is None:
                raise InvalidOperation("vazio")
            t = str(vazio)
        elif "," in t:
            t = t.replace(".", "").replace(",", ".")
        valor = Decimal(t)
    if not valor.is_finite():
        raise InvalidOperation("não é um número")
    return valor.quantize(casas) if casas else valor


def decimal_ou(v, padrao=0):
    """Valor guardado (no banco ou na configuração) como Decimal; vazio ou inválido devolve `padrao`."""
    try:
        d = Decimal(str(v)) if v not in (None, "") else Decimal(str(padrao))
        return d if d.is_finite() else Decimal(str(padrao))
    except (InvalidOperation, ValueError):
        return Decimal(str(padrao))


def arredondar(v, casas=CENTAVO):
    """Arredonda como na nota fiscal e no boleto: 0,005 vai para cima."""
    return Decimal(v).quantize(casas, ROUND_HALF_UP)


def tamanho(n):
    """Tamanho de arquivo: 2_500_000 -> "2,5 MB"."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f} GB".replace(".", ",")
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB".replace(".", ",")
    return f"{max(n // 1000, 1 if n else 0)} KB"


# ---------- textos de formulário ----------
def campo(form, chave, limite=60):
    """Texto de um formulário sem espaços repetidos e com tamanho máximo."""
    return " ".join((form.get(chave) or "").split())[:limite]


# ---------- datas no horário de Brasília ----------
def hoje():
    """Data de hoje em Brasília (o servidor roda em UTC)."""
    return (datetime.now(timezone.utc) - FUSO).date()


def data_br(momento):
    """Data em Brasília de um momento gravado em UTC (vazio: hoje)."""
    return (momento - FUSO).date() if momento else hoje()


def hora_br(momento):
    """"24/11/2026 11:07" no horário de Brasília (vazio para None)."""
    return (momento - FUSO).strftime("%d/%m/%Y %H:%M") if momento else ""


def dia_br(momento):
    """"24/11/2026" no horário de Brasília (vazio para None)."""
    return (momento - FUSO).strftime("%d/%m/%Y") if momento else ""


def inicio_do_dia(dia):
    """Momento em UTC em que o dia começa em Brasília (para filtrar datas gravadas em UTC)."""
    return datetime.combine(dia, datetime.min.time()) + FUSO
