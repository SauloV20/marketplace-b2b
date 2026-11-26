"""Etiqueta e postagem pelo Melhor Envio: comprar, gerar, imprimir, rastrear e cancelar a etiqueta de um pedido.

Fluxo da API (https://docs.melhorenvio.com.br/docs/compra-de-fretes):
  1. POST /api/v2/me/cart                inclui o envio no carrinho (um por volume: Correios não aceita vários)
  2. POST /api/v2/me/shipment/checkout   paga com o saldo da carteira do Melhor Envio
  3. POST /api/v2/me/shipment/generate   gera a etiqueta (a transportadora é avisada)
  4. POST /api/v2/me/shipment/print      link para imprimir
  5. POST /api/v2/me/shipment/tracking   situação e código de rastreio
  6. POST /api/v2/me/shipment/cancel     cancela (o valor volta para a carteira); no carrinho: DELETE /cart/{id}
Cada etiqueta fica gravada a cada passo: se algo falhar no meio, "Continuar" retoma de onde parou, sem pagar de novo.

Remetente: os dados da empresa em Nota fiscal > Configuração. Destinatário: o do pedido, com o endereço separado
pelo CEP, para a loja conferir antes de comprar. Envio com nota fiscal (chave da NF-e autorizada) ou com declaração
de conteúdo (só para cliente pessoa física ou empresa não contribuinte do ICMS).
"""
from decimal import Decimal

from flask import current_app

import melhor_envio
from b2b import cnpj_valido, config, cpf_valido, so_digitos
from formato import campo
from melhor_envio import ErroMelhorEnvio
from models import EtiquetaEnvio, agora_utc, db

SITUACOES = {"carrinho": "No carrinho (não paga)", "paga": "Paga, falta gerar", "gerada": "Pronta para imprimir",
             "postada": "Postada", "entregue": "Entregue", "cancelada": "Cancelada"}
# situação do Melhor Envio -> a nossa
DA_API = {"pending": "carrinho", "released": "paga", "generated": "gerada", "received": "postada", "posted": "postada",
          "delivered": "entregue", "canceled": "cancelada", "cancelled": "cancelada", "expired": "cancelada",
          "undelivered": "postada", "paused": "postada", "suspended": "postada"}
# situações que pedem atenção da loja (a etiqueta segue "postada", com o aviso em `erro`)
ALERTAS = {"undelivered": "Não entregue: a transportadora vai tentar de novo ou devolver. Fale com o cliente.",
           "paused": "Entrega interrompida: o destinatário precisa agir (ex.: retirar na agência). Avise o cliente.",
           "suspended": "Entrega suspensa pela transportadora. Veja os detalhes no Melhor Envio."}
EM_ANDAMENTO = ("paga", "gerada", "postada")
ORDEM = {"carrinho": 0, "paga": 1, "gerada": 2, "postada": 3, "entregue": 4}
DIAS_RASTREIO = 90  # a conferência automática acompanha as etiquetas por até 90 dias
TIPOS = {"nota": "Com nota fiscal (chave da NF-e)", "declaracao": "Com declaração de conteúdo"}
LINK_RASTREIO = "https://www.melhorrastreio.com.br/rastreio/{}"


def ativas(o):
    return [e for e in o.etiquetas if e.situacao != "cancelada"]


def nota_autorizada(o):
    return next((n for n in reversed(o.notas) if n.status == "autorizada" and n.chave), None)


def servico_do_pedido(o):
    if (o.frete_codigo or "").startswith("me:"):
        return o.frete_codigo[3:]
    servicos = melhor_envio.ajustes()["servicos"]
    return servicos[0] if servicos else "1"


