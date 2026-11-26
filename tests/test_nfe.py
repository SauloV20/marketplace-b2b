"""NF-e direto na SEFAZ: configuração, montagem, assinatura, autorização, rejeição, eventos e o lugar da API.

A SEFAZ é simulada (respostas no formato real dos webservices 4.00); o certificado A1 é gerado para o teste."""
import datetime
import io
import os
import re
import tempfile
import unittest
import zipfile
from decimal import Decimal
from unittest import mock

from test_b2b import BaseB2B

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, pkcs12  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from lxml import etree  # noqa: E402

from app import app  # noqa: E402
import nfe_provedores  # noqa: E402
from models import NotaEvento, NotaFiscal, Order, PedidoArquivo, PerfilFiscal, Product, User, db  # noqa: E402

NS = "http://www.portalfiscal.inf.br/nfe"
EMPRESA = {"acao": "empresa", "razao_social": "Marketplace B2B Ferramentas Ltda", "cnpj": "11.222.333/0001-81",
           "ie": "123456789", "crt": "1", "cep": "01001-000", "logradouro": "Praça da Sé", "numero": "100",
           "bairro": "Sé", "municipio": "São Paulo", "cod_municipio": "3550308", "uf": "SP", "telefone": "1133334444",
           "ambiente": "2", "serie": "1", "proximo_1": "1", "proximo_2": "1", "natureza": "Venda de mercadoria",
           "rt_email": "ti@loja-exemplo.com.br"}
DESTINO = {"indicador_ie": "9", "cep": "01001000", "logradouro": "Rua Direita", "numero": "50", "bairro": "Sé",
           "municipio": "São Paulo", "cod_municipio": "3550308", "uf": "SP", "mod_frete": "9", "volumes": "1",
           "consumidor_final": "on"}


def _soap(corpo):
    return ('<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"><soap:Body>'
            f'<nfeResultMsg xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/X">{corpo}</nfeResultMsg>'
            '</soap:Body></soap:Envelope>')


class Resp:
    def __init__(self, texto, status=200):
        self.status_code, self.text, self.content, self.encoding = status, texto, texto.encode(), "utf-8"


