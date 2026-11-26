"""Nota fiscal eletrônica (NF-e, modelo 55) a partir dos pedidos.

Aqui ficam os dados fiscais da empresa, as regras fiscais (CFOP, ICMS, IPI, PIS, COFINS), a montagem da nota a
partir do pedido, a numeração e o registro das notas e dos eventos (cancelamento, carta de correção e
inutilização). O envio é de nfe_provedores.py: hoje direto na SEFAZ; depois, uma API, sem mudar nada daqui.

Primeiro em HOMOLOGAÇÃO (ambiente de testes da SEFAZ, sem valor fiscal). Só depois de conferir com o contador,
troque para PRODUÇÃO em Nota fiscal > Configuração.
"""
import io
import re
import zipfile
from datetime import timedelta
from decimal import Decimal

from flask import current_app

import formacao
import frete
import historico
import nfe_provedores as prov
from b2b import cnpj_valido, config, cpf_valido, salvar_config, so_digitos
from formato import arredondar, brl, campo, decimal_ou, inicio_do_dia
from models import Configuracao, NotaEvento, NotaFiscal, PedidoArquivo, PerfilFiscal, agora_utc, db

CENTAVO = Decimal("0.01")
CEM = Decimal(100)
STATUS = {"processando": "Processando", "autorizada": "Autorizada", "rejeitada": "Rejeitada", "denegada": "Denegada",
          "cancelada": "Cancelada", "erro": "Sem resposta", "inutilizada": "Número inutilizado"}
AMBIENTES = {"2": "Homologação (testes, sem valor fiscal)", "1": "Produção (nota com valor fiscal)"}
CRT = {"1": "Simples Nacional", "2": "Simples Nacional, excesso de sublimite", "3": "Regime normal (Presumido ou Real)"}
SITUACOES = {"101": "101 · Simples, com permissão de crédito", "102": "102 · Simples, sem permissão de crédito",
             "103": "103 · Simples, isenção por faixa de receita", "300": "300 · Imune", "400": "400 · Não tributada pelo Simples",
             "500": "500 · ICMS já cobrado por substituição tributária (Simples)",
             "00": "00 · Tributada integralmente", "40": "40 · Isenta", "41": "41 · Não tributada",
             "60": "60 · ICMS já cobrado por substituição tributária"}
ORIGENS = {0: "0 · Nacional", 1: "1 · Estrangeira, importação direta", 2: "2 · Estrangeira, comprada no Brasil",
           3: "3 · Nacional com mais de 40% de conteúdo importado", 4: "4 · Nacional, processos básicos",
           5: "5 · Nacional com até 40% de conteúdo importado", 6: "6 · Estrangeira sem similar (CAMEX), importação direta",
           7: "7 · Estrangeira sem similar (CAMEX), comprada no Brasil", 8: "8 · Nacional com mais de 70% importado"}
IPI_CST = {"": "Sem IPI (comércio não equiparado à indústria)", "50": "50 · Saída tributada", "51": "51 · Alíquota zero",
           "52": "52 · Isenta", "53": "53 · Não tributada", "54": "54 · Imune", "55": "55 · Suspensão",
           "99": "99 · Outras saídas"}
PIS_CST = {"01": "01 · Tributável, alíquota básica", "04": "04 · Monofásica (revenda a alíquota zero)",
           "06": "06 · Alíquota zero", "07": "07 · Isenta", "08": "08 · Sem incidência", "09": "09 · Suspensão",
           "49": "49 · Outras operações de saída", "99": "99 · Outras operações"}
MOD_FRETE = {"0": "Por conta do emitente (CIF)", "1": "Por conta do destinatário (FOB)", "9": "Sem frete"}
INDICADOR_IE = {"1": "Contribuinte do ICMS (tem inscrição estadual)", "2": "Contribuinte isento de inscrição",
                "9": "Não contribuinte (pessoa física ou empresa sem IE)"}
TPAG = {"pix": ("17", ""), "boleto": ("15", ""), "faturado": ("15", ""), "cartao": ("03", ""),
        "whatsapp": ("99", "Combinado com a loja"), "orcamento": ("99", "Combinado com a loja")}
FRASE_SIMPLES = "DOCUMENTO EMITIDO POR ME OU EPP OPTANTE PELO SIMPLES NACIONAL. NAO GERA DIREITO A CREDITO FISCAL DE IPI."

CAMPOS_EMITENTE = {"razao_social": "Razão social", "nome_fantasia": "Nome fantasia", "cnpj": "CNPJ",
                   "ie": "Inscrição estadual", "crt": "Regime tributário", "logradouro": "Rua",
                   "numero": "Número", "complemento": "Complemento", "bairro": "Bairro", "municipio": "Cidade",
                   "cod_municipio": "Código IBGE da cidade", "uf": "Estado", "cep": "CEP", "telefone": "Telefone"}
CAMPOS_CONFIG = {"ambiente": "2", "serie": "1", "natureza": "Venda de mercadoria", "info": "", "rt_cnpj": "",
                 "rt_contato": "", "rt_email": "", "rt_fone": "", "enviar_email": "1"}


# ---------- configuração da empresa ----------
def emitente():
    """Dados fiscais da empresa (com o que já existe em Regras de venda como ponto de partida)."""
    d = {k: config(f"nfe_{k}", "") for k in CAMPOS_EMITENTE}
    d.update({k: config(f"nfe_{k}", padrao) for k, padrao in CAMPOS_CONFIG.items()})
    if not d["razao_social"]:
        d["razao_social"] = config("empresa_razao_social", "")
    if not d["cnpj"]:
        d["cnpj"] = so_digitos(config("empresa_cnpj", ""))
    d["proximo_1"] = config(f"nfe_numero_1_{d['serie']}", "1")
    d["proximo_2"] = config(f"nfe_numero_2_{d['serie']}", "1")
    return d


