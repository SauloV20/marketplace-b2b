"""Envio da NF-e: a nota em formato neutro (DadosNota) e os provedores que sabem mandá-la.

- ProvedorSefaz (padrão): direto nos webservices da SEFAZ do estado, com o certificado A1 da empresa (PyNFe).
- ProvedorApi: lugar pronto para uma API de emissão (Focus NFe, NFE.io, PlugNotas...). Para ligar: defina
  NFE_PROVEDOR=api, NFE_API_URL e NFE_API_TOKEN e escreva o envio em ProvedorApi._chamar (o formato dos dados já
  sai pronto em ProvedorApi.payload). O resto do sistema (telas, numeração, arquivos, financeiro) não muda.

Todo provedor devolve uma Resposta com o mesmo formato, e é isso que fiscal.py grava.
"""
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal

from flask import current_app

NS = "http://www.portalfiscal.inf.br/nfe"
TEXTO_HOMOLOGACAO = "NF-E EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL"
DENEGADA = {"110", "301", "302", "303"}  # o número fica usado e a nota não vale


class ErroProvedor(Exception):
    pass


# ---------- a nota em formato neutro ----------
@dataclass
class Endereco:
    logradouro: str
    numero: str
    bairro: str
    municipio: str
    cod_municipio: str
    uf: str
    cep: str
    complemento: str = ""
    telefone: str = ""


@dataclass
class Emitente:
    cnpj: str
    ie: str
    razao_social: str
    crt: str  # 1 Simples Nacional | 2 Simples (excesso de sublimite) | 3 regime normal
    endereco: Endereco
    nome_fantasia: str = ""


@dataclass
class Destinatario:
    nome: str
    documento: str  # CPF (11) ou CNPJ (14)
    indicador_ie: int  # 1 contribuinte | 2 isento | 9 não contribuinte
    endereco: Endereco
    ie: str = ""
    email: str = ""


@dataclass
class Item:
    codigo: str
    descricao: str
    ncm: str
    cfop: str
    unidade: str
    quantidade: Decimal
    valor_unitario: Decimal
    valor_total: Decimal
    origem: int = 0
    cest: str = ""
    ean: str = ""
    desconto: Decimal = Decimal(0)
    frete: Decimal = Decimal(0)
    icms_situacao: str = "102"
    icms_base: Decimal = Decimal(0)
    icms_aliquota: Decimal = Decimal(0)
    icms_valor: Decimal = Decimal(0)
    credito_sn_aliquota: Decimal = Decimal(0)
    credito_sn_valor: Decimal = Decimal(0)
    ipi_cst: str = ""
    ipi_enquadramento: str = "999"
    ipi_base: Decimal = Decimal(0)
    ipi_aliquota: Decimal = Decimal(0)
    ipi_valor: Decimal = Decimal(0)
    pis_cst: str = "99"
    pis_base: Decimal = Decimal(0)
    pis_aliquota: Decimal = Decimal(0)
    pis_valor: Decimal = Decimal(0)
    cofins_cst: str = "99"
    cofins_base: Decimal = Decimal(0)
    cofins_aliquota: Decimal = Decimal(0)
    cofins_valor: Decimal = Decimal(0)


@dataclass
class Pagamento:
    tipo: str  # tPag: 01 dinheiro, 03 cartão de crédito, 15 boleto, 17 Pix, 99 outros
    valor: Decimal
    a_prazo: bool = False
    descricao: str = ""  # obrigatória quando o tipo é 99 (outros)


@dataclass
class Duplicata:
    numero: str
    vencimento: date
    valor: Decimal


