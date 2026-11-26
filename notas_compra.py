"""Notas de compra: as NF-e que os fornecedores emitem para o CNPJ da loja, buscadas na SEFAZ (Distribuição DF-e,
com o mesmo certificado A1) ou enviadas em XML, e lançadas em Compras com um clique.

Como a SEFAZ funciona:
- a busca devolve primeiro um RESUMO da nota (emitente, valor, data). Para baixar o XML completo, a loja manifesta
  "ciência da operação"; o XML chega na busca seguinte (ou na hora, pela chave);
- a SEFAZ limita as buscas: quando não há nada novo, só deixa buscar de novo depois de 1 hora (o sistema espera).
"""
import re
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from lxml import etree

import fiscal
import nfe_provedores as prov
from b2b import config, data_config, salvar_config, so_digitos
from formato import data_br, decimal_ou, hora_br
from models import (Compra, CompraItem, Fornecedor, NotaRecebida, Product, ProdutoFornecedor, agora_utc, db)

NS = {"n": prov.NS}
ESPERA = timedelta(hours=1)
MAX_ARQUIVO = 3_000_000
SITUACAO_RESUMO = {"1": "autorizada", "2": "denegada", "3": "cancelada"}


class ErroNota(Exception):
    pass


def _parser():
    return etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, remove_blank_text=True)


def _txt(no, caminho):
    achado = no.find(caminho, NS) if no is not None else None
    return (achado.text or "").strip() if achado is not None else ""


def _quando(texto):
    try:
        d = datetime.fromisoformat(texto)
        return (d - d.utcoffset()).replace(tzinfo=None) if d.utcoffset() is not None else d
    except (ValueError, TypeError):
        return None


# ---------- leitura do XML ----------
def ler_xml(xml):
    """Os dados de uma NF-e (nfeProc ou NFe): emitente, destinatário, itens, frete e duplicatas."""
    raiz = etree.fromstring(xml.encode() if isinstance(xml, str) else xml, _parser())
    if raiz.getroottree().docinfo.doctype:  # NF-e nunca tem DOCTYPE: entidades podem atacar quem ler o XML depois
        raise ErroNota("O arquivo não é um XML de NF-e válido (tem declarações DOCTYPE).")
    inf = raiz if raiz.tag.endswith("}infNFe") else raiz.find(".//n:infNFe", NS)
    if inf is None:
        raise ErroNota("O arquivo não é uma NF-e (não tem o grupo infNFe).")
    emit, dest, ide, tot = (inf.find(f"n:{g}", NS) for g in ("emit", "dest", "ide", "total/n:ICMSTot"))
    itens = []
    for det in inf.findall("n:det", NS):
        prod, imp = det.find("n:prod", NS), det.find("n:imposto", NS)
        ipi = imp.find(".//n:IPITrib", NS) if imp is not None else None
        icms = imp.find("n:ICMS", NS) if imp is not None else None
        st = decimal_ou(_txt(icms[0], "n:vICMSST")) if icms is not None and len(icms) else Decimal(0)
        ean = _txt(prod, "n:cEAN")
        itens.append({"codigo": _txt(prod, "n:cProd"), "descricao": _txt(prod, "n:xProd"), "ncm": _txt(prod, "n:NCM"),
                      "cest": _txt(prod, "n:CEST"), "ean": "" if ean.upper().startswith("SEM") else ean,
                      "unidade": _txt(prod, "n:uCom"), "quantidade": decimal_ou(_txt(prod, "n:qCom")),
                      "valor_unitario": decimal_ou(_txt(prod, "n:vUnCom")), "valor": decimal_ou(_txt(prod, "n:vProd")),
                      "desconto": decimal_ou(_txt(prod, "n:vDesc")), "ipi_pct": decimal_ou(_txt(ipi, "n:pIPI")),
                      "st": st})
    dups = [{"numero": _txt(d, "n:nDup"), "vencimento": _txt(d, "n:dVenc"), "valor": decimal_ou(_txt(d, "n:vDup"))}
            for d in inf.findall("n:cobr/n:dup", NS)]
    return {"chave": inf.get("Id", "")[3:], "numero": int(_txt(ide, "n:nNF") or 0), "serie": int(_txt(ide, "n:serie") or 0),
            "emissao": _quando(_txt(ide, "n:dhEmi")),
            "emitente": {"cnpj": _txt(emit, "n:CNPJ") or _txt(emit, "n:CPF"), "nome": _txt(emit, "n:xNome"),
                         "uf": _txt(emit, "n:enderEmit/n:UF"), "fone": _txt(emit, "n:enderEmit/n:fone")},
            "destinatario": _txt(dest, "n:CNPJ") or _txt(dest, "n:CPF"), "valor": decimal_ou(_txt(tot, "n:vNF")),
            "frete": decimal_ou(_txt(tot, "n:vFrete")), "itens": itens, "duplicatas": dups}


