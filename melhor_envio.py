"""Cotação de frete pelo Melhor Envio: Correios (PAC, SEDEX, Mini Envios) e transportadoras, sem contrato.

Configure no Render:
  MELHOR_ENVIO_TOKEN     token de acesso (Melhor Envio > Configurações > Permissões de acesso > Gerar novo token,
                         com a permissão de cálculo de frete "shipping-calculate")
  MELHOR_ENVIO_CONTATO   e-mail técnico que vai no User-Agent (o Melhor Envio pede um contato em cada chamada)
  MELHOR_ENVIO_SANDBOX=1 usa o ambiente de testes (sandbox.melhorenvio.com.br, com token gerado lá)
E no painel (Configurações > Frete > Melhor Envio): ligar, CEP de onde sai a mercadoria, serviços, caixa padrão,
prazo de separação e acréscimo. Sem token ou desligado, o frete continua só com a tabela própria.

Toda a conversa com a API fica neste arquivo: se o Melhor Envio mudar algum campo, o ajuste é só aqui.
"""
import json
import math
import time
import urllib.error
import urllib.request
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from flask import current_app, has_app_context

from b2b import config, salvar_config
from formato import decimal_ou, num

PRODUCAO, SANDBOX = "https://melhorenvio.com.br", "https://sandbox.melhorenvio.com.br"
CAMINHO = "/api/v2/me/shipment/calculate"
CENTAVO = Decimal("0.01")
TEMPO_LIMITE = 8  # segundos: a página do carrinho não pode ficar presa esperando
CACHE_SEGUNDOS = 15 * 60  # a mesma cotação vale entre o carrinho e a confirmação do pedido
PAUSA_APOS_ERRO = 60  # API fora do ar: não tenta de novo por 1 minuto (o site segue com a tabela própria)

# serviços mais usados (o id é o do Melhor Envio); o painel deixa escolher quais mostrar
SERVICOS = {"1": "Correios PAC", "2": "Correios SEDEX", "17": "Correios Mini Envios",
            "3": "Jadlog .Package", "4": "Jadlog .Com"}
PADRAO_SERVICOS = "1,2"

# caixa usada quando o produto não tem medidas (cm) nem peso (kg) cadastrados
CAMPOS = {"me_ativo": "", "me_cep_origem": "", "me_servicos": PADRAO_SERVICOS, "me_prazo_extra": "0",
          "me_acrescimo": "0", "me_altura": "10", "me_largura": "15", "me_comprimento": "20", "me_peso": "0.3",
          "me_seguro": ""}

_cache = {}
_pausa = {"ate": 0.0}


class ErroMelhorEnvio(Exception):
    pass


# ---------- configuração ----------
def ajustes():
    a = {k: config(k, v) for k, v in CAMPOS.items()}
    return {"ativo": a["me_ativo"] == "1", "cep_origem": a["me_cep_origem"],
            "servicos": [s for s in a["me_servicos"].split(",") if s.strip()],
            "prazo_extra": int(decimal_ou(a["me_prazo_extra"])), "acrescimo": decimal_ou(a["me_acrescimo"]),
            "altura": decimal_ou(a["me_altura"], "10"), "largura": decimal_ou(a["me_largura"], "15"),
            "comprimento": decimal_ou(a["me_comprimento"], "20"), "peso": decimal_ou(a["me_peso"], "0.3"),
            "seguro": a["me_seguro"] == "1"}


def tem_token():
    return has_app_context() and bool(current_app.config.get("MELHOR_ENVIO_TOKEN"))


def ligado():
    if not tem_token():
        return False
    a = ajustes()
    return a["ativo"] and len(a["cep_origem"]) == 8 and bool(a["servicos"])