def configurada():
    """Lista do que falta para emitir (vazia = pronto)."""
    d = emitente()
    faltam = [rotulo for k, rotulo in CAMPOS_EMITENTE.items() if k not in ("nome_fantasia", "complemento", "telefone")
              and not d.get(k)]
    if not PerfilFiscal.query.filter_by(padrao=True).first():
        faltam.append("regra fiscal padrão")
    return faltam


def ler_config(f):
    """Valida a configuração fiscal. Devolve (dados, erro)."""
    d = {k: campo(f, k) for k in CAMPOS_EMITENTE}
    for k in ("cnpj", "cod_municipio", "cep", "telefone"):
        d[k] = so_digitos(d[k])
    ie = (f.get("ie") or "").strip().upper()
    d["ie"] = "ISENTO" if ie == "ISENTO" else so_digitos(ie)
    d["uf"] = d["uf"].upper()
    if not d["razao_social"]:
        return None, "Informe a razão social."
    if not cnpj_valido(d["cnpj"]):
        return None, "CNPJ da empresa inválido."
    if not d["ie"] or (d["ie"] != "ISENTO" and not 2 <= len(d["ie"]) <= 14):
        return None, "Informe a inscrição estadual (só números)."
    if d["crt"] not in CRT:
        return None, "Escolha o regime tributário."
    for k in ("logradouro", "numero", "bairro", "municipio"):
        if not d[k]:
            return None, f"Informe {CAMPOS_EMITENTE[k].lower()} do endereço da empresa."
    if len(d["cod_municipio"]) != 7:
        return None, "O código IBGE da cidade tem 7 números (busque pelo CEP)."
    if d["uf"] not in frete.UFS:
        return None, "Estado inválido."
    if len(d["cep"]) != 8:
        return None, "CEP da empresa inválido."
    d["ambiente"] = f.get("ambiente") if f.get("ambiente") in AMBIENTES else "2"
    try:
        d["serie"] = str(int(f.get("serie") or 1))
        proximos = {a: int(f.get(f"proximo_{a}") or 1) for a in ("1", "2")}
    except ValueError:
        return None, "Série e próximo número precisam ser números inteiros."
    if not 0 <= int(d["serie"]) <= 889:
        return None, "A série vai de 0 a 889."
    if not all(1 <= n <= 999_999_999 for n in proximos.values()):
        return None, "O próximo número vai de 1 a 999.999.999."
    d["natureza"] = campo(f, "natureza") or "Venda de mercadoria"
    d["enviar_email"] = "1" if f.get("enviar_email") else "0"
    d["info"] = (f.get("info") or "").strip()[:2000]
    d["rt_cnpj"], d["rt_fone"] = so_digitos(f.get("rt_cnpj")), so_digitos(f.get("rt_fone"))[:14]
    d["rt_contato"], d["rt_email"] = campo(f, "rt_contato"), campo(f, "rt_email")
    if d["rt_cnpj"] and not cnpj_valido(d["rt_cnpj"]):
        return None, "CNPJ do responsável técnico inválido."
    return {**d, "proximos": proximos}, None


def gravar_config(d):
    proximos = d.pop("proximos")
    for k, v in d.items():
        salvar_config(f"nfe_{k}", v)
    for amb, n in proximos.items():
        usado = db.session.query(db.func.max(NotaFiscal.numero)).filter_by(ambiente=int(amb), serie=int(d["serie"])).scalar()
        salvar_config(f"nfe_numero_{amb}_{d['serie']}", max(n, (usado or 0) + 1))


def consultar_cep(cep):
    """Endereço pelo CEP (ViaCEP, gratuito): {logradouro, bairro, municipio, uf, cod_municipio} ou None."""
    cep = frete.normalizar_cep(cep or "")
    if not cep or current_app.config.get("TESTING") and not current_app.config.get("VIACEP_NOS_TESTES"):
        return None
    import requests
    try:
        r = requests.get(f"https://viacep.com.br/ws/{cep}/json/", timeout=5)
        j = r.json() if r.status_code == 200 else {}
    except Exception:
        return None
    if not j or j.get("erro"):
        return None
    return {"logradouro": j.get("logradouro", ""), "bairro": j.get("bairro", ""), "municipio": j.get("localidade", ""),
            "uf": j.get("uf", ""), "cod_municipio": j.get("ibge", ""), "cep": cep}


# ---------- regras fiscais ----------
def criar_perfil_padrao():
    crt = config("nfe_crt", "1")
    if crt == "3":
        p = PerfilFiscal(nome="Revenda (regime normal)", padrao=True, icms_situacao="00", pis_cst="01",
                         pis_aliquota=Decimal("0.65"), cofins_cst="01", cofins_aliquota=Decimal("3.00"))
    else:
        p = PerfilFiscal(nome="Revenda (Simples Nacional)", padrao=True, icms_situacao="102", pis_cst="99",
                         cofins_cst="99")
    db.session.add(p)
    return p


def perfil_padrao():
    return PerfilFiscal.query.filter_by(padrao=True).first()


def perfil_de(p, cache=None):
    cache = {} if cache is None else cache
    pid = p.perfil_fiscal_id or 0
    if pid not in cache:
        cache[pid] = (db.session.get(PerfilFiscal, pid) if pid else None) or perfil_padrao()
    return cache[pid]


