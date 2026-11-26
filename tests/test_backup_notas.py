"""Backup semanal por e-mail (sem fotos, com senha), NF-e enviada ao cliente e notas de compra (DF-e e XML)."""
import base64
import gzip
import io
import re
import unittest
from datetime import timedelta
from decimal import Decimal

from test_nfe import NS, BaseNFe, Resp, SefazFalsa, _soap

from app import app  # noqa: E402
import backup  # noqa: E402
import backup_auto  # noqa: E402
from b2b import config, salvar_config  # noqa: E402
from models import (Compra, Fornecedor, NotaRecebida, Product, ProdutoFornecedor, ProdutoFoto, agora_utc,  # noqa: E402
                    db)

MEU_CNPJ, CNPJ_FORNECEDOR = "11222333000181", "11444777000161"


def nota_fornecedor(chave="35261111444777000161550010000012341000000017", numero=1234, dest=MEU_CNPJ):
    return (f'<nfeProc xmlns="{NS}" versao="4.00"><NFe><infNFe versao="4.00" Id="NFe{chave}"><ide><cUF>35</cUF>'
            f'<natOp>Venda</natOp><mod>55</mod><serie>1</serie><nNF>{numero}</nNF><dhEmi>2026-11-10T10:00:00-03:00</dhEmi>'
            '</ide><emit><CNPJ>11444777000161</CNPJ><xNome>Abrasivos Brasil Ltda</xNome><enderEmit><xLgr>Rua X</xLgr>'
            '<UF>SP</UF><fone>1140000000</fone></enderEmit><IE>1</IE><CRT>3</CRT></emit>'
            f'<dest><CNPJ>{dest}</CNPJ><xNome>Marketplace B2B</xNome></dest>'
            '<det nItem="1"><prod><cProd>ABR-10</cProd><cEAN>SEM GTIN</cEAN><xProd>Fresa metal duro 10mm cx10</xProd>'
            '<NCM>82077010</NCM><CFOP>6102</CFOP><uCom>CX</uCom><qCom>5.0000</qCom><vUnCom>100.0000</vUnCom>'
            '<vProd>500.00</vProd><vDesc>20.00</vDesc></prod><imposto><ICMS><ICMS00><orig>0</orig><CST>00</CST></ICMS00>'
            '</ICMS><IPI><cEnq>999</cEnq><IPITrib><CST>50</CST><vBC>480.00</vBC><pIPI>5.00</pIPI><vIPI>24.00</vIPI>'
            '</IPITrib></IPI></imposto></det>'
            '<det nItem="2"><prod><cProd>OUTRO</cProd><cEAN>SEM GTIN</cEAN><xProd>Brinde</xProd><NCM>49111090</NCM>'
            '<uCom>UN</uCom><qCom>1.0000</qCom><vUnCom>0.0100</vUnCom><vProd>0.01</vProd></prod></det>'
            '<total><ICMSTot><vProd>500.01</vProd><vFrete>30.00</vFrete><vNF>534.01</vNF></ICMSTot></total>'
            '<cobr><dup><nDup>001</nDup><dVenc>2026-12-10</dVenc><vDup>267.00</vDup></dup>'
            '<dup><nDup>002</nDup><dVenc>2027-01-09</dVenc><vDup>267.01</vDup></dup></cobr>'
            '</infNFe></NFe></nfeProc>')


def resumo(chave):
    return (f'<resNFe xmlns="{NS}" versao="1.01"><chNFe>{chave}</chNFe><CNPJ>{CNPJ_FORNECEDOR}</CNPJ>'
            '<xNome>Abrasivos Brasil Ltda</xNome><IE>1</IE><dhEmi>2026-11-10T10:00:00-03:00</dhEmi><tpNF>1</tpNF>'
            '<vNF>534.01</vNF><digVal>x</digVal><dhRecbto>2026-11-10T10:00:01-03:00</dhRecbto><nProt>1</nProt>'
            '<cSitNFe>1</cSitNFe></resNFe>')


def doczip(nsu, schema, xml):
    return f'<docZip NSU="{nsu:015d}" schema="{schema}">{base64.b64encode(gzip.compress(xml.encode())).decode()}</docZip>'