# ---------- remetente e destinatário ----------
def remetente():
    """Dados da empresa para o remetente. Devolve (dados, lista do que falta)."""
    import fiscal
    e = fiscal.emitente()
    d = {"name": e["razao_social"][:60], "phone": so_digitos(e["telefone"]),
         "email": config("empresa_email_privacidade", "") or current_app.config.get("MELHOR_ENVIO_CONTATO", ""),
         "company_document": so_digitos(e["cnpj"]), "state_register": so_digitos(e["ie"]),
         "address": e["logradouro"], "complement": e["complemento"], "number": e["numero"],
         "district": e["bairro"], "city": e["municipio"], "country_id": "BR",
         "postal_code": so_digitos(e["cep"]), "state_abbr": e["uf"]}
    rotulos = {"name": "razão social", "company_document": "CNPJ", "address": "rua", "number": "número",
               "district": "bairro", "city": "cidade", "postal_code": "CEP", "state_abbr": "estado",
               "phone": "telefone", "email": "e-mail (Regras de venda > e-mail para privacidade)"}
    return d, [r for k, r in rotulos.items() if not d.get(k)]


def preparar(o):
    """Valores iniciais do formulário: destinatário do pedido (endereço pelo CEP), serviço e nota ou declaração."""
    import fiscal
    p = fiscal.preparar(o)
    nota = nota_autorizada(o)
    # sem bairro ou cidade pelo CEP (cidade com CEP único, ViaCEP fora do ar), usa o texto "Rua, número, bairro, cidade"
    partes = [x.strip() for x in (o.address or "").split(",") if x.strip()]
    if not p["bairro"] and len(partes) >= 3:
        p["bairro"] = partes[2][:60]
    if not p["municipio"] and len(partes) >= 4:
        p["municipio"] = partes[3].split("-")[0].strip()[:60]
    return {"nome": p["nome"], "documento": p["documento"], "ie": p["ie"], "email": p["email"],
            "telefone": p["telefone"], "logradouro": p["logradouro"], "numero": p["numero"], "complemento": "",
            "bairro": p["bairro"], "municipio": p["municipio"], "uf": p["uf"], "cep": p["cep"],
            "servico": servico_do_pedido(o), "tipo": "nota" if nota else "declaracao",
            "chave": nota.chave if nota else ""}


def ler(f):
    """Valida o formulário. Devolve (dados, erros)."""
    import frete
    d = {k: campo(f, k) for k in ("nome", "logradouro", "numero", "complemento", "bairro", "municipio", "email")}
    d.update(documento=so_digitos(f.get("documento")), ie=so_digitos(f.get("ie")), cep=so_digitos(f.get("cep")),
             telefone=so_digitos(f.get("telefone"))[:14], uf=(f.get("uf") or "").strip().upper(),
             servico=(f.get("servico") or "").strip(), tipo=f.get("tipo", ""), chave=so_digitos(f.get("chave")))
    erros = []
    if not d["nome"]:
        erros.append("Informe o nome do destinatário.")
    if not ((len(d["documento"]) == 11 and cpf_valido(d["documento"])) or
            (len(d["documento"]) == 14 and cnpj_valido(d["documento"]))):
        erros.append("CPF ou CNPJ do destinatário inválido.")
    for k, rotulo in (("logradouro", "a rua"), ("numero", "o número (ou S/N)"), ("bairro", "o bairro"),
                      ("municipio", "a cidade")):
        if not d[k]:
            erros.append(f"Informe {rotulo} do destinatário.")
    if d["uf"] not in frete.UFS:
        erros.append("Estado do destinatário inválido.")
    if len(d["cep"]) != 8:
        erros.append("CEP do destinatário inválido.")
    elif d["uf"] in frete.UFS and frete.uf_do_cep(d["cep"]) != d["uf"]:
        erros.append(f"O CEP {frete.formatar_cep(d['cep'])} não é de {d['uf']}: confira o estado.")
    if d["servico"] not in melhor_envio.SERVICOS:
        erros.append("Escolha o serviço de entrega.")
    if d["tipo"] not in TIPOS:
        erros.append("Escolha se o envio vai com nota fiscal ou com declaração de conteúdo.")
    elif d["tipo"] == "nota" and len(d["chave"]) != 44:
        erros.append("A chave da nota fiscal tem 44 números.")
    elif d["tipo"] == "declaracao" and len(d["documento"]) == 14 and d["ie"]:
        erros.append("Cliente com inscrição estadual (contribuinte do ICMS) não pode receber com declaração de "
                     "conteúdo: emita a nota fiscal e use a chave dela.")
    return d, erros