def _pct(f, k, rotulo, vazio=Decimal(0)):
    return formacao.ler_pct(f.get(k, ""), rotulo, vazio=vazio)


def ler_perfil(f):
    nome = campo(f, "nome")
    if not nome:
        return None, "Dê um nome à regra (ex.: Revenda, Produtos com ST)."
    cfops = [so_digitos(f.get(k)) for k in ("cfop_estadual", "cfop_interestadual")]
    if not all(len(c) == 4 for c in cfops) or cfops[0][0] != "5" or cfops[1][0] != "6":
        return None, "CFOP com 4 números: dentro do estado começa com 5 (ex.: 5102) e para outro estado com 6 (6102)."
    situacao = f.get("icms_situacao")
    if situacao not in SITUACOES:
        return None, "Escolha a situação do ICMS (CSOSN ou CST)."
    dados = {"nome": nome, "cfop_estadual": cfops[0], "cfop_interestadual": cfops[1], "icms_situacao": situacao,
             "ipi_cst": f.get("ipi_cst") if f.get("ipi_cst") in IPI_CST else "",
             "pis_cst": f.get("pis_cst") if f.get("pis_cst") in PIS_CST else "99",
             "cofins_cst": f.get("cofins_cst") if f.get("cofins_cst") in PIS_CST else "99",
             "ipi_enquadramento": so_digitos(f.get("ipi_enquadramento"))[:3] or "999",
             "info_adicional": (f.get("info_adicional") or "").strip()[:500], "padrao": "padrao" in f}
    for k, rotulo, vazio in (("icms_aliquota", "a alíquota do ICMS", None), ("credito_sn", "o crédito do Simples", Decimal(0)),
                             ("ipi_aliquota", "a alíquota do IPI", Decimal(0)), ("pis_aliquota", "a alíquota do PIS", Decimal(0)),
                             ("cofins_aliquota", "a alíquota da COFINS", Decimal(0))):
        dados[k], erro = _pct(f, k, rotulo, vazio)
        if erro:
            return None, erro
    return dados, None


def ler_fiscal_produto(f):
    """Campos fiscais do cadastro do produto. Devolve (dados, erro)."""
    ncm, cest, ean = so_digitos(f.get("ncm")), so_digitos(f.get("cest")), so_digitos(f.get("ean"))
    if ncm and len(ncm) != 8:
        return None, "O NCM tem 8 números (ex.: 8207.70.10)."
    if cest and len(cest) != 7:
        return None, "O CEST tem 7 números."
    if ean and len(ean) not in (8, 12, 13, 14):
        return None, "O código de barras (EAN/GTIN) tem 8, 12, 13 ou 14 números."
    try:
        origem = int(f.get("origem") or 0)
    except ValueError:
        origem = -1
    if origem not in ORIGENS:
        return None, "Origem da mercadoria inválida."
    unidade = re.sub(r"[^A-Z0-9]", "", (f.get("unidade") or "UN").upper())[:6] or "UN"
    perfil = f.get("perfil_fiscal_id", type=int) if hasattr(f, "getlist") else None
    if perfil and not db.session.get(PerfilFiscal, perfil):
        perfil = None
    return {"ncm": ncm, "cest": cest, "ean": ean, "origem": origem, "unidade": unidade,
            "perfil_fiscal_id": perfil or None}, None


# ---------- montagem da nota ----------
def _ratear(total, pesos):
    """Divide `total` proporcional aos pesos, em centavos (a sobra vai para o último)."""
    total = arredondar(total)
    soma = sum(pesos, Decimal(0))
    if not total or not soma:
        return [Decimal(0)] * len(pesos)
    partes = [arredondar(total * p / soma) for p in pesos[:-1]]
    return partes + [total - sum(partes, Decimal(0))]


def aliquota_icms(uf_emit, uf_dest):
    """Alíquota do ICMS próprio: a interna do estado da loja ou a do estado de destino (Formação de preço);
    sem tabela, a interestadual de 7% ou 12%."""
    tabela = formacao.parametros()["ufs"]
    if uf_emit == uf_dest:
        return (tabela.get(uf_emit) or {}).get("icms")
    linha = (tabela.get(uf_dest) or {}).get("icms")
    return linha if linha is not None else formacao.icms_interestadual(uf_emit, uf_dest)


def _numero_do_endereco(texto):
    m = re.search(r",\s*(\d+[A-Za-z]?)\b", texto or "") or re.search(r"\b(\d{1,6})\b", texto or "")
    return m.group(1) if m else "S/N"


def _resp_tecnico(e):
    """Responsável técnico pelo sistema (exigido em vários estados): o configurado ou a própria empresa, desde que
    haja e-mail e telefone de verdade; sem eles, o grupo fica de fora."""
    rt = {"cnpj": e["rt_cnpj"] or e["cnpj"], "contato": e["rt_contato"] or e["razao_social"][:60],
          "email": e["rt_email"] or config("empresa_email_privacidade", ""), "fone": e["rt_fone"] or e["telefone"]}
    return rt if rt["email"] and rt["fone"] else {}