class SefazFalsa:
    """Responde como a SEFAZ conforme o serviço chamado. `autorizacao` diz como responder a próxima nota."""

    def __init__(self):
        self.autorizacao = ["100"]
        self.chamadas = []
        self.consulta = "100"
        self.ultima_chave = ""

    def __call__(self, com, url, xml, timeout=None):
        texto = etree.tostring(xml, encoding="unicode")
        self.chamadas.append(texto)
        if "<enviNFe" in texto:
            chave = re.search(r'Id="NFe(\d{44})"', texto).group(1)
            self.ultima_chave = chave
            situacao = self.autorizacao.pop(0) if self.autorizacao else "100"
            if situacao == "falha":
                import requests
                raise requests.exceptions.ConnectionError("tempo esgotado")
            motivo = "Autorizado o uso da NF-e" if situacao == "100" else "Rejeicao: CEP do destinatario invalido"
            return Resp(_soap(f'<retEnviNFe xmlns="{NS}" versao="4.00"><tpAmb>2</tpAmb><verAplic>T</verAplic>'
                              '<cStat>104</cStat><xMotivo>Lote processado</xMotivo><cUF>35</cUF>'
                              f'<protNFe versao="4.00"><infProt><tpAmb>2</tpAmb><verAplic>T</verAplic><chNFe>{chave}</chNFe>'
                              '<dhRecbto>2026-11-13T10:00:00-03:00</dhRecbto><nProt>135260000000123</nProt>'
                              f'<digVal>abc=</digVal><cStat>{situacao}</cStat><xMotivo>{motivo}</xMotivo></infProt>'
                              '</protNFe></retEnviNFe>'))
        if "<consSitNFe" in texto:
            chave = re.search(r"<chNFe>(\d{44})</chNFe>", texto).group(1)
            prot = (f'<protNFe versao="4.00"><infProt><tpAmb>2</tpAmb><chNFe>{chave}</chNFe><nProt>135260000000777</nProt>'
                    '<cStat>100</cStat><xMotivo>Autorizado o uso da NF-e</xMotivo></infProt></protNFe>'
                    if self.consulta == "100" else "")
            motivo = "Autorizado o uso da NF-e" if self.consulta == "100" else "Rejeicao: NF-e nao consta na base"
            return Resp(_soap(f'<retConsSitNFe xmlns="{NS}" versao="4.00"><tpAmb>2</tpAmb><cStat>{self.consulta}</cStat>'
                              f'<xMotivo>{motivo}</xMotivo><cUF>35</cUF><chNFe>{chave}</chNFe>{prot}</retConsSitNFe>'))
        if "<envEvento" in texto:
            seq = re.search(r"<nSeqEvento>(\d+)</nSeqEvento>", texto).group(1)
            return Resp(_soap(f'<retEnvEvento xmlns="{NS}" versao="1.00"><idLote>1</idLote><tpAmb>2</tpAmb>'
                              '<cStat>128</cStat><xMotivo>Lote de Evento Processado</xMotivo><retEvento versao="1.00">'
                              '<infEvento><tpAmb>2</tpAmb><cOrgao>35</cOrgao><cStat>135</cStat>'
                              f'<xMotivo>Evento registrado e vinculado a NF-e</xMotivo><nSeqEvento>{seq}</nSeqEvento>'
                              f'<nProt>13526000000099{seq}</nProt></infEvento></retEvento></retEnvEvento>'))
        if "<inutNFe" in texto:
            return Resp(_soap(f'<retInutNFe xmlns="{NS}" versao="4.00"><infInut><tpAmb>2</tpAmb><cStat>102</cStat>'
                              '<xMotivo>Inutilizacao de numero homologado</xMotivo><nProt>135260000000555</nProt>'
                              '</infInut></retInutNFe>'))
        if "<consStatServ" in texto:
            return Resp(_soap(f'<retConsStatServ xmlns="{NS}" versao="4.00"><tpAmb>2</tpAmb><cStat>107</cStat>'
                              '<xMotivo>Servico em Operacao</xMotivo></retConsStatServ>'))
        return Resp("erro", 500)


class BaseNFe(BaseB2B):
    @classmethod
    def setUpClass(cls):
        chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Marketplace B2B FERRAMENTAS LTDA:11222333000181")])
        agora = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(nome).issuer_name(nome).public_key(chave.public_key())
                .serial_number(7).not_valid_before(agora - datetime.timedelta(days=1))
                .not_valid_after(agora + datetime.timedelta(days=200)).sign(chave, hashes.SHA256()))
        cls.pfx = os.path.join(tempfile.mkdtemp(), "certificado.pfx")
        with open(cls.pfx, "wb") as f:
            f.write(pkcs12.serialize_key_and_certificates(b"t", chave, cert, None, BestAvailableEncryption(b"senha-teste")))

    def setUp(self):
        super().setUp()
        app.config.update(NFE_PROVEDOR="sefaz", NFE_CERTIFICADO=self.pfx, NFE_CERTIFICADO_SENHA="senha-teste")
        self.sefaz = SefazFalsa()
        trava = mock.patch("pynfe.processamento.comunicacao.ComunicacaoSefaz._post", autospec=True,
                           side_effect=self.sefaz)
        trava.start()
        self.addCleanup(trava.stop)
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50, ncm="82077010")
        self.a = self.admin()

    def tearDown(self):
        app.config.update(NFE_CERTIFICADO="", NFE_CERTIFICADO_SENHA="", NFE_PROVEDOR="sefaz")
        super().tearDown()

    def configurar(self, **extra):
        r = self.post(self.a, "/admin/notas/configuracao", {**EMPRESA, **extra}, "/admin/notas/configuracao")
        self.post(self.a, "/admin/notas/configuracao", {"acao": "perfil_padrao"}, "/admin/notas/configuracao")
        return r

    def pedido(self, pagamento="pix", qtd=2, c=None, **extra):
        c = c or self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, qtd)
        self.fechar(c, pagamento, **extra)
        return self.ultimo_pedido()[0], c

    def emitir(self, oid, **extra):
        with app.app_context():
            o = db.session.get(Order, oid)
            dados = {"nome": o.customer_name, "documento": o.documento, **DESTINO, **extra}
        return self.post(self.a, f"/admin/orders/{oid}/nfe", dados, f"/admin/orders/{oid}/nfe")

    def nota(self, oid=None):
        with app.app_context():
            q = NotaFiscal.query.filter_by(order_id=oid) if oid else NotaFiscal.query
            n = q.order_by(NotaFiscal.id.desc()).first()
            return n and {"id": n.id, "numero": n.numero, "status": n.status, "chave": n.chave, "cstat": n.cstat,
                          "xml": n.xml, "protocolo": n.protocolo, "ambiente": n.ambiente}