def ler_ajustes(f):
    """Valida o formulário do painel. Devolve (dados, erro)."""
    import frete
    cep = frete.normalizar_cep(f.get("me_cep_origem", ""))
    ativo = "me_ativo" in f
    if ativo and not cep:
        return None, "Informe o CEP de onde sai a mercadoria (8 números)."
    servicos = [s for s in f.getlist("me_servicos") if s in SERVICOS]
    if ativo and not servicos:
        return None, "Marque pelo menos um serviço de entrega."
    dados = {"me_ativo": "1" if ativo else "", "me_cep_origem": cep or "",
             "me_servicos": ",".join(servicos) or PADRAO_SERVICOS, "me_seguro": "1" if "me_seguro" in f else ""}
    limites = (("me_prazo_extra", "os dias de separação", 0, 60, True),
               ("me_acrescimo", "o acréscimo", 0, 100, False),
               ("me_altura", "a altura da caixa padrão", Decimal("0.1"), 1000, False),
               ("me_largura", "a largura da caixa padrão", Decimal("0.1"), 1000, False),
               ("me_comprimento", "o comprimento da caixa padrão", Decimal("0.1"), 1000, False),
               ("me_peso", "o peso padrão", Decimal("0.001"), 1000, False))
    for campo, rotulo, minimo, maximo, inteiro in limites:
        texto = (f.get(campo) or "").replace(",", ".").strip() or CAMPOS[campo]
        try:
            v = Decimal(texto)
        except InvalidOperation:
            return None, f"Confira {rotulo}: use um número."
        if not v.is_finite() or not minimo <= v <= maximo or (inteiro and v != v.to_integral_value()):
            return None, f"{rotulo[0].upper()}{rotulo[1:]} vai de {num(minimo)} a {num(maximo)}" + \
                (" (número inteiro)." if inteiro else ".")
        dados[campo] = str(int(v) if inteiro else v.normalize())
    return dados, None


def gravar_ajustes(dados):
    for k, v in dados.items():
        salvar_config(k, v)
    _cache.clear()


# ---------- cotação ----------
def item(p, qtd, preco):
    """Um produto do carrinho no formato da cotação (medidas em cm, peso em kg por unidade)."""
    return {"pid": p.id, "qtd": int(qtd), "preco": Decimal(preco or 0),
            "altura": p.altura_cm, "largura": p.largura_cm, "comprimento": p.comprimento_cm,
            "peso": p.peso_kg if p.peso_kg and p.peso_kg > 0 else None}


def itens_das_linhas(lines):
    return [item(l["p"], l["qty"], l.get("unit") or l["p"].price) for l in lines if l.get("qty")]


def itens_do_pedido(o):
    return [item(i.product, i.quantity, i.unit_price) for i in o.items if i.product]


def _produtos(itens, a):
    lista = []
    for i in itens:
        def medida(v, padrao):  # a API pede centímetros inteiros: arredonda para cima (a caixa não encolhe)
            return max(1, math.ceil(Decimal(v if v else padrao)))
        lista.append({"id": str(i["pid"]), "width": medida(i["largura"], a["largura"]),
                      "height": medida(i["altura"], a["altura"]), "length": medida(i["comprimento"], a["comprimento"]),
                      "weight": float(Decimal(i["peso"] if i["peso"] else a["peso"])),
                      "insurance_value": float(i["preco"]) if a["seguro"] else 0, "quantity": i["qtd"]})
    return lista


def _texto_erro(bruto):
    """Resposta de erro da API -> texto legível ("Peso do pacote 1 não informado; ...")."""
    try:
        j = json.loads(bruto)
    except ValueError:
        return bruto[:300]
    partes = []

    def juntar(v):
        if isinstance(v, dict):
            for x in v.values():
                juntar(x)
        elif isinstance(v, list):
            for x in v:
                juntar(x)
        elif v not in (None, ""):
            partes.append(str(v))
    juntar(j.get("errors") or j.get("error") or j.get("message") or j)
    return "; ".join(dict.fromkeys(partes))[:500] or bruto[:300]