def preparar(o):
    """Valores iniciais do formulário de emissão: destinatário do pedido (com o endereço pelo CEP) e frete."""
    from models import User
    cliente = db.session.get(User, o.user_id) if o.user_id else None
    doc = so_digitos(o.documento or (cliente.documento if cliente else ""))
    ie = so_digitos(o.inscricao_estadual or "")
    end = consultar_cep(o.cep) or {}
    rua = (o.address or "").split(",")[0].strip()
    return {"nome": (o.razao_social or o.customer_name or "")[:60], "documento": doc, "ie": ie,
            "indicador_ie": "1" if ie and len(doc) == 14 else "9", "email": o.customer_email or "",
            "logradouro": (end.get("logradouro") or rua)[:60], "numero": _numero_do_endereco(o.address),
            "complemento": "", "bairro": end.get("bairro", "")[:60], "municipio": end.get("municipio", "")[:60],
            "cod_municipio": end.get("cod_municipio", ""), "uf": end.get("uf") or frete.uf_do_cep(o.cep or "00000000") or "",
            "cep": o.cep or "", "telefone": so_digitos(o.customer_phone)[:14],
            "consumidor_final": "on" if len(doc) == 11 or not ie else "",
            "mod_frete": "0" if o.frete else "9", "volumes": "1", "info": ""}


def ler_destinatario(f):
    d = {k: campo(f, k) for k in ("nome", "logradouro", "numero", "complemento", "bairro", "municipio")}
    d.update(documento=so_digitos(f.get("documento")), ie=so_digitos(f.get("ie")), cep=so_digitos(f.get("cep")),
             cod_municipio=so_digitos(f.get("cod_municipio")), uf=(f.get("uf") or "").strip().upper(),
             email=campo(f, "email"), telefone=so_digitos(f.get("telefone"))[:14],
             indicador_ie=f.get("indicador_ie") if f.get("indicador_ie") in INDICADOR_IE else "9")
    erros = []
    if not d["nome"]:
        erros.append("Informe o nome ou a razão social do destinatário.")
    if not ((len(d["documento"]) == 11 and cpf_valido(d["documento"])) or
            (len(d["documento"]) == 14 and cnpj_valido(d["documento"]))):
        erros.append("CPF ou CNPJ do destinatário inválido.")
    if d["indicador_ie"] == "1" and not 2 <= len(d["ie"]) <= 14:
        erros.append("Destinatário contribuinte precisa da inscrição estadual.")
    for k, rotulo in (("logradouro", "a rua"), ("numero", "o número"), ("bairro", "o bairro"), ("municipio", "a cidade")):
        if not d[k]:
            erros.append(f"Informe {rotulo} do destinatário.")
    if len(d["cod_municipio"]) != 7:
        erros.append("O código IBGE da cidade do destinatário tem 7 números (use Buscar pelo CEP).")
    if d["uf"] not in frete.UFS:
        erros.append("Estado do destinatário inválido.")
    if len(d["cep"]) != 8:
        erros.append("CEP do destinatário inválido.")
    return d, erros


