"""Testes da área do cliente: notas e boletos, comprar de novo e endereços de entrega."""
import io
import unittest

from test_b2b import DADOS, BaseB2B

from app import app  # noqa: E402
import lgpd  # noqa: E402
from models import Endereco, Order, User, db  # noqa: E402

PDF = b"%PDF-1.4\n%%EOF"


class BaseConta(BaseB2B):
    def ultimo_id(self):
        with app.app_context():
            return Order.query.order_by(Order.id.desc()).first().id

    def anexar(self, oid, nome, tipo="nota"):
        a = self.admin()
        a.post(f"/admin/orders/{oid}/arquivos", data={"_csrf": self.token(a, f"/admin/orders/{oid}"), "tipo": tipo,
                                                      "arquivo": (io.BytesIO(PDF), nome)},
               content_type="multipart/form-data")

    def uid(self, email="cli@teste.com"):
        with app.app_context():
            return User.query.filter_by(email=email).one().id


class TesteDocumentosDaConta(BaseConta):
    def test_lista_so_os_meus_documentos_com_filtro(self):
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        o1 = self.ultimo_id()
        self.adicionar(c, 2, 1)
        self.fechar(c, "boleto")
        o2 = self.ultimo_id()
        self.anexar(o1, "nota-1.pdf")
        self.anexar(o2, "boleto-2.pdf", tipo="boleto")
        outro = self.cadastrar("outro@teste.com")
        self.adicionar(outro, 1, 1)
        self.fechar(outro, "pix")
        self.anexar(self.ultimo_id(), "nota-do-outro.pdf")

        html = c.get("/conta/documentos").get_data(as_text=True)
        self.assertIn("nota-1.pdf", html)
        self.assertIn("boleto-2.pdf", html)
        self.assertNotIn("nota-do-outro.pdf", html)
        html = c.get("/conta/documentos?tipo=boleto").get_data(as_text=True)
        self.assertIn("boleto-2.pdf", html)
        self.assertNotIn("nota-1.pdf", html)
        self.assertIn("Nenhum documento com esse filtro", c.get("/conta/documentos?mes=1999-01").get_data(as_text=True))
        self.assertIn("Ainda não há documentos", self.cadastrar("vazio@teste.com").get("/conta/documentos")
                      .get_data(as_text=True))


class TesteComprarDeNovo(BaseConta):
    def test_lista_e_manda_marcados_ao_carrinho(self):
        self.ajustar_produto(1, qtd_minima=1, multiplo=1)
        self.ajustar_produto(2, qtd_minima=1, multiplo=1)
        c = self.cadastrar()
        self.adicionar(c, 1, 3)
        self.fechar(c, "pix")
        self.adicionar(c, 1, 2)
        self.adicionar(c, 2, 1)
        self.fechar(c, "pix")
        self.adicionar(c, 3, 1)
        self.fechar(c, "orcamento")  # orçamento não entra
        html = c.get("/conta/produtos").get_data(as_text=True)
        self.assertIn("5 un em 2 pedido(s)", html)
        self.assertNotIn('value="3" aria-label="Comprar', html)
        with app.app_context():
            from models import Product
            nome3 = db.session.get(Product, 3).name
        self.assertNotIn(nome3, html)

        self.ajustar_produto(2, stock=0)
        r = self.post(c, "/conta/produtos", {"pid": ["1", "2", "999"], "qtd_1": "4", "qtd_2": "1"}, "/conta/produtos")
        self.assertIn("/carrinho", r.headers["Location"])
        with c.session_transaction() as s:
            self.assertEqual(s["cart"], {"1": 4})

    def test_sem_compras(self):
        self.assertIn("ainda não tem compras", self.cadastrar().get("/conta/produtos").get_data(as_text=True))


