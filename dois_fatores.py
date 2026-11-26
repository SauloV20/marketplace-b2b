"""Verificação em duas etapas do painel: código de 6 dígitos de um app autenticador (TOTP, RFC 6238).

Funciona com Google Authenticator, Microsoft Authenticator, Authy, 1Password etc. Sem serviço pago e sem
biblioteca extra: o código é calculado aqui mesmo.

Se o celular for perdido: use um dos códigos de recuperação (cada um vale uma vez). Sem eles, defina a variável
ADMIN_2FA_DESLIGADO=1 no Render (Environment), entre só com a senha, desative e ative de novo pelo painel,
e depois apague a variável.
"""
import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from urllib.parse import quote

from flask import current_app

PASSO_S = 30  # um código novo a cada 30 segundos
TOLERANCIA = 1  # aceita o código anterior e o próximo (relógio do celular um pouco adiantado/atrasado)
QTD_RECUPERACAO = 8


def novo_segredo():
    return base64.b32encode(secrets.token_bytes(20)).decode()


def codigo(segredo, passo):
    chave = base64.b32decode(segredo)
    h = hmac.new(chave, struct.pack(">Q", passo), hashlib.sha1).digest()
    inicio = h[-1] & 0x0F
    numero = (struct.unpack(">I", h[inicio:inicio + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{numero:06d}"


def _so_digitos(texto):
    return "".join(ch for ch in (texto or "") if ch.isdigit())


def conferir(segredo, digitado, ultimo_passo=0, agora=None):
    """Devolve o passo de tempo do código, se ele estiver certo e ainda não tiver sido usado; senão None."""
    digitado = _so_digitos(digitado)
    if not segredo or len(digitado) != 6:
        return None
    atual = int((agora if agora is not None else time.time()) // PASSO_S)
    for passo in range(atual - TOLERANCIA, atual + TOLERANCIA + 1):
        if passo > (ultimo_passo or 0) and hmac.compare_digest(codigo(segredo, passo), digitado):
            return passo  # guardar este passo impede usar o mesmo código duas vezes
    return None


def link_do_app(segredo, email):
    """Endereço otpauth:// que vira o QR Code lido pelo app autenticador."""
    return (f"otpauth://totp/Marketplace B2B:{quote(email)}?secret={segredo}&issuer=Marketplace B2B"
            f"&algorithm=SHA1&digits=6&period={PASSO_S}")


def segredo_formatado(segredo):
    return " ".join(segredo[i:i + 4] for i in range(0, len(segredo), 4))


# ---------- códigos de recuperação ----------
def _normalizar(c):
    return "".join(ch for ch in (c or "").lower() if ch.isalnum())


def _hash(c):
    return hashlib.sha256(_normalizar(c).encode()).hexdigest()


def novos_codigos_recuperacao():
    """Devolve (códigos para mostrar uma vez, JSON com os hashes para guardar)."""
    codigos = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(QTD_RECUPERACAO)]
    return codigos, json.dumps([_hash(c) for c in codigos])


def restantes(user):
    try:
        return len(json.loads(user.codigos_recuperacao or "[]"))
    except ValueError:
        return 0


def usar_codigo_recuperacao(user, digitado):
    """Se o código de recuperação for válido, gasta ele (não vale de novo) e devolve True."""
    if len(_normalizar(digitado)) != 12:
        return False
    try:
        hashes = json.loads(user.codigos_recuperacao or "[]")
    except ValueError:
        return False
    h = _hash(digitado)
    for guardado in hashes:
        if hmac.compare_digest(guardado, h):
            hashes.remove(guardado)
            user.codigos_recuperacao = json.dumps(hashes)
            return True
    return False


# ---------- estado ----------
def ativo(user):
    return bool(user and user.totp_segredo)


def desligado_pela_variavel():
    return bool(current_app.config.get("ADMIN_2FA_DESLIGADO"))


def exigido(user):
    return ativo(user) and not desligado_pela_variavel()


def ativar(user, segredo, passo):
    codigos, hashes = novos_codigos_recuperacao()
    user.totp_segredo, user.totp_ultimo, user.codigos_recuperacao = segredo, passo, hashes
    return codigos


def desativar(user):
    user.totp_segredo, user.totp_ultimo, user.codigos_recuperacao = "", 0, ""