def montar(o, f, numero=0):
    """Monta a nota do pedido com o formulário de emissão. Devolve (DadosNota, avisos, erros)."""
    e = emitente()
    erros = [f"Configure em Nota fiscal > Configuração: {', '.join(configurada())}."] if configurada() else []
    dest, erros_dest = ler_destinatario(f)
    erros += erros_dest
    if erros:
        return None, [], erros
    uf_e, uf_d = e["uf"], dest["uf"]
    consumidor_final = 1 if ("consumidor_final" in f or dest["indicador_ie"] == "9") else 0
    if uf_e != uf_d and consumidor_final and dest["indicador_ie"] == "9" and e["crt"] == "3":
        erros.append("Venda a consumidor não contribuinte de outro estado precisa do DIFAL na nota (grupo ICMSUFDest), "
                     "que ainda não está pronto neste sistema. Emita esta pelo emissor da contabilidade.")
    mod_frete = int(f.get("mod_frete")) if f.get("mod_frete") in MOD_FRETE else 9
    try:
        volumes = max(int(f.get("volumes") or 0), 0)
    except ValueError:
        volumes = 0
    itens_pedido = sorted(o.items, key=lambda i: i.id or 0)
    brutos = [arredondar(Decimal(i.unit_price) * i.quantity) for i in itens_pedido]
    descontos = _ratear(decimal_ou(o.desconto), brutos)
    fretes = _ratear(decimal_ou(o.frete), brutos) if o.frete else [Decimal(0)] * len(brutos)
    cache, itens, avisos, infos = {}, [], [], []
    for i, bruto, desc, fr in zip(itens_pedido, brutos, descontos, fretes):
        p = i.product
        perfil = perfil_de(p, cache)
        if not p.ncm:
            erros.append(f"{p.name}: falta o NCM no cadastro do produto.")
            continue
        sit = perfil.icms_situacao
        item = prov.Item(codigo=(p.codigo or str(p.id))[:60], descricao=(p.name + (" (fracionado)" if i.fracionado else ""))[:120],
                         ncm=p.ncm, cest=p.cest or "", ean=p.ean or "", origem=p.origem or 0, unidade=p.unidade or "UN",
                         cfop=perfil.cfop_estadual if uf_e == uf_d else perfil.cfop_interestadual,
                         quantidade=Decimal(i.quantity), valor_unitario=Decimal(i.unit_price), valor_total=bruto,
                         desconto=desc, frete=fr, icms_situacao=sit)
        liquido = bruto - desc
        if sit == "00":
            aliq = perfil.icms_aliquota if perfil.icms_aliquota is not None else aliquota_icms(uf_e, uf_d)
            if aliq is None:
                erros.append(f"Falta a alíquota interna de ICMS de {uf_e} (Formação de preço ou na regra fiscal).")
                continue
            item.icms_aliquota, item.icms_base = decimal_ou(aliq), liquido + fr
            item.icms_valor = arredondar(item.icms_base * item.icms_aliquota / CEM)
        elif sit == "101":
            item.credito_sn_aliquota = decimal_ou(perfil.credito_sn)
            item.credito_sn_valor = arredondar(liquido * item.credito_sn_aliquota / CEM)
        if perfil.ipi_cst:
            item.ipi_cst, item.ipi_enquadramento = perfil.ipi_cst, perfil.ipi_enquadramento or "999"
            if perfil.ipi_cst in ("50", "99") and perfil.ipi_aliquota:
                item.ipi_base, item.ipi_aliquota = liquido + fr, decimal_ou(perfil.ipi_aliquota)
                item.ipi_valor = arredondar(item.ipi_base * item.ipi_aliquota / CEM)
        for imposto in ("pis", "cofins"):
            cst, aliq = getattr(perfil, f"{imposto}_cst"), decimal_ou(getattr(perfil, f"{imposto}_aliquota"))
            setattr(item, f"{imposto}_cst", cst)
            if cst in ("01", "02"):
                setattr(item, f"{imposto}_base", liquido)
                setattr(item, f"{imposto}_aliquota", aliq)
                setattr(item, f"{imposto}_valor", arredondar(liquido * aliq / CEM))
        if perfil.info_adicional and perfil.info_adicional not in infos:
            infos.append(perfil.info_adicional)
        itens.append(item)
    if erros:
        return None, [], erros
    ipi = sum((i.ipi_valor for i in itens), Decimal(0))
    if ipi:
        avisos.append(f"O IPI soma {brl(ipi)} ao total da nota (o pedido foi fechado sem IPI).")
    credito = sum((i.credito_sn_valor for i in itens), Decimal(0))
    texto = [e["info"]] if e["info"] else []
    if e["crt"] in ("1", "2") and FRASE_SIMPLES not in e["info"]:
        texto.append(FRASE_SIMPLES)
    if credito:
        aliq = itens[0].credito_sn_aliquota
        texto.append(f"PERMITE O APROVEITAMENTO DO CREDITO DE ICMS NO VALOR DE R$ {credito:.2f}, CORRESPONDENTE A "
                     f"ALIQUOTA DE {aliq:.2f}%, NOS TERMOS DO ART. 23 DA LC 123/2006.".replace(".", ",", 1))
    texto += infos
    if (f.get("info") or "").strip():
        texto.append(f.get("info").strip()[:1000])
    texto.append(f"Pedido {o.id}")
    dados = prov.DadosNota(
        ambiente=int(e["ambiente"]), serie=int(e["serie"]), numero=numero, emissao=prov.agora_brasilia(),
        natureza=e["natureza"][:60],
        emitente=prov.Emitente(cnpj=e["cnpj"], ie=e["ie"], razao_social=e["razao_social"][:60], crt=e["crt"],
                               nome_fantasia=e["nome_fantasia"][:60],
                               endereco=prov.Endereco(e["logradouro"], e["numero"], e["bairro"], e["municipio"],
                                                      e["cod_municipio"], uf_e, e["cep"], e["complemento"], e["telefone"])),
        destinatario=prov.Destinatario(nome=dest["nome"], documento=dest["documento"],
                                       indicador_ie=int(dest["indicador_ie"]), ie=dest["ie"], email=dest["email"],
                                       endereco=prov.Endereco(dest["logradouro"], dest["numero"], dest["bairro"],
                                                              dest["municipio"], dest["cod_municipio"], uf_d,
                                                              dest["cep"], dest["complemento"], dest["telefone"])),
        itens=itens, pagamentos=[], id_destino=1 if uf_e == uf_d else 2, consumidor_final=consumidor_final,
        mod_frete=mod_frete, volumes=volumes,
        peso=sum((decimal_ou(i.product.peso_kg) * i.quantity for i in itens_pedido), Decimal(0)).quantize(Decimal("0.001")),
        info_complementar=" ".join(texto)[:5000],
        resp_tecnico=_resp_tecnico(e))
    total = dados.total
    tpag, descricao = TPAG.get(o.payment_method, ("99", "Combinado com a loja"))
    dados.pagamentos = [prov.Pagamento(tipo=tpag, valor=total, a_prazo=o.payment_method == "faturado", descricao=descricao)]
    parcelas = [l for l in o.lancamentos if l.tipo == "receber"] if o.payment_method == "faturado" else []
    if parcelas:
        valores = _ratear(total, [decimal_ou(l.valor) for l in parcelas])
        dados.duplicatas = [prov.Duplicata(numero=str(n).zfill(3), vencimento=l.vencimento, valor=v)
                            for n, (l, v) in enumerate(zip(parcelas, valores), 1)]
    return dados, avisos, []


# ---------- numeração ----------
def reservar_numero(ambiente, serie):
    """Próximo número da série, sem repetir mesmo com dois cliques ao mesmo tempo (UPDATE só se ninguém mudou)."""
    from sqlalchemy import select, update
    chave = f"nfe_numero_{ambiente}_{serie}"
    if db.session.get(Configuracao, chave) is None:
        salvar_config(chave, 1)
        db.session.flush()
    for _ in range(50):
        atual = int(db.session.execute(select(Configuracao.valor).where(Configuracao.chave == chave)).scalar() or 1)
        r = db.session.execute(update(Configuracao).where(Configuracao.chave == chave, Configuracao.valor == str(atual))
                               .values(valor=str(atual + 1)).execution_options(synchronize_session=False))
        if r.rowcount == 1 and not NotaFiscal.query.filter_by(ambiente=ambiente, serie=serie, numero=atual).first():
            return atual
    raise RuntimeError("Não foi possível reservar o número da nota. Tente de novo.")


# ---------- emissão e eventos ----------
def nota_aberta(o):
    """A nota do pedido que ainda pode ser (re)enviada: rejeitada, sem resposta ou processando."""
    return next((n for n in reversed(o.notas) if n.status in ("rejeitada", "erro", "processando")), None)