class TesteConfiguracao(BaseNFe):
    def test_configuracao_valida_e_regra_padrao(self):
        r = self.post(self.a, "/admin/notas/configuracao", {**EMPRESA, "cnpj": "11.111.111/1111-11"},
                      "/admin/notas/configuracao")
        self.assertIn("CNPJ da empresa inválido", r.get_data(as_text=True))
        self.configurar()
        with app.app_context():
            perfil = PerfilFiscal.query.one()
            self.assertEqual((perfil.icms_situacao, perfil.cfop_estadual, perfil.padrao), ("102", "5102", True))
        html = self.a.get("/admin/notas/configuracao").get_data(as_text=True)
        self.assertIn("Marketplace B2B FERRAMENTAS LTDA", html, "mostra o titular do certificado")
        self.post(self.a, "/admin/notas/configuracao", {"acao": "status"}, "/admin/notas/configuracao")
        self.assertIn("107", self.a.get("/admin/notas/configuracao").get_data(as_text=True))

    def test_sem_ncm_nao_envia_nada(self):
        self.configurar()
        self.ajustar_produto(1, ncm="")
        oid, _ = self.pedido()
        r = self.emitir(oid)
        self.assertIn("falta o NCM", r.get_data(as_text=True))
        self.assertIsNone(self.nota())
        self.assertEqual(self.sefaz.chamadas, [])

    def test_provedor_api_ainda_nao_configurado(self):
        self.configurar()
        app.config["NFE_PROVEDOR"] = "api"
        oid, _ = self.pedido()
        r = self.emitir(oid)
        self.assertIn("NFE_API_URL", r.get_data(as_text=True))
        self.assertIsNone(self.nota())
        with app.test_request_context():
            import fiscal
            with app.app_context():
                o = db.session.get(Order, oid)
                from werkzeug.datastructures import MultiDict
                dados, _, erros = fiscal.montar(o, MultiDict({"nome": "X", "documento": o.documento, **DESTINO}))
                payload = nfe_provedores.ProvedorApi.payload(dados)
        self.assertEqual(erros, [])
        self.assertEqual(payload["itens"][0]["ncm"], "82077010")
        self.assertIsInstance(payload["itens"][0]["valor_total"], str)