def _destinatario(d):
    to = {"name": d["nome"], "phone": d["telefone"], "email": d["email"], "address": d["logradouro"],
          "complement": d["complemento"], "number": d["numero"], "district": d["bairro"], "city": d["municipio"],
          "country_id": "BR", "postal_code": d["cep"], "state_abbr": d["uf"]}
    if len(d["documento"]) == 14:
        to.update(company_document=d["documento"], state_register=d["ie"] or "ISENTO")
    else:
        to["document"] = d["documento"]
    return to


# ---------- cotação e compra ----------
def cotacao(o, cep, servico):
    """A cotação do serviço para os itens do pedido (com os volumes), ou None."""
    ops = melhor_envio.cotar(cep, melhor_envio.itens_do_pedido(o), servicos=[servico])
    return ops[0] if ops else None


def _produtos(o, pacote):
    """Produtos de um volume (nome, quantidade e valor unitário: vão na declaração de conteúdo)."""
    por_id = {}
    for i in o.items:
        atual = por_id.setdefault(str(i.product_id), {"name": (i.product.name if i.product else "Produto")[:100],
                                                       "quantity": 0, "valor": Decimal(0)})
        atual["quantity"] += i.quantity
        atual["valor"] += Decimal(i.unit_price) * i.quantity
    lista = []
    for p in (pacote.get("products") or [{"id": k, "quantity": v["quantity"]} for k, v in por_id.items()]):
        info = por_id.get(str(p.get("id")))
        if not info:
            continue
        unit = (info["valor"] / info["quantity"]).quantize(Decimal("0.01")) if info["quantity"] else Decimal(0)
        lista.append({"name": info["name"], "quantity": str(int(p.get("quantity") or 1)), "unitary_value": str(unit)})
    return lista


def _volume(pacote):
    dim = pacote.get("dimensions") or {}
    return {"height": int(Decimal(str(dim.get("height") or 1))), "width": int(Decimal(str(dim.get("width") or 1))),
            "length": int(Decimal(str(dim.get("length") or 1))), "weight": float(Decimal(str(pacote.get("weight") or 0)))}