@dataclass
class DadosNota:
    ambiente: int  # 1 produção | 2 homologação
    serie: int
    numero: int
    emissao: datetime  # com fuso (horário de Brasília)
    natureza: str
    emitente: Emitente
    destinatario: Destinatario
    itens: list
    pagamentos: list
    id_destino: int = 1  # 1 operação interna | 2 interestadual
    consumidor_final: int = 0
    mod_frete: int = 9  # 0 por conta do emitente | 1 do destinatário | 9 sem frete
    volumes: int = 0
    peso: Decimal = Decimal(0)
    duplicatas: list = field(default_factory=list)
    info_complementar: str = ""
    resp_tecnico: dict = field(default_factory=dict)  # cnpj, contato, email, fone

    @property
    def total(self):
        return sum((i.valor_total - i.desconto + i.frete + i.ipi_valor for i in self.itens), Decimal(0))


@dataclass
class Resposta:
    ok: bool
    status: str  # autorizada | rejeitada | denegada | erro | registrado | homologada | em_operacao | cancelada
    cstat: str = ""
    motivo: str = ""
    chave: str = ""
    protocolo: str = ""
    xml: str = ""  # nfeProc (autorizada), procEventoNFe (evento) ou o XML enviado (para conferir a rejeição)
    quando: datetime = None


# ---------- provedores ----------
class Provedor:
    nome = ""

    def configurado(self):
        """(True, "") ou (False, o que falta)."""
        return False, "Provedor não definido."

    def status_servico(self, uf, ambiente):
        raise NotImplementedError

    def emitir(self, dados):
        raise NotImplementedError

    def consultar(self, uf, ambiente, chave, xml_enviado=""):
        raise NotImplementedError

    def cancelar(self, uf, ambiente, cnpj, chave, protocolo, justificativa):
        raise NotImplementedError

    def carta_correcao(self, uf, ambiente, cnpj, chave, texto, sequencia):
        raise NotImplementedError

    def inutilizar(self, uf, ambiente, cnpj, serie, inicio, fim, justificativa):
        raise NotImplementedError

    def distribuicao(self, uf, ambiente, cnpj, ult_nsu=0, chave=None):
        """Notas e eventos emitidos contra o CNPJ da loja (Ambiente Nacional).
        Devolve {"cstat", "motivo", "docs": [{"nsu", "schema", "xml"}], "ult_nsu", "max_nsu"}."""
        raise NotImplementedError

    def manifestar(self, uf, ambiente, cnpj, chave, operacao=2):
        """Manifestação do destinatário (2 = ciência da operação, que libera o XML completo)."""
        raise NotImplementedError


_trava_pynfe = threading.Lock()  # a PyNFe guarda as entidades numa lista global: uma nota por vez


def _texto(no, caminho):
    achado = no.find(caminho, {"n": NS}) if no is not None else None
    return (achado.text or "").strip() if achado is not None else ""