class TesteEmissao(BaseNFe):
    def test_homologacao_autorizada_assinada_e_sem_ir_para_o_cliente(self):
        self.configurar()
        oid, c = self.pedido()
        self.emitir(oid)
        n = self.nota(oid)
        self.assertEqual((n["status"], n["numero"], n["ambiente"]), ("autorizada", 1, 2))
        self.assertEqual(len(n["chave"]), 44)
        self.assertIn("<nfeProc", n["xml"])
        self.assertIn("SEM VALOR FISCAL", n["xml"])
        self.assertIn("<Signature", n["xml"])
        self.assertIn("<CFOP>5102</CFOP>", n["xml"])
        self.assertIn("<tPag>17</tPag>", n["xml"])
        with app.app_context():
            self.assertEqual(PedidoArquivo.query.filter_by(order_id=oid).count(), 0, "homologação não vai ao cliente")
        r = self.a.get(f"/admin/notas/{n['id']}/danfe")
        self.assertEqual(r.headers["Content-Type"], "application/pdf")
        self.assertTrue(r.data.startswith(b"%PDF"))
        self.assertIn("NF-e 1", self.a.get(f"/admin/orders/{oid}").get_data(as_text=True))

    def test_producao_anexa_xml_e_danfe_e_bloqueia_exclusao(self):
        self.configurar(ambiente="1")
        oid, c = self.pedido()
        self.emitir(oid)
        n = self.nota(oid)
        self.assertEqual(n["status"], "autorizada")
        self.assertNotIn("SEM VALOR FISCAL", n["xml"])
        with app.app_context():
            tipos = sorted(a.tipo for a in PedidoArquivo.query.filter_by(order_id=oid).all())
        self.assertEqual(tipos, ["nota", "xml"])
        self.assertIn("DANFE NF-e 1", c.get("/conta/documentos").get_data(as_text=True))
        self.post(self.a, "/admin/orders/excluir", {"oid": str(oid)}, "/admin/orders")
        with app.app_context():
            self.assertIsNotNone(db.session.get(Order, oid), "pedido com nota autorizada não é excluído")
        conteudo = self.a.get("/admin/notas/xmls?de=2000-01-01&ate=2100-01-01").data
        self.assertIn(f"{n['chave']}-nfe.xml", zipfile.ZipFile(io.BytesIO(conteudo)).namelist())

    def test_rejeitada_reenvia_com_o_mesmo_numero(self):
        self.configurar()
        self.sefaz.autorizacao = ["539", "100"]
        oid, c = self.pedido()
        r = self.emitir(oid)
        self.assertIn("CEP do destinatario invalido", self.a.get(r.headers.get("Location") or
                                                                 f"/admin/orders/{oid}/nfe").get_data(as_text=True)
                      if r.status_code == 302 else r.get_data(as_text=True))
        self.assertEqual((self.nota(oid)["status"], self.nota(oid)["numero"]), ("rejeitada", 1))
        self.emitir(oid)
        self.assertEqual((self.nota(oid)["status"], self.nota(oid)["numero"]), ("autorizada", 1))
        oid2, _ = self.pedido(c=c, qtd=1)
        self.emitir(oid2)
        self.assertEqual(self.nota(oid2)["numero"], 2)

    def test_sem_resposta_consulta_antes_de_reenviar(self):
        self.configurar()
        self.sefaz.autorizacao = ["falha"]
        oid, _ = self.pedido()
        self.emitir(oid)
        n = self.nota(oid)
        self.assertEqual(n["status"], "erro")
        self.assertEqual(n["chave"], self.sefaz.ultima_chave)
        enviadas = sum("<enviNFe" in x for x in self.sefaz.chamadas)
        self.emitir(oid)  # a SEFAZ diz que ela já estava autorizada: não manda outra
        n = self.nota(oid)
        self.assertEqual((n["status"], n["protocolo"]), ("autorizada", "135260000000777"))
        self.assertEqual(sum("<enviNFe" in x for x in self.sefaz.chamadas), enviadas)
        self.assertIn("<nfeProc", n["xml"])

    def test_interestadual_e_faturado_com_duplicatas(self):
        self.configurar()
        c = self.empresa()
        oid, _ = self.pedido("faturado", c=c, prazo="30/60/90")
        self.emitir(oid, uf="MG", cep="30140071", municipio="Belo Horizonte", cod_municipio="3106200",
                    indicador_ie="9", consumidor_final="")
        xml = self.nota(oid)["xml"]
        self.assertIn("<CFOP>6102</CFOP>", xml)
        self.assertIn("<idDest>2</idDest>", xml)
        self.assertEqual(xml.count("<dup>"), 3)
        self.assertIn("<tPag>15</tPag>", xml)
        self.assertIn("<indPag>1</indPag>", xml)