def nota_valida(o, ambiente=None):
    """A nota autorizada do pedido no ambiente atual (a de homologação não impede emitir a de produção)."""
    ambiente = ambiente or int(emitente()["ambiente"])
    return next((n for n in o.notas if n.status == "autorizada" and n.ambiente == ambiente), None)


def _historico(oid, texto):
    """Linha no histórico do pedido, na mesma gravação da nota (sem commit à parte)."""
    from models import PedidoEvento
    db.session.add(PedidoEvento(order_id=oid, autor="loja", autor_nome=(historico._nome_do_admin() or "")[:120],
                                texto=" ".join(texto.split())[:500]))


def _aplicar(nota, r):
    nota.status, nota.cstat, nota.motivo = r.status, r.cstat or "", (r.motivo or "")[:500]
    nota.chave = r.chave or nota.chave
    nota.protocolo = r.protocolo or nota.protocolo
    if r.xml:
        nota.xml = r.xml
    if r.status == "autorizada":
        nota.autorizada_em = agora_utc()


def danfe_pdf(xml):
    """DANFE em PDF a partir do XML autorizado (BrazilFiscalReport)."""
    from brazilfiscalreport.danfe import Danfe
    return bytes(Danfe(xml=xml).output())


def _anexar(o, nome, mime, dados, tipo):
    o.arquivos.append(PedidoArquivo(tipo=tipo, nome=nome[:120], mime=mime, tamanho=len(dados), dados=dados))


def _anexar_nota(nota):
    """XML e DANFE no pedido: o cliente baixa em Minha conta > Notas e boletos. Nota de homologação (teste) não vai
    para o cliente: baixe pela tela da nota."""
    o = nota.order
    if not o or nota.ambiente != 1:
        return ""
    sufixo = "" if nota.ambiente == 1 else "-homologacao"
    _anexar(o, f"NF-e {nota.numero}{sufixo}.xml", "application/xml", nota.xml.encode(), "xml")
    try:
        _anexar(o, f"DANFE NF-e {nota.numero}{sufixo}.pdf", "application/pdf", danfe_pdf(nota.xml), "nota")
        return ""
    except Exception as e:  # o XML é o que vale; o DANFE dá para gerar de novo na tela da nota
        current_app.logger.error("DANFE da NF-e %s: %s", nota.numero, e)
        return " O DANFE não pôde ser gerado agora (o XML foi anexado)."


def enviar_ao_cliente(nota):
    """Manda o DANFE e o XML da nota autorizada para o e-mail do pedido. Devolve (enviou?, mensagem)."""
    o = nota.order
    if not o or not o.customer_email:
        return False, "O pedido não tem e-mail do cliente."
    if nota.status != "autorizada" or nota.ambiente != 1:
        return False, "Só nota autorizada em produção vai para o cliente."
    import avisos
    from email_utils import enviar_email_detalhado
    anexos = [(f"NF-e {nota.numero}.xml", nota.xml.encode(), "application/xml")]
    try:
        anexos.insert(0, (f"DANFE NF-e {nota.numero}.pdf", danfe_pdf(nota.xml), "application/pdf"))
    except Exception as e:
        current_app.logger.error("DANFE da NF-e %s para o e-mail: %s", nota.numero, e)
    texto = (f"Olá, {o.customer_name}!\n\nA nota fiscal do seu pedido #{o.id} foi emitida.\n\n"
             f"NF-e nº {nota.numero}, série {nota.serie}\nChave de acesso: {nota.chave}\nValor: R$ {nota.valor:.2f}\n\n"
             "Em anexo, o DANFE (PDF) e o arquivo XML (guarde o XML: ele é a nota fiscal de verdade).\n"
             f"Eles também ficam na sua conta, em Notas e boletos: {avisos._link_cliente(o)}\n\nEquipe Marketplace B2B")
    ok, motivo = enviar_email_detalhado(o.customer_email, f"Marketplace B2B: nota fiscal do pedido #{o.id}", texto, anexos)
    if ok and o.id:
        _historico(o.id, f"NF-e {nota.numero} enviada por e-mail para {o.customer_email}")
        db.session.commit()
    return ok, (f"Nota enviada para {o.customer_email}." if ok else f"Não foi possível enviar o e-mail: {motivo}")


def _avisar_cliente(nota):
    """Depois de gravar: manda a nota ao cliente (se ligado na configuração). Falha no e-mail não desfaz a nota."""
    if nota.status == "autorizada" and nota.ambiente == 1 and emitente().get("enviar_email", "1") == "1":
        ok, msg = enviar_ao_cliente(nota)
        return "" if ok else f" ({msg})"
    return ""


def _finalizar(nota):
    extra = _anexar_nota(nota)
    if nota.order_id:
        _historico(nota.order_id, f"NF-e {nota.numero} autorizada" + ("" if nota.ambiente == 1 else " (homologação)")
                   + f" · protocolo {nota.protocolo}")
    return extra


