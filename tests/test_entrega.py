"""Testes do pós-venda: envio com rastreio e documentos do pedido (nota fiscal, XML, boleto)."""
import io
import unittest

from test_b2b import BaseB2B

from app import app  # noqa: E402
import entrega  # noqa: E402
from models import Order, PedidoArquivo, db  # noqa: E402

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
XML = b'<?xml version="1.0" encoding="UTF-8"?><nfeProc><NFe></NFe></nfeProc>'


class BaseEntrega(BaseB2B):
    def pedido_pago(self, c=None):
        c = c or self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        with app.app_context():
            oid = Order.query.order_by(Order.id.desc()).first().id
        return c, oid

    def anexar(self, a, oid, conteudo, nome, tipo="nota", avisar=True):
        dados = {"_csrf": self.token(a, f"/admin/orders/{oid}"), "tipo": tipo,
                 "arquivo": (io.BytesIO(conteudo), nome)}
        if avisar:
            dados["avisar"] = "on"
        return a.post(f"/admin/orders/{oid}/arquivos", data=dados, content_type="multipart/form-data")

    def para_cliente(self):
        return [e for e in self.emails if e["para"] == "compras@ex.com"]


class TesteEnvio(BaseEntrega):
    def test_envio_com_rastreio_avisa_e_aparece_para_o_cliente(self):
        c, oid = self.pedido_pago()
        a = self.admin()
        del self.emails[:]
        self.post(a, f"/admin/orders/{oid}/envio", {"acao": "enviar", "transportadora": "Correios",
                                                     "rastreio": "AA123456789BR",
                                                     "rastreio_link": "https://rastreio.exemplo/AA123"},
                  f"/admin/orders/{oid}")
        with app.app_context():
            o = db.session.get(Order, oid)
            self.assertEqual((o.envio, o.transportadora, o.rastreio), ("enviado", "Correios", "AA123456789BR"))
            self.assertIsNotNone(o.enviado_em)
        email = self.para_cliente()[-1]
        self.assertIn("enviado", email["assunto"])
        self.assertIn("AA123456789BR", email["texto"])
        self.assertIn("https://rastreio.exemplo/AA123", email["texto"])
        html = c.get(f"/conta/pedidos/{oid}").get_data(as_text=True)
        self.assertIn("Pedido enviado", html)
        self.assertIn("AA123456789BR", html)
        self.assertNotIn("Cancelar pedido", html)
        self.assertIn("Enviado", c.get("/conta/pedidos").get_data(as_text=True))

        self.post(a, f"/admin/orders/{oid}/envio", {"acao": "entregue"}, f"/admin/orders/{oid}")
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).envio, "entregue")
        self.assertIn("entregue", self.para_cliente()[-1]["assunto"])
        self.post(a, f"/admin/orders/{oid}/envio", {"acao": "desfazer"}, f"/admin/orders/{oid}")
        with app.app_context():
            o = db.session.get(Order, oid)
            self.assertEqual((o.envio, o.entregue_em), ("enviado", None))

    def test_editar_rastreio_nao_reenvia_sem_pedir(self):
        _, oid = self.pedido_pago()
        a = self.admin()
        dados = {"acao": "enviar", "transportadora": "Jadlog"}
        self.post(a, f"/admin/orders/{oid}/envio", dados, f"/admin/orders/{oid}")
        n = len(self.para_cliente())
        self.post(a, f"/admin/orders/{oid}/envio", {**dados, "rastreio": "123"}, f"/admin/orders/{oid}")
        self.assertEqual(len(self.para_cliente()), n)
        self.post(a, f"/admin/orders/{oid}/envio", {**dados, "rastreio": "456", "avisar": "on"}, f"/admin/orders/{oid}")
        self.assertEqual(len(self.para_cliente()), n + 1)

    def test_validacoes_do_envio(self):
        self.assertIsNone(entrega.ler_envio({"transportadora": ""})[0])
        self.assertIsNone(entrega.ler_envio({"transportadora": "X", "rastreio_link": "javascript:alert(1)"})[0])
        c, oid = self.pedido_pago()
        a = self.admin()
        self.post(a, f"/admin/orders/{oid}/status", {"status": "cancelado"}, "/admin/orders")
        self.post(a, f"/admin/orders/{oid}/envio", {"acao": "enviar", "transportadora": "Correios"}, f"/admin/orders/{oid}")
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).envio, "")

    def test_cliente_nao_cancela_pedido_enviado(self):
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "boleto")
        with app.app_context():
            oid = Order.query.order_by(Order.id.desc()).first().id
        self.post(self.admin(), f"/admin/orders/{oid}/envio", {"acao": "enviar", "transportadora": "Própria"},
                  f"/admin/orders/{oid}")
        self.post(c, f"/conta/pedidos/{oid}/cancelar", {}, f"/conta/pedidos/{oid}")
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).status, "pendente")