class TesteEventos(BaseNFe):
    def autorizada(self):
        self.configurar()
        oid, _ = self.pedido()
        self.emitir(oid)
        return self.nota(oid)

    def test_carta_de_correcao_e_cancelamento(self):
        n = self.autorizada()
        url = f"/admin/notas/{n['id']}"
        self.post(self.a, url, {"acao": "correcao", "texto": "curta"}, url)
        self.post(self.a, url, {"acao": "correcao", "texto": "Endereço de entrega: Rua Direita, 50, fundos"}, url)
        self.post(self.a, url, {"acao": "correcao", "texto": "Transportadora correta: Expresso Teste Ltda"}, url)
        with app.app_context():
            cartas = [(e.sequencia, e.ok) for e in NotaEvento.query.filter_by(nota_id=n["id"], tipo="correcao").all()]
        self.assertEqual(cartas, [(1, True), (2, True)])
        self.post(self.a, url, {"acao": "cancelar", "justificativa": "Pedido cancelado pelo cliente antes do envio"}, url)
        self.assertEqual(self.nota()["status"], "cancelada")
        with app.app_context():
            ev = NotaEvento.query.filter_by(tipo="cancelamento").one()
            self.assertIn("procEventoNFe", ev.xml)

    def test_inutilizacao(self):
        self.configurar()
        self.post(self.a, "/admin/notas/inutilizar", {"inicio": "5", "fim": "6",
                                                      "justificativa": "Numeros pulados por erro de digitacao"},
                  "/admin/notas")
        with app.app_context():
            ev = NotaEvento.query.filter_by(tipo="inutilizacao").one()
            self.assertTrue(ev.ok)
            from b2b import config
            self.assertEqual(config("nfe_numero_2_1"), "7")


class TestePainelNFe(BaseNFe):
    def test_permissao_e_campos_fiscais_do_produto(self):
        with app.app_context():
            u = User(name="Vend", email="vend@loja-exemplo.com", is_admin=True, email_verified=True, permissoes="pedidos")
            u.set_password("senha-vendedor-123")
            db.session.add(u)
            db.session.commit()
        v = self.cliente()
        self.post(v, "/admin/login", {"email": "vend@loja-exemplo.com", "password": "senha-vendedor-123"}, "/admin/login")
        self.assertEqual(v.get("/admin/notas").status_code, 302)
        oid, _ = self.pedido()
        self.assertNotIn("Emitir NF-e", v.get(f"/admin/orders/{oid}").get_data(as_text=True))
        with app.app_context():
            p = db.session.get(Product, 1)
            form = {"name": p.name, "price": "189,90", "stock": "50", "category_id": str(p.category_id),
                    "qtd_minima": "1", "multiplo": "1", "active": "on", "ncm": "8207.70.10", "cest": "",
                    "origem": "2", "unidade": "pc", "ean": "7891234567895", "perfil_fiscal_id": ""}
        self.post(self.a, "/admin/products/1/edit", form, "/admin/products/1/edit")
        with app.app_context():
            p = db.session.get(Product, 1)
            self.assertEqual((p.ncm, p.origem, p.unidade, p.ean), ("82077010", 2, "PC", "7891234567895"))
        for url in ("/admin/notas", "/admin/notas/configuracao", f"/admin/orders/{oid}/nfe"):
            self.assertEqual(self.a.get(url).status_code, 200, url)

    def test_planilha_com_ncm(self):
        import planilha
        nome = "Fresa de Topo Metal Duro 10 mm 4 Cortes"
        with app.app_context():
            _, erros = planilha.validar([(2, {"nome": nome, "ncm": 1012100.0, "cest": "1234"})])
        self.assertTrue(any("CEST tem 7" in e[1] for e in erros))
        with app.app_context():
            alteracoes, erros = planilha.validar([(2, {"nome": nome, "ncm": 1012100.0})])
            self.assertEqual(erros, [])
            self.assertEqual(alteracoes[0]["dados"]["ncm"], "01012100")


if __name__ == "__main__":
    unittest.main()