def emitir(o, f, autor=""):
    """Emite (ou reenvia) a NF-e do pedido. Devolve (ok, mensagem, avisos)."""
    if nota_valida(o):
        return False, "Este pedido já tem NF-e autorizada.", []
    dados, avisos, erros = montar(o, f)
    if erros:
        return False, " ".join(erros), []
    p = prov.provedor()
    ok, msg = p.configurado()
    if not ok:
        return False, msg, []
    nota = nota_aberta(o)
    if nota and nota.status in ("erro", "processando") and nota.chave:  # pode ter chegado: confere antes
        try:
            r = p.consultar(dados.emitente.endereco.uf, nota.ambiente, nota.chave, _nfe_do_xml(nota.xml))
        except prov.ErroProvedor as e:
            return False, str(e), []
        if r.status == "autorizada" and r.xml:
            _aplicar(nota, r)
            extra = _finalizar(nota)
            db.session.commit()
            extra += _avisar_cliente(nota)
            return True, f"A NF-e {nota.numero} já estava autorizada na SEFAZ (protocolo {nota.protocolo}).{extra}", avisos
    if nota and nota.ambiente != dados.ambiente:
        nota.status = "erro"  # mudou o ambiente: a numeração é outra
        nota = None
    if not nota:
        nota = NotaFiscal(order=o, ambiente=dados.ambiente, serie=dados.serie,
                          numero=reservar_numero(dados.ambiente, dados.serie))
        db.session.add(nota)
    nota.status, nota.valor, nota.destinatario = "processando", arredondar(dados.total), dados.destinatario.nome[:160]
    nota.provedor, nota.autor, nota.serie = p.nome, autor[:120], dados.serie
    dados.numero = nota.numero
    db.session.commit()  # o número fica reservado mesmo se a SEFAZ demorar
    try:
        r = p.emitir(dados)
    except prov.ErroProvedor as e:
        r = prov.Resposta(ok=False, status="erro", motivo=str(e))
    except Exception as e:
        current_app.logger.exception("Emissão da NF-e %s", nota.numero)
        r = prov.Resposta(ok=False, status="erro", motivo=f"Falha ao montar ou enviar a nota: {e}")
    _aplicar(nota, r)
    extra = _finalizar(nota) if r.status == "autorizada" else ""
    db.session.commit()
    extra += _avisar_cliente(nota)
    if r.status == "autorizada":
        return True, f"NF-e {nota.numero} autorizada (protocolo {nota.protocolo}).{extra}", avisos
    if r.status == "denegada":
        return False, f"NF-e {nota.numero} DENEGADA pela SEFAZ ({r.cstat}: {r.motivo}). O número fica usado.", avisos
    if r.status == "erro":
        return False, f"NF-e {nota.numero}: {r.motivo}", avisos
    return False, f"NF-e {nota.numero} rejeitada ({r.cstat}: {r.motivo}). Corrija e envie de novo (o número é o mesmo).", avisos


def _nfe_do_xml(xml):
    """O elemento NFe (assinado) dentro do XML guardado."""
    if not xml:
        return ""
    from lxml import etree
    raiz = etree.fromstring(xml.encode(), etree.XMLParser(resolve_entities=False, no_network=True))
    nfe = raiz if raiz.tag.endswith("}NFe") else raiz.find(f".//{{{prov.NS}}}NFe")
    return etree.tostring(nfe, encoding="unicode") if nfe is not None else ""


def consultar(nota):
    """Situação na SEFAZ. Acerta a nota se ela foi autorizada ou cancelada sem a loja saber."""
    if not nota.chave:
        return False, "Esta nota ainda não tem chave de acesso (não chegou a ser montada)."
    try:
        r = prov.provedor().consultar(emitente()["uf"], nota.ambiente, nota.chave, _nfe_do_xml(nota.xml))
    except prov.ErroProvedor as e:
        return False, str(e)
    except Exception as e:
        return False, f"Sem resposta da SEFAZ ({e.__class__.__name__})."
    if r.status == "autorizada" and nota.status != "autorizada" and r.xml:
        _aplicar(nota, r)
        extra = _finalizar(nota)
        db.session.commit()
        extra += _avisar_cliente(nota)
        return True, f"A SEFAZ confirma: NF-e {nota.numero} autorizada.{extra}"
    if r.status == "cancelada" and nota.status != "cancelada":
        nota.status, nota.cancelada_em = "cancelada", agora_utc()
        db.session.commit()
    return r.ok, f"SEFAZ: {r.cstat} · {r.motivo}"


def _evento(nota, tipo, texto, r, autor, sequencia=1):
    ev = NotaEvento(nota=nota, tipo=tipo, texto=texto[:1000], ok=r.ok, cstat=r.cstat, motivo=(r.motivo or "")[:500],
                    protocolo=r.protocolo, xml=r.xml, autor=autor[:120], sequencia=sequencia)
    db.session.add(ev)
    return ev


def cancelar(nota, justificativa, autor=""):
    justificativa = " ".join((justificativa or "").split())
    if nota.status != "autorizada":
        return False, "Só nota autorizada pode ser cancelada."
    if not 15 <= len(justificativa) <= 255:
        return False, "Escreva o motivo do cancelamento com 15 a 255 caracteres."
    e = emitente()
    try:
        r = prov.provedor().cancelar(e["uf"], nota.ambiente, e["cnpj"], nota.chave, nota.protocolo, justificativa)
    except prov.ErroProvedor as erro:
        return False, str(erro)
    except Exception as erro:
        return False, f"Sem resposta da SEFAZ ({erro.__class__.__name__}). Tente de novo."
    _evento(nota, "cancelamento", justificativa, r, autor)
    if r.ok:
        nota.status, nota.cancelada_em = "cancelada", agora_utc()
        if nota.order and nota.ambiente == 1:
            _anexar(nota.order, f"Cancelamento NF-e {nota.numero}.xml", "application/xml", r.xml.encode(), "xml")
        if nota.order_id:
            _historico(nota.order_id, f"NF-e {nota.numero} cancelada: {justificativa}")
    db.session.commit()
    return r.ok, (f"NF-e {nota.numero} cancelada (protocolo {r.protocolo})." if r.ok
                  else f"Cancelamento recusado ({r.cstat}: {r.motivo}).")


