"""Confirmação do celular por SMS com a Twilio Verify (a Twilio gera, envia e confere o código).

Configure no Render: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN e TWILIO_VERIFY_SID (o "Service SID", começa com VA).
Sem essas variáveis a confirmação fica desligada e ninguém é bloqueado.
Docs: https://www.twilio.com/docs/verify/api
"""
import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from flask import current_app

from b2b import so_digitos

URL = "https://verify.twilio.com/v2/Services/{}/{}"
APROVADO, ERRADO, EXPIRADO, FALHOU = "aprovado", "errado", "expirado", "falhou"


def ligado():
    c = current_app.config
    return bool(c.get("TWILIO_ACCOUNT_SID") and c.get("TWILIO_AUTH_TOKEN") and c.get("TWILIO_VERIFY_SID"))


def celular_pendente(user):
    """True se a confirmação está ligada e este cliente ainda não confirmou o celular."""
    return bool(user and not user.is_admin and ligado() and not user.telefone_verificado)


def normalizar_celular(texto):
    """'(11) 98765-4321' -> '+5511987654321'. Devolve None se não for um celular brasileiro."""
    d = so_digitos(texto)
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    d = d.lstrip("0")
    if len(d) != 11 or d[2] != "9" or d[0] == "0":  # DDD + 9 + 8 dígitos
        return None
    return "+55" + d


def formatar_celular(e164):
    d = (e164 or "").removeprefix("+55")
    return f"({d[:2]}) {d[2:7]}-{d[7:]}" if len(d) == 11 else (e164 or "")


def _chamar(acao, dados):
    c = current_app.config
    req = urllib.request.Request(URL.format(c["TWILIO_VERIFY_SID"], acao),
                                 data=urllib.parse.urlencode(dados).encode(), method="POST")
    cred = base64.b64encode(f"{c['TWILIO_ACCOUNT_SID']}:{c['TWILIO_AUTH_TOKEN']}".encode()).decode()
    req.add_header("Authorization", "Basic " + cred)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def enviar_codigo(celular):
    """Manda o SMS com o código. Devolve True se a Twilio aceitou."""
    try:
        return _chamar("Verifications", {"To": celular, "Channel": "sms", "Locale": "pt-BR"}).get("status") == "pending"
    except (urllib.error.URLError, OSError, ValueError) as e:
        current_app.logger.warning("Twilio: falha ao enviar código: %s", e)
        return False


def conferir_codigo(celular, codigo):
    """APROVADO | ERRADO | EXPIRADO (passou de 10 min ou de tentativas: peça outro) | FALHOU (Twilio fora do ar)."""
    try:
        status = _chamar("VerificationCheck", {"To": celular, "Code": codigo}).get("status")
    except urllib.error.HTTPError as e:
        if e.code == 404:  # a Twilio apaga a verificação vencida, aprovada ou com tentativas esgotadas
            return EXPIRADO
        current_app.logger.warning("Twilio: falha ao conferir código: %s", e)
        return FALHOU
    except (urllib.error.URLError, OSError, ValueError) as e:
        current_app.logger.warning("Twilio: falha ao conferir código: %s", e)
        return FALHOU
    if status == "approved":
        return APROVADO
    return ERRADO if status == "pending" else EXPIRADO