# ---------- guardar o que chegou ----------
def _nota(chave):
    return NotaRecebida.query.filter_by(chave=chave).first()


def _guardar_completa(xml, nsu="", origem="sefaz"):
    d = ler_xml(xml)
    nr = _nota(d["chave"])
    novo = nr is None or nr.tipo != "completa"
    nr = nr or NotaRecebida(chave=d["chave"])
    nr.tipo, nr.xml, nr.origem = "completa", xml, nr.origem if nr.id else origem
    nr.nsu = nsu or nr.nsu or ""
    nr.cnpj_emitente, nr.nome_emitente = d["emitente"]["cnpj"], d["emitente"]["nome"][:160]
    nr.numero, nr.serie, nr.valor, nr.emitida_em = d["numero"], d["serie"], d["valor"], d["emissao"]
    db.session.add(nr)
    return novo


def _guardar_documento(doc):
    """Um documento da SEFAZ (resumo, nota completa ou evento). Devolve 1 se trouxe novidade."""
    schema = doc["schema"]
    raiz = etree.fromstring(doc["xml"].encode(), _parser())
    if schema.startswith("procNFe"):
        return int(_guardar_completa(doc["xml"], doc["nsu"]))
    if schema.startswith("resNFe"):
        chave = _txt(raiz, "n:chNFe") or _txt(raiz, ".//n:chNFe")
        if not chave:
            return 0
        nr = _nota(chave)
        situacao = SITUACAO_RESUMO.get(_txt(raiz, "n:cSitNFe"), "autorizada")
        if nr:
            nr.situacao = situacao
            return 0
        db.session.add(NotaRecebida(
            chave=chave, nsu=doc["nsu"], tipo="resumo", situacao=situacao, cnpj_emitente=_txt(raiz, "n:CNPJ"),
            nome_emitente=_txt(raiz, "n:xNome")[:160], valor=decimal_ou(_txt(raiz, "n:vNF")),
            emitida_em=_quando(_txt(raiz, "n:dhEmi")), numero=int(chave[25:34]), serie=int(chave[22:25])))
        return 1
    if schema.startswith(("resEvento", "procEventoNFe")):
        tipo, chave = _txt(raiz, ".//n:tpEvento"), _txt(raiz, ".//n:chNFe")
        nr = _nota(chave) if chave else None
        if nr and tipo == "110111":  # o fornecedor cancelou a nota
            nr.situacao = "cancelada"
            return 1
    return 0


def estado():
    return {"ult_nsu": config("dfe_ult_nsu", "0"), "liberado_em": data_config("dfe_liberado_em"),
            "ultima": data_config("dfe_ultima_busca")}


def _bloquear():
    salvar_config("dfe_liberado_em", (agora_utc() + ESPERA).isoformat(timespec="seconds"))


def _contexto():
    e = fiscal.emitente()
    if not (e.get("cnpj") and e.get("uf")):
        raise ErroNota("Preencha os dados da empresa em Notas fiscais > Configuração.")
    p = prov.provedor()
    ok, msg = p.configurado()
    if not ok:
        raise ErroNota(msg)
    return e, p