def _http(url, corpo, token, contato, metodo="POST"):
    """Chamada à API. Separado para os testes trocarem por uma resposta simulada."""
    req = urllib.request.Request(url, method=metodo, data=json.dumps(corpo).encode() if corpo is not None else None)
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", f"Marketplace B2B Loja ({contato})" if contato else "Marketplace B2B Loja")
    try:
        with urllib.request.urlopen(req, timeout=TEMPO_LIMITE) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detalhe = _texto_erro(e.read().decode(errors="replace"))
        if e.code == 401:
            detalhe = "token recusado (vencido ou sem permissão): gere outro no Melhor Envio. " + detalhe
        raise ErroMelhorEnvio(detalhe or f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise ErroMelhorEnvio(str(e)) from None


def _prazo(dias):
    if not dias:
        return ""
    return f"{dias} dia útil" if dias == 1 else f"{dias} dias úteis"


def base():
    return SANDBOX if current_app.config.get("MELHOR_ENVIO_SANDBOX") else PRODUCAO


def api(metodo, caminho, corpo=None):
    """Chamada autenticada (etiquetas, rastreio, cancelamento). Levanta ErroMelhorEnvio com a mensagem da API."""
    c = current_app.config
    if not c.get("MELHOR_ENVIO_TOKEN"):
        raise ErroMelhorEnvio("falta o MELHOR_ENVIO_TOKEN nas variáveis do Render")
    return _http(base() + caminho, corpo, c["MELHOR_ENVIO_TOKEN"], c.get("MELHOR_ENVIO_CONTATO", ""), metodo)


def _ler_resposta(dados, a):
    """Resposta da API -> opções [{codigo, nome, valor, dias, prazo}]. Serviço com erro (ex.: CEP não atendido,
    peso acima do limite) fica de fora."""
    if isinstance(dados, dict):
        dados = [dados]
    opcoes = []
    for s in dados or []:
        if not isinstance(s, dict) or s.get("error") or str(s.get("id", "")) not in a["servicos"]:
            continue
        preco = decimal_ou(s.get("custom_price") or s.get("price"), "0")
        if preco <= 0:
            continue
        valor = (preco * (100 + a["acrescimo"]) / 100).quantize(CENTAVO, ROUND_HALF_UP)
        dias = s.get("custom_delivery_time") or s.get("delivery_time") or 0
        try:
            dias = int(dias) + a["prazo_extra"] if dias else 0
        except (TypeError, ValueError):
            dias = 0
        empresa = (s.get("company") or {}).get("name") or ""
        nome = " ".join(f"{empresa} {s.get('name') or ''}".split()) or SERVICOS.get(str(s.get("id")), "Entrega")
        opcoes.append({"codigo": f"me:{s.get('id')}", "nome": nome[:80], "valor": valor, "dias": dias,
                       "prazo": _prazo(dias), "custo": preco, "servico": str(s.get("id")),
                       "empresa": empresa, "pacotes": s.get("packages") or []})
    return sorted(opcoes, key=lambda o: (o["valor"], o["dias"]))


def cotar(cep_destino, itens, servicos=None):
    """Opções de entrega do Melhor Envio para os itens. Lista vazia se desligado ou fora do ar (o erro vai para o
    log). A mesma cotação é reaproveitada por 15 minutos. `servicos` troca os serviços da configuração (etiqueta)."""
    if not ligado() or not itens:
        return []
    a = ajustes()
    if servicos:
        a["servicos"] = [str(x) for x in servicos]
    chave = (a["cep_origem"], cep_destino, tuple(sorted((i["pid"], i["qtd"], str(i["preco"])) for i in itens)),
             tuple(a["servicos"]), str(a["acrescimo"]), a["prazo_extra"], a["seguro"])
    agora = time.time()
    guardado = _cache.get(chave)
    if guardado and agora - guardado[0] < CACHE_SEGUNDOS:
        return guardado[1]
    if agora < _pausa["ate"]:
        return []
    c = current_app.config
    corpo = {"from": {"postal_code": a["cep_origem"]}, "to": {"postal_code": cep_destino},
             "products": _produtos(itens, a), "options": {"receipt": False, "own_hand": False},
             "services": ",".join(a["servicos"])}
    try:
        resposta = _http(base() + CAMINHO, corpo, c["MELHOR_ENVIO_TOKEN"], c.get("MELHOR_ENVIO_CONTATO", ""))
    except ErroMelhorEnvio as e:
        _pausa["ate"] = agora + PAUSA_APOS_ERRO
        current_app.logger.warning("Melhor Envio: cotação para o CEP %s falhou: %s", cep_destino[:5] + "***", e)
        return []
    opcoes = _ler_resposta(resposta, a)
    if len(_cache) > 500:
        _cache.clear()
    _cache[chave] = (agora, opcoes)
    return opcoes


def limpar_cache():
    _cache.clear()
    _pausa["ate"] = 0.0
