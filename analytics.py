"""Google Analytics 4 com consentimento (LGPD): só carrega depois que o visitante aceita os cookies.

- O ID de medição (G-XXXXXXX) fica em Painel > Métricas (Google Analytics). Vazio = desligado, sem aviso de cookies.
- Mede só as páginas da loja (vitrine, produto, carrinho, finalização e página do pedido). Conta, painel e links
  com código (confirmar e-mail, nova senha) ficam de fora: nada de dado pessoal ou código indo para o Google.
- A compra vai como evento "purchase" (número do pedido, valor, frete e produtos), uma vez por pedido.
- A escolha fica no cookie "cookies_ok" (sim/nao) por 180 dias; o link "Cookies" no rodapé abre o aviso de novo.
"""
import re

from flask import request, session

from b2b import config, salvar_config

PADRAO_ID = re.compile(r"G-[A-Z0-9]{4,15}")
CSP_GOOGLE = {"script-src": "https://www.googletagmanager.com",
              "connect-src": "https://*.google-analytics.com https://*.analytics.google.com "
                             "https://*.googletagmanager.com"}


def medicao_id():
    valor = config("ga_id", "")
    return valor if PADRAO_ID.fullmatch(valor or "") else ""


def salvar_id(texto):
    """Devolve a mensagem de erro, ou None se salvou (vazio desliga)."""
    valor = (texto or "").strip().upper()
    if valor and not PADRAO_ID.fullmatch(valor):
        return "O ID de medição do Google Analytics tem o formato G-XXXXXXXXXX (Administrador > Fluxos de dados)."
    salvar_config("ga_id", valor)
    return None


def id_da_pagina():
    """O ID, se esta página pode ser medida; senão vazio."""
    if request.blueprint != "shop":
        return ""
    return medicao_id()


def csp(base):
    """A política de segurança da página, liberando o Google só onde o Analytics pode carregar."""
    if not id_da_pagina():
        return base
    partes = [p.strip() for p in base.split(";") if p.strip()]
    nomes = [p.split()[0] for p in partes]
    for diretiva, origens in CSP_GOOGLE.items():
        if diretiva in nomes:
            i = nomes.index(diretiva)
            partes[i] += " " + origens
        else:
            partes.append(f"{diretiva} 'self' {origens}")
    return "; ".join(partes)


def compra(o):
    """Dados do evento "purchase" do pedido, só na primeira vez que a página dele abre nesta sessão."""
    if not id_da_pagina() or o.status in ("orcamento", "aprovacao", "cancelado"):
        return None
    vistos = session.get("ga_compras", [])
    if o.id in vistos:
        return None
    session["ga_compras"] = (vistos + [o.id])[-20:]
    return {"transaction_id": str(o.id), "value": float(o.total), "currency": "BRL",
            "shipping": float(o.frete or 0), "coupon": o.cupom or None,
            "items": [{"item_id": i.product.codigo or str(i.product_id), "item_name": i.product.name,
                       "price": float(i.unit_price), "quantity": i.quantity} for i in o.items]}