class ProvedorSefaz(Provedor):
    """Direto na SEFAZ do estado do emitente (webservices NF-e 4.00), assinando com o certificado A1."""
    nome = "sefaz"

    def __init__(self, certificado=None, senha=None):
        cfg = current_app.config
        self.certificado = certificado or cfg.get("NFE_CERTIFICADO", "")
        self.senha = senha if senha is not None else cfg.get("NFE_CERTIFICADO_SENHA", "")

    def configurado(self):
        import os
        if not self.certificado:
            return False, ("Falta o certificado digital A1: no Render, envie o arquivo .pfx em Secret Files e "
                           "preencha NFE_CERTIFICADO (caminho) e NFE_CERTIFICADO_SENHA.")
        if not os.path.exists(self.certificado):
            return False, f"O arquivo do certificado não foi encontrado em {self.certificado}."
        return True, ""

    def validade_certificado(self):
        """(CNPJ/nome do titular, data de vencimento) do certificado, ou (None, erro)."""
        from cryptography.hazmat.primitives.serialization import pkcs12
        try:
            with open(self.certificado, "rb") as f:
                _, cert, _ = pkcs12.load_key_and_certificates(f.read(), (self.senha or "").encode())
        except Exception as e:  # senha errada, arquivo corrompido
            return None, f"Não foi possível abrir o certificado ({e.__class__.__name__}). Confira o arquivo e a senha."
        titular = cert.subject.rfc4514_string()
        venc = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
        return titular, venc

    def _com(self, uf, ambiente):
        from pynfe.processamento.comunicacao import ComunicacaoSefaz
        ok, erro = self.configurado()
        if not ok:
            raise ErroProvedor(erro)
        return ComunicacaoSefaz(uf.lower(), self.certificado, self.senha, ambiente == 2)

    @staticmethod
    def _xml(resposta):
        from lxml import etree
        if resposta.status_code != 200:
            raise ErroProvedor(f"A SEFAZ respondeu com erro HTTP {resposta.status_code}. Tente de novo em alguns "
                               "minutos (consulte antes de reenviar).")
        try:
            return etree.fromstring(resposta.content)
        except Exception:
            raise ErroProvedor("Resposta da SEFAZ em formato inesperado.")

    def status_servico(self, uf, ambiente):
        r = self._xml(self._com(uf, ambiente).status_servico("nfe", timeout=20))
        ret = r.find(".//n:retConsStatServ", {"n": NS})
        cstat = _texto(ret, "n:cStat")
        return Resposta(ok=cstat == "107", status="em_operacao" if cstat == "107" else "fora", cstat=cstat,
                        motivo=_texto(ret, "n:xMotivo"))

    # ----- montagem com a PyNFe -----
    def _montar(self, d):
        from pynfe.entidades.cliente import Cliente
        from pynfe.entidades.emitente import Emitente as EmitenteP
        from pynfe.entidades.fonte_dados import _fonte_dados
        from pynfe.entidades.notafiscal import NotaFiscal
        from pynfe.processamento.serializacao import SerializacaoXML
        from pynfe.utils.flags import CODIGO_BRASIL
        e, de = d.emitente, d.destinatario
        _fonte_dados.limpar_dados()
        emit = EmitenteP(
            razao_social=e.razao_social, nome_fantasia=e.nome_fantasia or e.razao_social, cnpj=e.cnpj,
            codigo_de_regime_tributario=e.crt, inscricao_estadual=e.ie, endereco_logradouro=e.endereco.logradouro,
            endereco_numero=e.endereco.numero, endereco_complemento=e.endereco.complemento,
            endereco_bairro=e.endereco.bairro, endereco_municipio=e.endereco.municipio,
            endereco_cod_municipio=e.endereco.cod_municipio, endereco_uf=e.endereco.uf, endereco_cep=e.endereco.cep,
            endereco_pais=CODIGO_BRASIL, endereco_telefone=e.endereco.telefone)
        cliente = Cliente(
            razao_social=TEXTO_HOMOLOGACAO if d.ambiente == 2 else de.nome,
            tipo_documento="CNPJ" if len(de.documento) == 14 else "CPF", numero_documento=de.documento,
            indicador_ie=de.indicador_ie, inscricao_estadual=de.ie if de.indicador_ie == 1 else "", email=de.email,
            endereco_logradouro=de.endereco.logradouro, endereco_numero=de.endereco.numero,
            endereco_complemento=de.endereco.complemento, endereco_bairro=de.endereco.bairro,
            endereco_municipio=de.endereco.municipio, endereco_cod_municipio=de.endereco.cod_municipio,
            endereco_uf=de.endereco.uf, endereco_cep=de.endereco.cep, endereco_pais=CODIGO_BRASIL,
            endereco_telefone=de.endereco.telefone)
        nota = NotaFiscal(
            emitente=emit, cliente=cliente, uf=e.endereco.uf.upper(), natureza_operacao=d.natureza, modelo=55,
            serie=str(d.serie), numero_nf=str(d.numero), data_emissao=d.emissao, data_saida_entrada=d.emissao,
            tipo_documento=1, municipio=e.endereco.cod_municipio, tipo_impressao_danfe=1, forma_emissao="1",
            cliente_final=d.consumidor_final, indicador_destino=d.id_destino, indicador_presencial=2,
            indicador_intermediador=0, finalidade_emissao="1", processo_emissao="0",
            transporte_modalidade_frete=d.mod_frete,
            informacoes_complementares_interesse_contribuinte=d.info_complementar)
        for i in d.itens:
            nota.adicionar_produto_servico(
                codigo=i.codigo, descricao=i.descricao, ncm=i.ncm, cest=i.cest, cfop=i.cfop,
                unidade_comercial=i.unidade, unidade_tributavel=i.unidade, ean=i.ean or "SEM GTIN",
                ean_tributavel=i.ean or "SEM GTIN", quantidade_comercial=i.quantidade,
                quantidade_tributavel=i.quantidade, valor_unitario_comercial=i.valor_unitario,
                valor_unitario_tributavel=i.valor_unitario, valor_total_bruto=i.valor_total, desconto=i.desconto,
                total_frete=i.frete, ind_total=1, icms_modalidade=i.icms_situacao, icms_origem=i.origem,
                icms_csosn=i.icms_situacao if len(i.icms_situacao) == 3 else "",
                icms_modalidade_determinacao_bc=3, icms_valor_base_calculo=i.icms_base,
                icms_aliquota=i.credito_sn_aliquota if i.icms_situacao == "101" else i.icms_aliquota,
                icms_valor=i.icms_valor, icms_credito=i.credito_sn_valor,
                ipi_codigo_enquadramento=i.ipi_cst, ipi_classe_enquadramento=i.ipi_enquadramento,
                ipi_valor_base_calculo=i.ipi_base, ipi_aliquota=i.ipi_aliquota, ipi_valor_ipi=i.ipi_valor,
                pis_modalidade=i.pis_cst, pis_valor_base_calculo=i.pis_base, pis_aliquota_percentual=i.pis_aliquota,
                pis_valor=i.pis_valor, cofins_modalidade=i.cofins_cst, cofins_valor_base_calculo=i.cofins_base,
                cofins_aliquota_percentual=i.cofins_aliquota, cofins_valor=i.cofins_valor, valor_tributos_aprox="")
        if d.volumes:
            nota.adicionar_transporte_volume(quantidade=Decimal(d.volumes), especie="VOLUME",
                                             peso_liquido=d.peso, peso_bruto=d.peso)
        if d.duplicatas:
            nota.fatura_numero = str(d.numero)
            nota.fatura_valor_original = nota.fatura_valor_liquido = sum((x.valor for x in d.duplicatas), Decimal(0))
            nota.fatura_valor_desconto = Decimal(0)
            for x in d.duplicatas:
                nota.adicionar_duplicata(numero=x.numero, data_vencimento=x.vencimento, valor=x.valor)
        for pg in d.pagamentos:
            nota.adicionar_pagamento(t_pag=pg.tipo, v_pag=pg.valor, ind_pag=1 if pg.a_prazo else 0,
                                     x_pag=pg.descricao if pg.tipo == "99" else "")
        if d.resp_tecnico.get("cnpj"):
            nota.adicionar_responsavel_tecnico(**{k: d.resp_tecnico.get(k, "") for k in ("cnpj", "contato", "email",
                                                                                         "fone")})
        return SerializacaoXML(_fonte_dados, homologacao=d.ambiente == 2).exportar()

    def assinar(self, xml):
        from pynfe.processamento.assinatura import AssinaturaA1
        return AssinaturaA1(self.certificado, self.senha).assinar(xml)

    def gerar_xml(self, d):
        """XML assinado da nota (sem enviar)."""
        with _trava_pynfe:
            from pynfe.entidades.fonte_dados import _fonte_dados
            try:
                xml = self._montar(d)
            finally:
                _fonte_dados.limpar_dados()
        return self.assinar(xml)

    def emitir(self, d):
        from lxml import etree
        assinado = self.gerar_xml(d)
        chave = assinado.find(".//n:infNFe", {"n": NS}).attrib["Id"][3:]
        enviado = etree.tostring(assinado, encoding="unicode")
        try:
            retorno = self._com(d.emitente.endereco.uf, d.ambiente).autorizacao(modelo="nfe", nota_fiscal=assinado,
                                                                               timeout=40)
        except ErroProvedor:
            raise
        except Exception as e:  # sem resposta: a nota pode ter chegado. Guardamos a chave para consultar antes.
            return Resposta(ok=False, status="erro", chave=chave, xml=enviado,
                            motivo=f"Sem resposta da SEFAZ ({e.__class__.__name__}). Consulte a nota antes de reenviar.")
        if retorno[0] == 0:  # autorizada: a PyNFe devolve o nfeProc (nota + protocolo)
            proc = retorno[1]
            inf = proc.find(".//n:protNFe/n:infProt", {"n": NS})
            return Resposta(ok=True, status="autorizada", cstat=_texto(inf, "n:cStat"),
                            motivo=_texto(inf, "n:xMotivo"), chave=chave, protocolo=_texto(inf, "n:nProt"),
                            xml=etree.tostring(proc, encoding="unicode"))
        try:
            r = self._xml(retorno[1])
        except ErroProvedor as e:
            return Resposta(ok=False, status="erro", chave=chave, xml=enviado, motivo=str(e))
        inf = r.find(".//n:protNFe/n:infProt", {"n": NS})
        lote = r.find(".//n:retEnviNFe", {"n": NS})
        cstat = _texto(inf, "n:cStat") or _texto(lote, "n:cStat")
        motivo = _texto(inf, "n:xMotivo") or _texto(lote, "n:xMotivo") or "Sem resposta da SEFAZ."
        return Resposta(ok=False, status="denegada" if cstat in DENEGADA else "rejeitada", cstat=cstat,
                        motivo=motivo, chave=chave, xml=enviado)

    def consultar(self, uf, ambiente, chave, xml_enviado=""):
        """Situação da nota na SEFAZ. Se estiver autorizada e houver o XML enviado, devolve o nfeProc montado."""
        from lxml import etree
        r = self._xml(self._com(uf, ambiente).consulta_nota("nfe", chave))
        ret = r.find(".//n:retConsSitNFe", {"n": NS})
        cstat, motivo = _texto(ret, "n:cStat"), _texto(ret, "n:xMotivo")
        prot = ret.find("n:protNFe", {"n": NS}) if ret is not None else None
        protocolo = _texto(prot, "n:infProt/n:nProt") if prot is not None else ""
        if cstat in ("100", "150") and prot is not None:
            xml = ""
            if xml_enviado:
                proc = etree.Element(f"{{{NS}}}nfeProc", versao="4.00", nsmap={None: NS})
                proc.append(etree.fromstring(xml_enviado.encode()))
                proc.append(prot)
                xml = etree.tostring(proc, encoding="unicode")
            return Resposta(ok=True, status="autorizada", cstat=cstat, motivo=motivo, chave=chave,
                            protocolo=protocolo, xml=xml)
        if cstat in ("101", "151", "135", "155"):
            return Resposta(ok=True, status="cancelada", cstat=cstat, motivo=motivo, chave=chave, protocolo=protocolo)
        return Resposta(ok=False, status="rejeitada" if cstat else "erro", cstat=cstat, motivo=motivo, chave=chave)

    def _evento(self, uf, ambiente, evento):
        from lxml import etree
        from pynfe.entidades.fonte_dados import _fonte_dados
        from pynfe.processamento.serializacao import SerializacaoXML
        with _trava_pynfe:
            try:
                xml = SerializacaoXML(_fonte_dados, homologacao=ambiente == 2).serializar_evento(evento)
            finally:
                _fonte_dados.limpar_dados()
        assinado = self.assinar(xml)
        r = self._xml(self._com(uf, ambiente).evento(modelo="nfe", evento=assinado))
        ret = r.find(".//n:retEnvEvento/n:retEvento", {"n": NS})
        inf = ret.find("n:infEvento", {"n": NS}) if ret is not None else None
        cstat = _texto(inf, "n:cStat") or _texto(r.find(".//n:retEnvEvento", {"n": NS}), "n:cStat")
        motivo = _texto(inf, "n:xMotivo") or _texto(r.find(".//n:retEnvEvento", {"n": NS}), "n:xMotivo")
        ok = cstat in ("135", "136", "155")
        xml = ""
        if ok:
            proc = etree.Element(f"{{{NS}}}procEventoNFe", versao="1.00", nsmap={None: NS})
            proc.append(assinado)
            proc.append(ret)
            xml = etree.tostring(proc, encoding="unicode")
        return Resposta(ok=ok, status="registrado" if ok else "rejeitada", cstat=cstat, motivo=motivo,
                        protocolo=_texto(inf, "n:nProt"), xml=xml)

    def cancelar(self, uf, ambiente, cnpj, chave, protocolo, justificativa):
        from pynfe.entidades.evento import EventoCancelarNota
        evento = EventoCancelarNota(cnpj=cnpj, chave=chave, data_emissao=_agora_servidor(), uf=uf,
                                    protocolo=protocolo, justificativa=justificativa, n_seq_evento=1)
        return self._evento(uf, ambiente, evento)

    def carta_correcao(self, uf, ambiente, cnpj, chave, texto, sequencia):
        from pynfe.entidades.evento import EventoCartaCorrecao
        evento = EventoCartaCorrecao(cnpj=cnpj, chave=chave, data_emissao=_agora_servidor(), uf=uf, correcao=texto,
                                     n_seq_evento=sequencia)
        return self._evento(uf, ambiente, evento)

    def inutilizar(self, uf, ambiente, cnpj, serie, inicio, fim, justificativa):
        from lxml import etree
        r = self._xml(self._com(uf, ambiente).inutilizacao(modelo="nfe", cnpj=cnpj, numero_inicial=inicio,
                                                          numero_final=fim, justificativa=justificativa,
                                                          serie=str(serie)))
        ret = r.find(".//n:retInutNFe", {"n": NS})
        inf = ret.find("n:infInut", {"n": NS}) if ret is not None else None
        cstat = _texto(inf, "n:cStat")
        ok = cstat == "102"
        return Resposta(ok=ok, status="homologada" if ok else "rejeitada", cstat=cstat, motivo=_texto(inf, "n:xMotivo"),
                        protocolo=_texto(inf, "n:nProt"), xml=etree.tostring(ret, encoding="unicode") if ok else "")

    def distribuicao(self, uf, ambiente, cnpj, ult_nsu=0, chave=None):
        from lxml import etree
        from pynfe.utils.descompactar import DescompactaGzip
        r = self._xml(self._com(uf, ambiente).consulta_distribuicao(cnpj=cnpj, chave=chave, nsu=int(ult_nsu or 0)))
        ret = r.find(".//n:retDistDFeInt", {"n": NS})
        docs = []
        for doc in (ret.findall(".//n:loteDistDFeInt/n:docZip", {"n": NS}) if ret is not None else []):
            try:
                xml = etree.tostring(DescompactaGzip.descompacta(doc.text), encoding="unicode")
            except Exception:
                continue
            docs.append({"nsu": doc.get("NSU", ""), "schema": doc.get("schema", ""), "xml": xml})
        return {"cstat": _texto(ret, "n:cStat"), "motivo": _texto(ret, "n:xMotivo"), "docs": docs,
                "ult_nsu": _texto(ret, "n:ultNSU") or str(ult_nsu), "max_nsu": _texto(ret, "n:maxNSU") or str(ult_nsu)}


    def manifestar(self, uf, ambiente, cnpj, chave, operacao=2):
        from pynfe.entidades.evento import EventoManifestacaoDest
        evento = EventoManifestacaoDest(cnpj=cnpj, chave=chave, data_emissao=_agora_servidor(), uf="AN",
                                        operacao=operacao, n_seq_evento=1)
        return self._evento(uf, ambiente, evento)