class TesteEnderecos(BaseConta):
    def test_cadastro_principal_e_exclusao(self):
        c = self.cadastrar()
        self.post(c, "/conta/enderecos", {"apelido": "Matriz", "cep": "01001-000", "endereco": "Rua A, 100, Centro, SP"},
                  "/conta/enderecos")
        self.post(c, "/conta/enderecos", {"apelido": "Obra", "cep": "13010-000", "endereco": "Av. B, 2, Campinas/SP"},
                  "/conta/enderecos")
        r = self.post(c, "/conta/enderecos", {"apelido": "Obra", "cep": "123", "endereco": "x"}, "/conta/enderecos")
        self.assertIn("CEP válido", r.get_data(as_text=True))
        with app.app_context():
            ends = {e.apelido: (e.cep, e.padrao, e.id) for e in Endereco.query.all()}
        self.assertEqual(ends["Matriz"][:2], ("01001000", True))
        self.assertEqual(ends["Obra"][:2], ("13010000", False))

        self.post(c, f"/conta/enderecos/{ends['Obra'][2]}/padrao", {}, "/conta/enderecos")
        self.post(c, f"/conta/enderecos/{ends['Obra'][2]}/excluir", {}, "/conta/enderecos")
        with app.app_context():
            self.assertEqual([(e.apelido, e.padrao) for e in Endereco.query.all()], [("Matriz", True)])

    def test_nao_mexe_no_endereco_dos_outros(self):
        c = self.cadastrar()
        self.post(c, "/conta/enderecos", {"apelido": "Matriz", "cep": "01001-000", "endereco": "Rua A, 100, Centro"},
                  "/conta/enderecos")
        with app.app_context():
            eid = Endereco.query.one().id
        outro = self.cadastrar("outro@teste.com")
        self.assertEqual(self.post(outro, f"/conta/enderecos/{eid}/excluir", {}, "/conta").status_code, 404)
        self.assertEqual(self.post(outro, "/conta/enderecos", {"eid": eid, "apelido": "X", "cep": "01001000",
                                                                "endereco": "Rua invadida, 1"}, "/conta").status_code, 404)
        with app.app_context():
            self.assertEqual(Endereco.query.one().apelido, "Matriz")

    def test_finalizacao_usa_e_guarda_enderecos(self):
        c = self.cadastrar()
        self.post(c, "/conta/enderecos", {"apelido": "Matriz", "cep": "01001-000", "endereco": "Rua Matriz, 100, SP"},
                  "/conta/enderecos")
        self.adicionar(c, 1, 1)
        html = c.get("/checkout").get_data(as_text=True)
        self.assertIn('value="Rua Matriz, 100, SP"', html)
        self.assertIn('value="01001-000"', html)
        self.assertIn('id="endereco-salvo"', html)
        # endereço novo, guardado na conta; o mesmo de novo não duplica
        self.fechar(c, "pix", address="Av. Nova, 5, Campinas", cep="13010-000", guardar_endereco="on")
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix", address="Av.  Nova, 5, Campinas", cep="13010000", guardar_endereco="on")
        with app.app_context():
            ends = [(e.apelido, e.cep, e.padrao) for e in Endereco.query.order_by(Endereco.id).all()]
        self.assertEqual(ends, [("Matriz", "01001000", True), ("Endereço 2", "13010000", False)])

    def test_lgpd_exporta_e_exclusao_apaga_enderecos(self):
        c = self.cadastrar()
        self.post(c, "/conta/enderecos", {"apelido": "Casa", "cep": "01001-000", "endereco": "Rua A, 1, SP"},
                  "/conta/enderecos")
        with app.app_context():
            u = db.session.get(User, self.uid())
            self.assertEqual(lgpd.exportar(u)["enderecos"][0]["nome"], "Casa")
            self.assertEqual(lgpd.excluir_conta(u), 0)
            self.assertEqual(Endereco.query.count(), 0)

    def test_menu_da_conta(self):
        html = self.cadastrar().get("/conta").get_data(as_text=True)
        for texto in ("Comprar de novo", "Notas e boletos", "Endereços"):
            self.assertIn(texto, html)


if __name__ == "__main__":
    unittest.main()