def comprar(o, f, autor):
    """Inclui os volumes no carrinho do Melhor Envio e segue até a etiqueta pronta. Devolve (ok, mensagem)."""
    if ativas(o):
        return False, "Este pedido já tem etiqueta. Para trocar, cancele a atual antes."
    rem, faltam = remetente()
    if faltam:
        return False, "Complete os dados da empresa em Nota fiscal › Configuração: " + ", ".join(faltam) + "."
    d, erros = ler(f)
    if erros:
        return False, " ".join(erros)
    op = cotacao(o, d["cep"], d["servico"])
    if not op or not op["pacotes"]:
        return False, (f"O Melhor Envio não cotou {melhor_envio.SERVICOS.get(d['servico'], 'este serviço')} para o "
                       f"CEP {d['cep'][:5]}-{d['cep'][5:]} agora. Tente outro serviço ou mais tarde.")
    seguro = melhor_envio.ajustes()["seguro"]
    rem = dict(rem, state_register=rem["state_register"] if d["tipo"] == "nota" else "")
    sandbox = bool(current_app.config.get("MELHOR_ENVIO_SANDBOX"))
    for n, pacote in enumerate(op["pacotes"], start=1):
        produtos = _produtos(o, pacote)
        valor_conteudo = sum((Decimal(p["unitary_value"]) * int(p["quantity"]) for p in produtos), Decimal(0))
        opcoes = {"insurance_value": float(valor_conteudo) if seguro else 0, "receipt": False, "own_hand": False,
                  "reverse": False, "non_commercial": d["tipo"] == "declaracao", "platform": "Marketplace B2B",
                  "reminder": f"Pedido {o.id}" + (f" · volume {n}/{len(op['pacotes'])}" if len(op["pacotes"]) > 1 else ""),
                  "tags": [{"tag": str(o.id), "url": None}]}
        if d["tipo"] == "nota":
            opcoes["invoice"] = {"key": d["chave"]}
        corpo = {"service": int(d["servico"]), "from": rem, "to": _destinatario(d), "products": produtos,
                 "volumes": [_volume(pacote)], "options": opcoes}
        try:
            r = melhor_envio.api("POST", "/api/v2/me/cart", corpo)
        except ErroMelhorEnvio as e:
            db.session.commit()  # os volumes que já entraram no carrinho ficam gravados (dá para cancelar)
            return False, f"O Melhor Envio recusou o volume {n}: {e}"
        if not r.get("id"):
            db.session.commit()
            return False, f"O Melhor Envio não devolveu o número da etiqueta do volume {n}."
        o.etiquetas.append(EtiquetaEnvio(me_id=str(r["id"])[:40], protocolo=str(r.get("protocol") or "")[:40],
                                         servico=op["nome"][:80], valor=Decimal(str(r.get("price") or op["custo"])),
                                         situacao="carrinho", sandbox=sandbox, autor=(autor or "")[:120]))
        db.session.commit()
    return continuar(o)


def continuar(o):
    """Leva as etiquetas adiante: paga as do carrinho, gera as pagas e pede o link de impressão. Uma etapa que falha
    deixa a mensagem em `erro` e a etiqueta onde estava: chamar de novo retoma dali, sem pagar duas vezes.
    Devolve (ok, mensagem)."""
    def na(*situacoes):
        return [e for e in o.etiquetas if e.situacao in situacoes]

    def falhou(lista, texto):
        for x in lista:
            x.erro = texto[:500]
        db.session.commit()
        return False, texto

    pagar = na("carrinho")
    if pagar:
        try:
            melhor_envio.api("POST", "/api/v2/me/shipment/checkout", {"orders": [e.me_id for e in pagar]})
        except ErroMelhorEnvio as e:
            return falhou(pagar, f"Pagamento não feito: {e}. Confira o saldo da carteira no Melhor Envio.")
        for e in pagar:
            e.situacao, e.erro = "paga", ""
        db.session.commit()
    gerar = na("paga")
    if gerar:
        try:
            r = melhor_envio.api("POST", "/api/v2/me/shipment/generate", {"orders": [e.me_id for e in gerar]})
        except ErroMelhorEnvio as e:
            return falhou(gerar, f"Etiqueta paga, mas não gerada: {e}")
        problemas = []
        for e in gerar:
            res = (r or {}).get(e.me_id) or {}
            if res.get("status") is False:
                e.erro = ("Etiqueta paga, mas não gerada: " + str(res.get("message") or "sem motivo"))[:500]
                problemas.append(e.erro)
            else:
                e.situacao, e.erro = "gerada", ""
        db.session.commit()
        if problemas:
            return False, "; ".join(problemas)
    imprimir = [e for e in na("gerada", "postada") if not e.link_impressao]
    if imprimir:
        try:
            r = melhor_envio.api("POST", "/api/v2/me/shipment/print",
                                 {"mode": "public", "orders": [e.me_id for e in imprimir]})
        except ErroMelhorEnvio as e:
            return falhou(imprimir, f"Etiqueta gerada, mas o link de impressão falhou: {e}")
        for e in imprimir:
            e.link_impressao, e.erro = str((r or {}).get("url") or "")[:300], ""
        db.session.commit()
    return True, "Etiqueta pronta para imprimir."