def buscar():
    """Busca na SEFAZ os documentos novos para o CNPJ da loja. Devolve (ok, mensagem)."""
    try:
        e, p = _contexto()
    except ErroNota as erro:
        return False, str(erro)
    liberado = estado()["liberado_em"]
    if liberado and agora_utc() < liberado:
        hora = hora_br(liberado)[-5:]
        return False, f"A SEFAZ só permite uma nova busca a partir das {hora} (espera de 1 hora quando não há novidade)."
    ult, novidades = config("dfe_ult_nsu", "0") or "0", 0
    try:
        for _ in range(10):  # até 500 documentos por clique
            r = p.distribuicao(e["uf"], int(e["ambiente"]), e["cnpj"], ult)
            if r["cstat"] == "656":  # consumo indevido: a SEFAZ pede para esperar
                _bloquear()
                db.session.commit()
                return False, "A SEFAZ pediu para esperar 1 hora antes de buscar de novo."
            if r["cstat"] not in ("137", "138"):
                return False, f"SEFAZ: {r['cstat']} · {r['motivo']}"
            for doc in r["docs"]:
                novidades += _guardar_documento(doc)
            ult = r["ult_nsu"]
            salvar_config("dfe_ult_nsu", ult)
            if r["cstat"] == "137" or int(ult or 0) >= int(r["max_nsu"] or 0):
                _bloquear()  # em dia com a SEFAZ: a próxima busca só depois de 1 hora
                break
    except prov.ErroProvedor as erro:
        db.session.commit()
        return False, str(erro)
    except Exception as erro:
        db.session.commit()
        return False, f"Sem resposta da SEFAZ ({erro.__class__.__name__}). Tente mais tarde."
    salvar_config("dfe_ultima_busca", agora_utc().isoformat(timespec="seconds"))
    db.session.commit()
    return True, (f"{novidades} nota(s) ou atualização(ões) nova(s)." if novidades else "Nenhuma nota nova.")


def ciencia(nr):
    """Manifesta ciência da operação e tenta baixar o XML completo na hora."""
    if nr.tipo == "completa":
        return True, "O XML desta nota já está aqui."
    try:
        e, p = _contexto()
        r = p.manifestar(e["uf"], int(e["ambiente"]), e["cnpj"], nr.chave, 2)
    except ErroNota as erro:
        return False, str(erro)
    except prov.ErroProvedor as erro:
        return False, str(erro)
    except Exception as erro:
        return False, f"Sem resposta da SEFAZ ({erro.__class__.__name__}). Tente mais tarde."
    if not (r.ok or r.cstat == "573"):  # 573 = a ciência já tinha sido dada
        return False, f"A SEFAZ recusou a ciência ({r.cstat}: {r.motivo})."
    nr.manifestacao = "ciencia"
    db.session.commit()
    try:
        d = p.distribuicao(e["uf"], int(e["ambiente"]), e["cnpj"], chave=nr.chave)
        for doc in d.get("docs", []):
            _guardar_documento(doc)
        db.session.commit()
    except Exception:
        db.session.rollback()
    if nr.tipo == "completa":
        return True, "Ciência registrada e XML baixado."
    return True, "Ciência registrada. O XML completo costuma chegar em alguns minutos: busque de novo mais tarde."


def importar_arquivo(dados):
    """XML de nota de compra enviado pelo painel (o fornecedor mandou por e-mail). Devolve a NotaRecebida."""
    if not dados or len(dados) > MAX_ARQUIVO:
        raise ErroNota("Envie o arquivo XML da nota (até 3 MB).")
    try:
        texto = dados.decode("utf-8-sig")
        d = ler_xml(texto)
    except (UnicodeDecodeError, etree.XMLSyntaxError) as e:
        raise ErroNota("O arquivo não é um XML de NF-e válido.") from e
    meu = so_digitos(fiscal.emitente().get("cnpj", ""))
    if not meu:
        raise ErroNota("Preencha o CNPJ da empresa em Notas fiscais > Configuração.")
    if d["destinatario"] != meu:
        raise ErroNota("Esta nota não foi emitida para o CNPJ da loja.")
    if not re.fullmatch(r"\d{44}", d["chave"]):
        raise ErroNota("A nota não tem chave de acesso válida.")
    _guardar_completa(texto, origem="arquivo")
    db.session.commit()
    return _nota(d["chave"])