class SefazComDistribuicao(SefazFalsa):
    def __init__(self):
        super().__init__()
        self.lote = []  # documentos da próxima busca por NSU
        self.completas = {}  # chave -> XML liberado depois da ciência
        self.ciencias = []

    def __call__(self, com, url, xml, timeout=None):
        from lxml import etree
        texto = etree.tostring(xml, encoding="unicode")
        if "<distDFeInt" in texto:
            self.chamadas.append(texto)
            chave = re.search(r"<chNFe>(\d{44})</chNFe>", texto)
            if chave:
                docs = [doczip(90, "procNFe_v4.00.xsd", self.completas[chave.group(1)])] \
                    if chave.group(1) in self.completas else []
            else:
                docs, self.lote = self.lote, []
            cstat = "138" if docs else "137"
            return Resp(_soap(f'<retDistDFeInt xmlns="{NS}" versao="1.01"><tpAmb>1</tpAmb><cStat>{cstat}</cStat>'
                              f'<xMotivo>ok</xMotivo><ultNSU>{len(docs):015d}</ultNSU><maxNSU>{len(docs):015d}</maxNSU>'
                              f'<loteDistDFeInt>{"".join(docs)}</loteDistDFeInt></retDistDFeInt>'))
        if "<envEvento" in texto and "210210" in texto:
            self.ciencias.append(texto)
        return super().__call__(com, url, xml, timeout)


class BaseNotas(BaseNFe):
    def setUp(self):
        super().setUp()
        self.sefaz.__class__ = SefazComDistribuicao
        SefazComDistribuicao.__init__(self.sefaz)


class TesteEmailDaNota(BaseNotas):
    def test_cliente_recebe_danfe_e_xml_em_producao(self):
        self.configurar(ambiente="1", enviar_email="on")
        oid, _ = self.pedido()
        self.emails.clear()
        self.emitir(oid)
        enviados = [e for e in self.emails if "nota fiscal do pedido" in e["assunto"]]
        self.assertEqual(len(enviados), 1)
        nomes = [a[0] for a in enviados[0]["anexos"]]
        self.assertEqual(nomes, ["DANFE NF-e 1.pdf", "NF-e 1.xml"])
        self.assertTrue(enviados[0]["anexos"][0][1].startswith(b"%PDF"))
        n = self.nota(oid)
        self.post(self.a, f"/admin/notas/{n['id']}", {"acao": "email"}, f"/admin/notas/{n['id']}")
        self.assertEqual(sum("nota fiscal do pedido" in e["assunto"] for e in self.emails), 2)

    def test_desligado_e_homologacao_nao_enviam(self):
        self.configurar(ambiente="1")  # sem a caixa marcada
        oid, _ = self.pedido()
        self.emails.clear()
        self.emitir(oid)
        self.assertFalse(any("nota fiscal do pedido" in e["assunto"] for e in self.emails))