def aplicar(e, info):
    """Aplica numa etiqueta a situação e o rastreio vindos do Melhor Envio (consulta ou webhook).
    Devolve True se algo mudou."""
    import historico
    status = str(info.get("status") or "")
    antes = (e.situacao, e.rastreio, e.erro)
    nova = DA_API.get(status, e.situacao)
    if nova == "cancelada" or (e.situacao != "cancelada" and ORDEM.get(nova, 0) >= ORDEM.get(e.situacao, 0)):
        e.situacao = nova  # só avança: um aviso atrasado (ex.: "paga" depois de "postada") não volta a etapa
    e.rastreio = str(info.get("tracking") or info.get("self_tracking") or info.get("melhorenvio_tracking")
                     or e.rastreio or "")[:40]
    alerta = ALERTAS.get(status)
    if alerta:
        e.erro = alerta
    elif e.erro in ALERTAS.values():
        e.erro = ""
    mudou = (e.situacao, e.rastreio, e.erro) != antes
    if alerta and alerta != antes[2]:
        historico.registrar(e.order_id, f"Melhor Envio: {alerta}", autor="sistema")
    return mudou


def rastrear(o):
    """Atualiza a situação e o rastreio. Postada: o pedido vira "enviado" (e o cliente é avisado); tudo entregue:
    "entregue". Devolve (ok, mensagem)."""
    lista = [e for e in o.etiquetas if e.situacao in EM_ANDAMENTO]
    if not lista:
        return True, "Nada para rastrear."
    try:
        r = melhor_envio.api("POST", "/api/v2/me/shipment/tracking", {"orders": [e.me_id for e in lista]})
    except ErroMelhorEnvio as e:
        return False, f"Rastreio indisponível agora: {e}"
    for e in lista:
        aplicar(e, (r or {}).get(e.me_id) or {})
    db.session.commit()
    return True, _sincronizar_pedido(o)


def rastrear_pendentes():
    """Conferência automática (tarefa agendada): atualiza todas as etiquetas em andamento dos últimos 90 dias, de
    100 em 100, e leva cada pedido a "enviado" ou "entregue" (com o e-mail ao cliente). Devolve um resumo."""
    from datetime import timedelta
    from b2b import salvar_config
    resumo = {"etiquetas": 0, "mudaram": 0, "pedidos_atualizados": 0, "erro": ""}
    if not melhor_envio.tem_token():
        resumo["erro"] = "sem MELHOR_ENVIO_TOKEN"
        return resumo
    desde = agora_utc() - timedelta(days=DIAS_RASTREIO)
    lista = (EtiquetaEnvio.query.filter(EtiquetaEnvio.situacao.in_(EM_ANDAMENTO), EtiquetaEnvio.criado_em >= desde)
             .order_by(EtiquetaEnvio.id).all())
    resumo["etiquetas"] = len(lista)
    pedidos = set()
    for i in range(0, len(lista), 100):
        lote = lista[i:i + 100]
        try:
            r = melhor_envio.api("POST", "/api/v2/me/shipment/tracking", {"orders": [e.me_id for e in lote]})
        except ErroMelhorEnvio as erro:
            resumo["erro"] = str(erro)[:300]
            current_app.logger.warning("Rastreio automático do Melhor Envio: %s", erro)
            break
        for e in lote:
            if aplicar(e, (r or {}).get(e.me_id) or {}):
                resumo["mudaram"] += 1
                pedidos.add(e.order_id)
        db.session.commit()
    from models import Order
    for oid in sorted(pedidos):
        o = db.session.get(Order, oid)
        envio_antes = o.envio
        _sincronizar_pedido(o)
        if o.envio != envio_antes:
            resumo["pedidos_atualizados"] += 1
    salvar_config("rastreio_ultima", agora_utc().strftime("%Y-%m-%d %H:%M"))
    db.session.commit()
    return resumo