class TesteDocumentos(BaseEntrega):
    def test_nota_anexada_cliente_baixa(self):
        c, oid = self.pedido_pago()
        a = self.admin()
        del self.emails[:]
        r = self.anexar(a, oid, PDF, "NF 1234 ção.pdf")
        self.assertEqual(r.status_code, 302)
        self.assertIn("nota fiscal", self.para_cliente()[-1]["assunto"].lower())
        with app.app_context():
            arq = PedidoArquivo.query.one()
            aid, nome = arq.id, arq.nome
        self.assertEqual(nome, "NF-1234-cao.pdf")
        html = c.get(f"/conta/pedidos/{oid}").get_data(as_text=True)
        self.assertIn("Nota fiscal (DANFE)", html)
        r = c.get(f"/conta/pedidos/{oid}/documentos/{aid}")
        self.assertEqual((r.status_code, r.mimetype, r.data), (200, "application/pdf", PDF))
        self.assertIn("attachment", r.headers["Content-Disposition"])
        r = a.get(f"/admin/orders/{oid}/arquivos/{aid}")
        self.assertEqual(r.data, PDF)

    def test_outro_cliente_nao_baixa(self):
        _, oid = self.pedido_pago()
        self.anexar(self.admin(), oid, PDF, "nota.pdf")
        with app.app_context():
            aid = PedidoArquivo.query.one().id
        outro = self.cadastrar("outro@teste.com")
        self.assertEqual(outro.get(f"/conta/pedidos/{oid}/documentos/{aid}").status_code, 404)
        self.assertEqual(self.cliente().get(f"/conta/pedidos/{oid}/documentos/{aid}").status_code, 302)
        _, oid2 = self.pedido_pago(outro)
        self.assertEqual(self.admin().get(f"/admin/orders/{oid2}/arquivos/{aid}").status_code, 404)

    def test_xml_e_arquivo_invalido(self):
        _, oid = self.pedido_pago()
        a = self.admin()
        self.anexar(a, oid, XML, "nfe.xml", tipo="nota", avisar=False)  # XML enviado como "nota" vira "xml"
        self.anexar(a, oid, b"MZ\x90\x00 programa", "virus.exe")
        self.anexar(a, oid, PDF, "nota.pdf", tipo="xml")
        with app.app_context():
            arquivos = [(x.tipo, x.nome, x.mime) for x in PedidoArquivo.query.all()]
        self.assertEqual(arquivos, [("xml", "nfe.xml", "application/xml")])
        html = a.get(f"/admin/orders/{oid}").get_data(as_text=True)
        self.assertIn("nfe.xml", html)

    def test_remover_documento(self):
        _, oid = self.pedido_pago()
        a = self.admin()
        self.anexar(a, oid, PDF, "boleto.pdf", tipo="boleto")
        with app.app_context():
            aid = PedidoArquivo.query.one().id
        self.post(a, f"/admin/orders/{oid}/arquivos/{aid}/excluir", {}, f"/admin/orders/{oid}")
        with app.app_context():
            self.assertEqual(PedidoArquivo.query.count(), 0)

    def test_planilha_e_backup_levam_os_dados(self):
        _, oid = self.pedido_pago()
        a = self.admin()
        self.post(a, f"/admin/orders/{oid}/envio", {"acao": "enviar", "transportadora": "Correios", "rastreio": "BR1"},
                  f"/admin/orders/{oid}")
        self.anexar(a, oid, PDF, "nota.pdf", avisar=False)
        import backup
        with app.app_context():
            _, resumo = backup.gerar()
        self.assertEqual(resumo["pedido_arquivo"], 1)
        self.assertEqual(a.get("/admin/planilhas/pedidos.xlsx").status_code, 200)


if __name__ == "__main__":
    unittest.main()
