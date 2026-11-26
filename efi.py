"""Pix com a Efí (ex-Gerencianet): cria a cobrança, consulta se foi paga e confere quem pagou.

A Efí é o único dos gateways avaliados que informa o nome e o CPF/CNPJ do pagador (o CPF vem
parcialmente mascarado, ex. ***.123.456-**: comparamos os dígitos visíveis e o nome).

Configure no Render:
  EFI_CLIENT_ID, EFI_CLIENT_SECRET  (Efí > API > Aplicações)
  EFI_CERTIFICADO                   caminho do certificado .p12 (ou .pem) baixado na Efí;
                                    no Render, envie como "Secret File" (fica em /etc/secrets/<nome>)
  EFI_CHAVE_PIX                     a chave Pix da conta Efí que recebe (se vazio, usa PIX_KEY)
  EFI_SANDBOX=1                     usa o ambiente de homologação (cobranças até R$ 10 são pagas sozinhas)
Sem essas variáveis o Pix continua com o QR estático de antes.
Docs: https://dev.efipay.com.br/docs/api-pix/
"""
import base64
import json
import os
import re
import ssl
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request

from flask import current_app

PRODUCAO, HOMOLOGACAO = "https://pix.api.efipay.com.br", "https://pix-h.api.efipay.com.br"
_token = {"valor": "", "expira": 0.0}
_contexto = {}


class ErroEfi(Exception):
    pass


def ligado():
    c = current_app.config
    return bool(c.get("EFI_CLIENT_ID") and c.get("EFI_CLIENT_SECRET") and c.get("EFI_CERTIFICADO") and chave())


def chave():
    c = current_app.config
    return c.get("EFI_CHAVE_PIX") or c.get("PIX_KEY") or ""


def _base():
    return HOMOLOGACAO if current_app.config.get("EFI_SANDBOX") else PRODUCAO


def _ssl():
    """Contexto TLS com o certificado da conta (a Efí exige o certificado em todas as chamadas)."""
    caminho = current_app.config["EFI_CERTIFICADO"]
    if caminho not in _contexto:
        pem = caminho
        if caminho.lower().endswith(".p12"):  # converte o .p12 da Efí para PEM uma vez, num arquivo temporário
            from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, pkcs12
            senha = (current_app.config.get("EFI_CERTIFICADO_SENHA") or "").encode() or None
            with open(caminho, "rb") as f:
                chave_privada, cert, extras = pkcs12.load_key_and_certificates(f.read(), senha)
            conteudo = chave_privada.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
            conteudo += cert.public_bytes(Encoding.PEM) + b"".join(c.public_bytes(Encoding.PEM) for c in extras or [])
            fd, pem = tempfile.mkstemp(suffix=".pem")
            with os.fdopen(fd, "wb") as f:
                f.write(conteudo)
            os.chmod(pem, 0o600)
        ctx = ssl.create_default_context()
        ctx.load_cert_chain(pem)
        _contexto[caminho] = ctx
    return _contexto[caminho]


def _pedir(metodo, caminho, corpo=None, token=True):
    c = current_app.config
    req = urllib.request.Request(_base() + caminho, method=metodo,
                                 data=json.dumps(corpo).encode() if corpo is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + _token_valido())
    else:
        cred = base64.b64encode(f"{c['EFI_CLIENT_ID']}:{c['EFI_CLIENT_SECRET']}".encode()).decode()
        req.add_header("Authorization", "Basic " + cred)
    try:
        with urllib.request.urlopen(req, timeout=15, context=_ssl()) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detalhe = e.read()[:300].decode("utf-8", "replace")
        raise ErroEfi(f"Efí respondeu {e.code} em {caminho}: {detalhe}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ErroEfi(f"Efí indisponível: {e}") from e


def _token_valido():
    if time.time() < _token["expira"] - 60:
        return _token["valor"]
    r = _pedir("POST", "/oauth/token", {"grant_type": "client_credentials"}, token=False)
    _token.update(valor=r["access_token"], expira=time.time() + int(r.get("expires_in", 3600)))
    return _token["valor"]


def txid_do_pedido(oid):
    return "Marketplace B2B" + str(oid).zfill(25)  # 30 caracteres alfanuméricos (a Efí aceita de 26 a 35)


def criar_cobranca(pedido):
    """Cria a cobrança Pix do pedido. Devolve (txid, copia e cola). Levanta ErroEfi se não der."""
    txid = txid_do_pedido(pedido.id)
    devedor = {}
    if len(pedido.documento or "") == 14:
        devedor = {"cnpj": pedido.documento, "nome": (pedido.razao_social or pedido.customer_name)[:200]}
    elif len(pedido.documento or "") == 11:
        devedor = {"cpf": pedido.documento, "nome": pedido.customer_name[:200]}
    corpo = {"calendario": {"expiracao": 86400}, "valor": {"original": f"{pedido.total:.2f}"}, "chave": chave(),
             "solicitacaoPagador": f"Pedido {pedido.id} no Marketplace B2B"}
    if devedor:
        corpo["devedor"] = devedor
    r = _pedir("PUT", f"/v2/cob/{txid}", corpo)
    if not r.get("pixCopiaECola"):
        raise ErroEfi("A Efí não devolveu o código Pix.")
    return txid, r["pixCopiaECola"]


def consultar_cobranca(txid):
    """Devolve (pago?, pagador) — pagador = {"nome", "documento"} quando a Efí informa."""
    r = _pedir("GET", f"/v2/cob/{txid}")
    if r.get("status") != "CONCLUIDA":
        return False, None
    pix = (r.get("pix") or [{}])[0]
    p = pix.get("pagador") or (pix.get("gnExtras") or {}).get("pagador") or {}
    return True, {"nome": " ".join(str(p.get("nome") or "").split())[:160],
                  "documento": str(p.get("cpf") or p.get("cnpj") or "")[:20]}


# ---------- conferência do pagador ----------
def _nome(texto):
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().upper()
    t = re.sub(r"[^A-Z0-9 ]", " ", t)
    fora = {"LTDA", "ME", "EPP", "EIRELI", "SA", "S", "A", "DE", "DA", "DO", "DAS", "DOS", "E"}
    return [p for p in t.split() if p not in fora]


def pagador_confere(pagador, documento, nomes):
    """Compara o pagador informado pela Efí com o cadastro.

    Documento: cada dígito visível tem que bater (os mascarados com * são ignorados); com documento
    informado, ele decide. Sem documento, o nome do pagador tem que ser igual a algum nome do cadastro."""
    doc_pagador = re.sub(r"[^0-9*]", "", (pagador or {}).get("documento", ""))
    if doc_pagador and documento:
        return len(doc_pagador) == len(documento) and all(a in ("*", b) for a, b in zip(doc_pagador, documento))
    nome = _nome((pagador or {}).get("nome", ""))
    return bool(nome) and any(nome == _nome(n) for n in nomes if n)