class TesteBackupAutomatico(BaseNotas):
    def tearDown(self):
        app.config.update(BACKUP_SENHA="", TAREFAS_TOKEN="", BACKUP_AUTO_NOS_TESTES=False)
        super().tearDown()

    def test_backup_sem_fotos_e_com_senha(self):
        with app.app_context():
            db.session.add(ProdutoFoto(product_id=1, dados=b"x" * 5000, tipo="image/png"))
            db.session.commit()
            completo, _ = backup.gerar()
            leve, resumo_ = backup.gerar(sem_arquivos=True)
            self.assertNotIn("produto_foto", resumo_)
            self.assertLess(len(leve), len(completo))
            cifrado = backup.cifrar(leve, "senha forte 123")
            self.assertTrue(cifrado.startswith(backup.CIFRADO))
            self.assertNotIn(b"Fresa", cifrado)
            self.assertTrue(backup.ler(cifrado, "senha forte 123")["parcial"])
            with self.assertRaises(backup.ErroBackup):
                backup.ler(cifrado, "senha errada")
            with self.assertRaises(backup.ErroBackup):
                backup.ler(cifrado, "")

    def test_tarefa_do_github_com_token(self):
        c = self.cliente()
        self.assertEqual(c.post("/tarefas/backup-semanal").status_code, 404, "sem token configurado, não existe")
        app.config.update(TAREFAS_TOKEN="token-secreto-123", BACKUP_SENHA="senha forte 123")
        self.assertEqual(c.post("/tarefas/backup-semanal", headers={"Authorization": "Bearer errado"}).status_code, 403)
        self.emails.clear()
        r = c.post("/tarefas/backup-semanal", headers={"Authorization": "Bearer token-secreto-123"})
        self.assertTrue(r.get_json()["ok"], r.get_json())
        self.assertEqual(len(self.emails), 1)
        self.assertEqual(self.emails[0]["para"], "admin@exemplo.com", "sem e-mail configurado: o dono da loja")
        nome, arquivo, tipo = self.emails[0]["anexos"][0]
        self.assertTrue(nome.endswith(".zip"), "o Brevo recusa anexo .cifrado")
        self.assertEqual(tipo, "application/zip")
        import zipfile
        with zipfile.ZipFile(io.BytesIO(arquivo)) as z:
            dentro = z.namelist()
            self.assertEqual(len(dentro), 1)
            self.assertTrue(dentro[0].endswith(".json.gz.cifrado"))
            self.assertTrue(z.read(dentro[0]).startswith(backup.CIFRADO), "dentro do zip continua cifrado")
        with app.app_context():
            with self.assertRaises(backup.ErroBackup):
                backup.ler(arquivo, "")  # o zip não dispensa a senha
            conteudo = backup.ler(arquivo, "senha forte 123")
            self.assertTrue(conteudo["parcial"])
            self.assertIn("nota_fiscal", conteudo["tabelas"])
        r = c.post("/tarefas/backup-semanal", headers={"Authorization": "Bearer token-secreto-123"})
        self.assertFalse(r.get_json()["ok"], "menos de 7 dias: não manda de novo")
        self.assertEqual(len(self.emails), 1)

    def test_sem_senha_nao_envia_e_o_painel_envia_quando_atrasado(self):
        app.config.update(BACKUP_AUTO_NOS_TESTES=True)
        self.emails.clear()
        self.a.get("/admin/backup")
        self.assertEqual(self.emails, [], "sem BACKUP_SENHA, nada sai")
        self.assertIn("BACKUP_SENHA", self.a.get("/admin/backup").get_data(as_text=True))
        app.config.update(BACKUP_SENHA="senha forte 123")
        self.post(self.a, "/admin/backup/automatico", {"backup_email": "dono@loja-exemplo.com.br"}, "/admin/backup")
        self.assertEqual(self.emails, [], "a falha de agora há pouco espera 6 horas para tentar de novo")
        with app.app_context():
            salvar_config("backup_auto_tentativa", (agora_utc() - timedelta(hours=7)).isoformat())
            salvar_config("backup_auto_em", (agora_utc() - timedelta(days=8)).isoformat())
            db.session.commit()
        self.a.get("/admin/backup")
        self.assertEqual([e["para"] for e in self.emails if "backup" in e["assunto"]], ["dono@loja-exemplo.com.br"])
        with app.app_context():
            self.assertFalse(backup_auto.devido())

    def test_restaurar_o_zip_do_email_pelo_painel(self):
        with app.app_context():
            leve, _ = backup.gerar(sem_arquivos=True)
            arquivo = backup.em_zip("b.json.gz.cifrado", backup.cifrar(leve, "senha forte 123"))
            Product.query.filter_by(id=1).update({"name": "Nome trocado depois do backup"})
            db.session.commit()
        dados = {"senha": "admin-forte-123", "confirmacao": "RESTAURAR", "senha_backup": "senha forte 123",
                 "arquivo": (io.BytesIO(arquivo), "backup-marketplace-dados.zip")}
        token = self.token(self.a, "/admin/backup")
        r = self.a.post("/admin/backup/restaurar", data={"_csrf": token, **dados}, content_type="multipart/form-data")
        self.assertIn("/login", r.headers["Location"])
        with app.app_context():
            self.assertNotEqual(db.session.get(Product, 1).name, "Nome trocado depois do backup")

    def test_zip_sem_backup_dentro_e_recusado(self):
        with app.app_context():
            with self.assertRaises(backup.ErroBackup):
                backup.ler(backup.em_zip("foto.png", b"nada"))
            with self.assertRaises(backup.ErroBackup):
                backup.ler(b"PK\x03\x04 lixo")

    def test_restaurar_backup_cifrado_pelo_painel(self):
        with app.app_context():
            leve, _ = backup.gerar(sem_arquivos=True)
            arquivo = backup.cifrar(leve, "senha forte 123")
            Product.query.filter_by(id=1).update({"name": "Nome trocado depois do backup"})
            db.session.commit()
        dados = {"senha": "admin-forte-123", "confirmacao": "RESTAURAR", "senha_backup": "senha forte 123",
                 "arquivo": (io.BytesIO(arquivo), "backup.json.gz.cifrado")}
        token = self.token(self.a, "/admin/backup")
        r = self.a.post("/admin/backup/restaurar", data={"_csrf": token, **dados}, content_type="multipart/form-data")
        self.assertIn("/login", r.headers["Location"])
        with app.app_context():
            self.assertNotEqual(db.session.get(Product, 1).name, "Nome trocado depois do backup")