def carta_correcao(nota, texto, autor=""):
    texto = " ".join((texto or "").split())
    if nota.status != "autorizada":
        return False, "A carta de correção é só para nota autorizada."
    if not 15 <= len(texto) <= 1000:
        return False, "Escreva a correção com 15 a 1.000 caracteres."
    sequencia = sum(1 for ev in nota.eventos if ev.tipo == "correcao" and ev.ok) + 1
    if sequencia > 20:
        return False, "A SEFAZ aceita até 20 cartas de correção por nota."
    e = emitente()
    try:
        r = prov.provedor().carta_correcao(e["uf"], nota.ambiente, e["cnpj"], nota.chave, texto, sequencia)
    except prov.ErroProvedor as erro:
        return False, str(erro)
    except Exception as erro:
        return False, f"Sem resposta da SEFAZ ({erro.__class__.__name__}). Tente de novo."
    _evento(nota, "correcao", texto, r, autor, sequencia)
    if r.ok and nota.order and nota.ambiente == 1:
        _anexar(nota.order, f"Carta de correcao {sequencia} NF-e {nota.numero}.xml", "application/xml", r.xml.encode(), "xml")
    if r.ok and nota.order_id:
        _historico(nota.order_id, f"Carta de correção {sequencia} da NF-e {nota.numero}: {texto}")
    db.session.commit()
    return r.ok, (f"Carta de correção {sequencia} registrada." if r.ok else f"Carta recusada ({r.cstat}: {r.motivo}).")


def inutilizar(inicio, fim, justificativa, autor=""):
    justificativa = " ".join((justificativa or "").split())
    e = emitente()
    amb, serie = int(e["ambiente"]), int(e["serie"])
    if not (1 <= inicio <= fim <= 999_999_999 and fim - inicio < 10_000):
        return False, "Informe a faixa de números (do menor para o maior)."
    if not 15 <= len(justificativa) <= 255:
        return False, "Escreva o motivo com 15 a 255 caracteres."
    usadas = NotaFiscal.query.filter(NotaFiscal.ambiente == amb, NotaFiscal.serie == serie,
                                     NotaFiscal.numero.between(inicio, fim),
                                     NotaFiscal.status.in_(("autorizada", "cancelada", "denegada"))).count()
    if usadas:
        return False, "Há notas autorizadas, canceladas ou denegadas nessa faixa: só número que nunca virou nota pode ser inutilizado."
    try:
        r = prov.provedor().inutilizar(e["uf"], amb, e["cnpj"], serie, inicio, fim, justificativa)
    except prov.ErroProvedor as erro:
        return False, str(erro)
    except Exception as erro:
        return False, f"Sem resposta da SEFAZ ({erro.__class__.__name__}). Tente de novo."
    _evento(None, "inutilizacao", f"Série {serie}, números {inicio} a {fim}{' (homologação)' if amb == 2 else ''}: "
                                  f"{justificativa}", r, autor)
    if r.ok:
        for n in NotaFiscal.query.filter(NotaFiscal.ambiente == amb, NotaFiscal.serie == serie,
                                         NotaFiscal.numero.between(inicio, fim)).all():
            n.status = "inutilizada"
        chave = f"nfe_numero_{amb}_{serie}"
        if int(config(chave, "1") or 1) <= fim:
            salvar_config(chave, fim + 1)
    db.session.commit()
    return r.ok, (f"Números {inicio} a {fim} inutilizados (protocolo {r.protocolo})." if r.ok
                  else f"Inutilização recusada ({r.cstat}: {r.motivo}).")


def status_sefaz():
    e = emitente()
    if not e.get("uf"):
        return False, "Preencha o estado da empresa primeiro."
    try:
        r = prov.provedor().status_servico(e["uf"], int(e["ambiente"]))
    except prov.ErroProvedor as erro:
        return False, str(erro)
    except Exception as erro:
        return False, f"Sem resposta da SEFAZ ({erro.__class__.__name__})."
    return r.ok, f"SEFAZ {e['uf']} ({AMBIENTES[e['ambiente']].split(' (')[0].lower()}): {r.cstat} · {r.motivo}"


# ---------- arquivos para o contador ----------
def xmls_do_periodo(de, ate):
    """ZIP com os XMLs de notas autorizadas/canceladas, eventos e inutilizações do período (para o contador)."""
    inicio, fim = inicio_do_dia(de), inicio_do_dia(ate + timedelta(days=1))
    notas = NotaFiscal.query.filter(NotaFiscal.ambiente == 1, NotaFiscal.status.in_(("autorizada", "cancelada")),
                                    NotaFiscal.autorizada_em >= inicio, NotaFiscal.autorizada_em < fim).all()
    eventos = NotaEvento.query.join(NotaFiscal, isouter=True).filter(
        NotaEvento.ok.is_(True), NotaEvento.quando >= inicio, NotaEvento.quando < fim).all()
    saida = io.BytesIO()
    with zipfile.ZipFile(saida, "w", zipfile.ZIP_DEFLATED) as z:
        for n in notas:
            z.writestr(f"{n.chave}-nfe.xml", n.xml or "")
        for ev in eventos:
            if (ev.nota and ev.nota.ambiente != 1) or (not ev.nota and "(homologação)" in ev.texto):
                continue
            nome = f"{ev.nota.chave}-{ev.tipo}-{ev.sequencia}.xml" if ev.nota else f"inutilizacao-{ev.id}.xml"
            z.writestr(nome, ev.xml or "")
    return saida.getvalue(), len(notas)