class ProvedorApi(Provedor):
    """Lugar pronto para uma API de emissão de NF-e (a escolher). Hoje só monta os dados; o envio fica por fazer.

    Para ligar: NFE_PROVEDOR=api, NFE_API_URL e NFE_API_TOKEN no Render, e escrever `_chamar` (e, se o provedor
    pedir outro formato, ajustar `payload`). Ele precisa devolver as mesmas Respostas do ProvedorSefaz."""
    nome = "api"

    def __init__(self):
        cfg = current_app.config
        self.url, self.token = cfg.get("NFE_API_URL", ""), cfg.get("NFE_API_TOKEN", "")

    def configurado(self):
        if not (self.url and self.token):
            return False, "Provedor de API escolhido (NFE_PROVEDOR=api), mas faltam NFE_API_URL e NFE_API_TOKEN."
        return False, "O envio pela API ainda não foi programado (veja ProvedorApi em nfe_provedores.py)."

    @staticmethod
    def payload(d):
        """Os dados da nota como um dicionário simples (datas em texto), base para o JSON de qualquer API."""
        def limpar(v):
            if isinstance(v, dict):
                return {k: limpar(x) for k, x in v.items()}
            if isinstance(v, list):
                return [limpar(x) for x in v]
            if isinstance(v, Decimal):
                return str(v)
            if isinstance(v, (date, datetime)):
                return v.isoformat()
            return v
        return limpar(asdict(d))

    def _chamar(self, metodo, caminho, dados=None):
        raise ErroProvedor(self.configurado()[1])

    def status_servico(self, uf, ambiente):
        return self._chamar("GET", "/status")

    def emitir(self, d):
        return self._chamar("POST", "/nfe", self.payload(d))

    def consultar(self, uf, ambiente, chave, xml_enviado=""):
        return self._chamar("GET", f"/nfe/{chave}")

    def cancelar(self, uf, ambiente, cnpj, chave, protocolo, justificativa):
        return self._chamar("POST", f"/nfe/{chave}/cancelamento", {"justificativa": justificativa})

    def carta_correcao(self, uf, ambiente, cnpj, chave, texto, sequencia):
        return self._chamar("POST", f"/nfe/{chave}/carta-correcao", {"correcao": texto})

    def inutilizar(self, uf, ambiente, cnpj, serie, inicio, fim, justificativa):
        return self._chamar("POST", "/nfe/inutilizacao", {"serie": serie, "inicio": inicio, "fim": fim,
                                                          "justificativa": justificativa})

    def distribuicao(self, uf, ambiente, cnpj, ult_nsu=0, chave=None):
        return self._chamar("GET", "/nfe/recebidas", {"ult_nsu": ult_nsu, "chave": chave})

    def manifestar(self, uf, ambiente, cnpj, chave, operacao=2):
        return self._chamar("POST", f"/nfe/recebidas/{chave}/manifestacao", {"operacao": operacao})


PROVEDORES = {"sefaz": ProvedorSefaz, "api": ProvedorApi}


def provedor():
    return PROVEDORES.get(current_app.config.get("NFE_PROVEDOR", "sefaz"), ProvedorSefaz)()


def agora_brasilia():
    from datetime import timedelta, timezone
    return datetime.now(timezone(timedelta(hours=-3))).replace(microsecond=0)


def _agora_servidor():
    """Hora local do servidor, sem fuso: a PyNFe escreve o evento com o fuso do servidor (no Render, UTC)."""
    return datetime.now().replace(microsecond=0)