# ---------- lançar em Compras ----------
def sugestoes(nr, d=None):
    """Para cada item da nota, o produto da loja que parece ser ele: pelo código do fornecedor, pelo código de
    barras ou pelo código da loja. Devolve [(item, product_id ou None, fator)]."""
    d = d or ler_xml(nr.xml)
    forn = Fornecedor.query.filter_by(documento=d["emitente"]["cnpj"]).first()
    saida = []
    for item in d["itens"]:
        link = (ProdutoFornecedor.query.filter_by(fornecedor_id=forn.id, codigo=item["codigo"]).first()
                if forn and item["codigo"] else None)
        if link:
            saida.append((item, link.product_id, link.fator or 1))
            continue
        p = (Product.query.filter_by(ean=item["ean"]).first() if item["ean"] else None) or \
            (Product.query.filter_by(codigo=item["codigo"]).first() if item["codigo"] else None)
        saida.append((item, p.id if p else None, 1))
    return saida


def _condicao(d):
    """Prazo de pagamento em dias a partir das duplicatas da nota (ex.: 30/60/90)."""
    base = data_br(d["emissao"]) if d["emissao"] else None
    dias = []
    for dup in d["duplicatas"]:
        try:
            venc = datetime.strptime(dup["vencimento"], "%Y-%m-%d").date()
        except ValueError:
            continue
        if base:
            dias.append(str(max((venc - base).days, 0)))
    return "/".join(dias[:12])


def lancar(nr, f, autor=""):
    """Cria a compra (como enviada, pronta para receber) com os produtos escolhidos. Devolve (compra, erro)."""
    if nr.tipo != "completa":
        return None, "Baixe o XML da nota antes (ciência da operação)."
    if nr.compra_id:
        return None, "Esta nota já foi lançada em Compras."
    if nr.situacao != "autorizada":
        return None, "Esta nota foi cancelada pelo fornecedor."
    d = ler_xml(nr.xml)
    forn = Fornecedor.query.filter_by(documento=d["emitente"]["cnpj"]).first()
    if not forn:
        forn = Fornecedor(nome=d["emitente"]["nome"][:160] or "Fornecedor", documento=d["emitente"]["cnpj"][:14],
                          uf=d["emitente"]["uf"][:2], telefone=d["emitente"]["fone"][:30], transporte="fornecedor",
                          ativo=True)
        db.session.add(forn)
    c = Compra(fornecedor=forn, status="enviado", nota=str(d["numero"]), autor=autor[:120], enviado_em=agora_utc(),
               frete=d["frete"].quantize(Decimal("0.01")), condicao=_condicao(d),
               observacoes=f"Lançada da NF-e {d['numero']} (chave {d['chave']}).")
    for n, item in enumerate(d["itens"]):
        pid = f.get(f"produto_{n}", type=int)
        if not pid:
            continue
        p = db.session.get(Product, pid)
        if not p:
            continue
        try:
            fator = max(int(f.get(f"fator_{n}") or 1), 1)
        except ValueError:
            fator = 1
        unidades = item["quantidade"] * fator
        if unidades != unidades.to_integral_value() or unidades <= 0:
            db.session.rollback()
            return None, (f"Item {n + 1} ({item['descricao'][:40]}): {item['quantidade']} × {fator} não dá um número "
                          "inteiro de unidades. Ajuste as unidades por item da nota.")
        unidades = int(unidades)
        preco = ((item["valor"] - item["desconto"]) / unidades).quantize(Decimal("0.0001"), ROUND_HALF_UP)
        st = (item["st"] * 100 / item["valor"]).quantize(Decimal("0.01")) if item["st"] and item["valor"] else Decimal(0)
        c.itens.append(CompraItem(product=p, quantidade=unidades, preco=preco, ipi_pct=item["ipi_pct"], st_pct=st))
        link = next((x for x in p.custos if x.fornecedor is forn or (forn.id and x.fornecedor_id == forn.id)), None)
        if link:  # lembra o código e a conversão para a próxima nota (o preço muda no recebimento)
            link.codigo, link.fator = item["codigo"][:60], fator
        else:
            db.session.add(ProdutoFornecedor(product=p, fornecedor=forn, preco_compra=preco, ipi_pct=item["ipi_pct"],
                                             st_pct=st, codigo=item["codigo"][:60], fator=fator, principal=not p.custos))
    if not c.itens:
        db.session.rollback()
        return None, "Escolha o produto da loja de pelo menos um item da nota."
    db.session.add(c)
    nr.compra = c
    db.session.commit()
    return c, None