def _assinatura_ok(corpo, assinatura):
    """X-ME-Signature: HMAC-SHA256 do corpo com o secret do aplicativo (em base64; aceita também hexadecimal)."""
    import base64
    import hashlib
    import hmac
    segredo = current_app.config.get("MELHOR_ENVIO_SECRET", "")
    if not segredo or not assinatura:
        return False
    digest = hmac.new(segredo.encode(), corpo, hashlib.sha256).digest()
    assinatura = assinatura.strip()
    return (hmac.compare_digest(assinatura.encode(), base64.b64encode(digest)) or
            hmac.compare_digest(assinatura.lower().encode(), digest.hex().encode()))


def receber_webhook(corpo, assinatura):
    """Aviso do Melhor Envio (evento "order.*"). Devolve o código HTTP: 200 recebido (mesmo se a etiqueta não é
    nossa, para o Melhor Envio não reenviar), 401 assinatura inválida, 400 corpo inválido."""
    import json
    if not _assinatura_ok(corpo, assinatura):
        return 401
    try:
        evento = json.loads(corpo.decode())
        dados = evento.get("data") or {}
    except (ValueError, AttributeError, UnicodeDecodeError):
        return 400
    e = EtiquetaEnvio.query.filter_by(me_id=str(dados.get("id") or "")[:40]).first()
    if not e:
        return 200
    if aplicar(e, dados):
        db.session.commit()
        from models import Order
        _sincronizar_pedido(db.session.get(Order, e.order_id))
    return 200


def _sincronizar_pedido(o):
    import avisos
    import historico
    validas = ativas(o)
    codigos = [e.rastreio for e in validas if e.rastreio]
    msg = "Rastreio atualizado."
    if o.envio == "" and any(e.situacao in ("postada", "entregue") for e in validas):
        o.transportadora = (validas[0].servico.split(" ")[0] or "Melhor Envio")[:80]
        o.rastreio = ", ".join(codigos)[:80]
        o.rastreio_link = LINK_RASTREIO.format(codigos[0])[:300] if codigos else ""
        o.envio, o.enviado_em = "enviado", agora_utc()
        db.session.commit()
        avisos.pedido_enviado(o)
        historico.registrar(o.id, f"Postado ({validas[0].servico})" + (f" · rastreio {o.rastreio}" if o.rastreio else ""),
                            autor="sistema")
        msg = "Postado: o pedido foi marcado como enviado e o cliente recebeu o rastreio por e-mail."
    if o.envio == "enviado" and validas and all(e.situacao == "entregue" for e in validas):
        o.envio, o.entregue_em = "entregue", agora_utc()
        db.session.commit()
        avisos.pedido_entregue(o)
        historico.registrar(o.id, "Entregue (rastreio do Melhor Envio)", autor="sistema")
        msg = "Entregue: o pedido foi marcado como entregue e o cliente foi avisado."
    return msg


def cancelar(o, eid, autor):
    """Cancela uma etiqueta: no carrinho, só tira de lá; paga ou gerada, o valor volta para a carteira."""
    import historico
    e = next((x for x in o.etiquetas if x.id == eid), None)
    if not e or e.situacao == "cancelada":
        return False, "Etiqueta não encontrada."
    if e.situacao in ("postada", "entregue"):
        return False, "Etiqueta já postada não pode ser cancelada."
    try:
        if e.situacao == "carrinho":
            melhor_envio.api("DELETE", f"/api/v2/me/cart/{e.me_id}")
        else:
            melhor_envio.api("POST", "/api/v2/me/shipment/cancel",
                             {"order": {"id": e.me_id, "reason_id": "2", "description": f"Cancelada pela loja ({autor})"[:200]}})
    except ErroMelhorEnvio as erro:
        return False, f"O Melhor Envio não cancelou: {erro}"
    e.situacao, e.erro = "cancelada", ""
    db.session.commit()
    historico.registrar(o.id, f"Etiqueta {e.protocolo or e.me_id[:8]} cancelada ({e.servico})", nome=autor)
    return True, "Etiqueta cancelada" + ("." if e.valor == 0 else ": o valor volta para a carteira do Melhor Envio.")