class TesteNotasDeCompra(BaseNotas):
    chave = "35261111444777000161550010000012341000000017"

    def setUp(self):
        super().setUp()
        self.configurar(ambiente="1")

    def test_busca_resumo_ciencia_e_espera_de_uma_hora(self):
        self.sefaz.lote = [doczip(1, "resNFe_v1.01.xsd", resumo(self.chave))]
        self.post(self.a, "/admin/notas-de-compra", {"acao": "buscar"}, "/admin/notas-de-compra")
        with app.app_context():
            nr = NotaRecebida.query.one()
            self.assertEqual((nr.tipo, nr.numero, nr.valor, nr.nome_emitente),
                             ("resumo", 1234, Decimal("534.01"), "Abrasivos Brasil Ltda"))
            rid = nr.id
            self.assertTrue(config("dfe_liberado_em"))
        chamadas = len(self.sefaz.chamadas)
        r = self.post(self.a, "/admin/notas-de-compra", {"acao": "buscar"}, "/admin/notas-de-compra")
        self.assertIn("só permite uma nova busca", self.a.get(r.headers["Location"]).get_data(as_text=True))
        self.assertEqual(len(self.sefaz.chamadas), chamadas, "respeita a hora de espera da SEFAZ")
        self.sefaz.completas[self.chave] = nota_fornecedor(self.chave)
        self.post(self.a, f"/admin/notas-de-compra/{rid}", {"acao": "ciencia"}, f"/admin/notas-de-compra/{rid}")
        self.assertEqual(len(self.sefaz.ciencias), 1)
        with app.app_context():
            nr = db.session.get(NotaRecebida, rid)
            self.assertEqual((nr.tipo, nr.manifestacao), ("completa", "ciencia"))

    def test_xml_importado_vira_compra_com_conversao_de_caixa(self):
        r = self.importar(nota_fornecedor(self.chave))
        rid = int(re.search(r"/notas-de-compra/(\d+)", r.headers["Location"]).group(1))
        html = self.a.get(f"/admin/notas-de-compra/{rid}").get_data(as_text=True)
        self.assertIn("Fresa metal duro 10mm cx10", html)
        r = self.post(self.a, f"/admin/notas-de-compra/{rid}", {"produto_0": "1", "fator_0": "10", "produto_1": ""},
                      f"/admin/notas-de-compra/{rid}")
        cid = int(re.search(r"/compras/(\d+)", r.headers["Location"]).group(1))
        with app.app_context():
            c = db.session.get(Compra, cid)
            self.assertEqual((c.status, c.nota, c.frete, c.condicao), ("enviado", "1234", Decimal("30.00"), "30/60"))
            self.assertEqual([(i.product_id, i.quantidade, i.preco, i.ipi_pct) for i in c.itens],
                             [(1, 50, Decimal("9.6000"), Decimal("5.00"))])  # (500 − 20) ÷ 50 unidades
            forn = Fornecedor.query.filter_by(documento=CNPJ_FORNECEDOR).one()
            link = ProdutoFornecedor.query.filter_by(fornecedor_id=forn.id, product_id=1).one()
            self.assertEqual((link.codigo, link.fator), ("ABR-10", 10))
            iid = c.itens[0].id
            estoque_antes = db.session.get(Product, 1).stock
        self.post(self.a, f"/admin/compras/{cid}", {"acao": "receber", f"recebido_{iid}": "50", f"preco_{iid}": "9,60",
                                                     "frete": "30,00", "condicao": "30/60", "atualizar_precos": "on"},
                  f"/admin/compras/{cid}")
        with app.app_context():
            self.assertEqual(db.session.get(Product, 1).stock, estoque_antes + 50)
            self.assertEqual(db.session.get(NotaRecebida, rid).compra_id, cid)
        # a próxima nota do mesmo fornecedor já vem com o produto e a conversão escolhidos
        outra = "35261111444777000161550010000012351000000012"
        r = self.importar(nota_fornecedor(outra, numero=1235))
        rid2 = int(re.search(r"/notas-de-compra/(\d+)", r.headers["Location"]).group(1))
        html = self.a.get(f"/admin/notas-de-compra/{rid2}").get_data(as_text=True)
        self.assertRegex(html, r'<option value="1" selected>')
        self.assertIn('name="fator_0" type="number" min="1" value="10"', html)

    def test_xml_de_outra_empresa_e_recusado(self):
        r = self.importar(nota_fornecedor(self.chave, dest="99888777000100"))
        self.assertIn("não foi emitida para o CNPJ da loja", self.a.get(r.headers["Location"]).get_data(as_text=True))
        with app.app_context():
            self.assertEqual(NotaRecebida.query.count(), 0)

    def importar(self, xml):
        token = self.token(self.a, "/admin/notas-de-compra")
        return self.a.post("/admin/notas-de-compra", data={"_csrf": token, "arquivo": (io.BytesIO(xml.encode()), "nota.xml")},
                           content_type="multipart/form-data")


if __name__ == "__main__":
    unittest.main()
