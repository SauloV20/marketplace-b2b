"""Testes do cupom de desconto (painel, carrinho, pedido, orçamento) e do alerta de estoque baixo."""
import unittest
from datetime import date, timedelta
from decimal import Decimal

from test_b2b import BaseB2B

from app import app  # noqa: E402
import cupons  # noqa: E402
from models import Cupom, Order, Product, db  # noqa: E402


class BaseCupom(BaseB2B):
    def cupom(self, codigo="DESC10", tipo="percentual", valor="10", **campos):
        with app.app_context():
            db.session.add(Cupom(codigo=codigo, tipo=tipo, valor=Decimal(valor), **campos))
            db.session.commit()

    def aplicar(self, c, codigo):
        self.post(c, "/carrinho/cupom", {"codigo": codigo}, "/carrinho")
        return c.get("/carrinho").get_data(as_text=True)

    def pedido(self):
        with app.app_context():
            o = Order.query.order_by(Order.id.desc()).first()
            return o.id, o.cupom, o.desconto, o.subtotal, o.frete, o.total


class TesteCupom(BaseCupom):
    def test_cupom_percentual_entra_no_pedido(self):
        self.cupom()
        c = self.cadastrar()
        self.adicionar(c, 1, 2)
        html = self.aplicar(c, " desc10 ")
        self.assertIn("Cupom DESC10", html)
        self.assertIn("10% de desconto", html)
        self.fechar(c, "pix")
        _, codigo, desconto, subtotal, frete, total = self.pedido()
        self.assertEqual(codigo, "DESC10")
        self.assertEqual(desconto, (subtotal / 10).quantize(Decimal("0.01")))
        self.assertEqual(total, subtotal - desconto + frete)
        self.assertIn("Cupom DESC10: -", self.emails[-1]["texto"] + self.emails[-2]["texto"])
        self.assertIn("Remover cupom", html)
        self.assertNotIn("Cupom DESC10", c.get("/carrinho").get_data(as_text=True), "o carrinho novo começa sem cupom")

    def test_carrinho_ja_mostra_o_total_com_desconto(self):
        self.cupom("FIXO", tipo="valor", valor="5")
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        with app.app_context():
            preco = db.session.get(Product, 1).price
        brl = lambda v: "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        html = self.aplicar(c, "FIXO")  # sem CEP informado
        self.assertIn('Total <small class="mute">(sem o frete)</small></span><b>' + brl(preco - 5), html)
        self.assertIn("Você economiza R$ 5,00", html)

    def test_caixa_de_codigo_no_carrinho(self):
        self.ajustar_produto(1, codigo="FRESA-10", qtd_minima=1, multiplo=1)
        c = self.cadastrar()
        self.assertIn("Adicionar por código", c.get("/carrinho").get_data(as_text=True), "aparece com o carrinho vazio")
        r = self.post(c, "/pedido-rapido", {"acao": "adicionar", "codigo": " fresa-10 ", "qtd": "3"}, "/carrinho")
        self.assertIn("/carrinho", r.headers["Location"])
        html = c.get("/carrinho").get_data(as_text=True)
        self.assertIn('name="qty_1" form="carrinho-qtd" type="number" min="0" max', html)
        self.assertIn('value="3"', html)
        r = self.post(c, "/pedido-rapido", {"acao": "adicionar", "codigo": "NAO-EXISTE", "qtd": "1"}, "/carrinho")
        self.assertEqual(r.status_code, 200, "código errado mostra a conferência com o motivo")
        self.assertIn("NAO-EXISTE", r.get_data(as_text=True))

    def test_valor_fixo_nao_passa_do_subtotal(self):
        c = Cupom(codigo="X", tipo="valor", valor=Decimal("500"))
        self.assertEqual(cupons.calcular(c, Decimal("120.00")), Decimal("120.00"))
        self.assertEqual(cupons.calcular(Cupom(codigo="Y", tipo="percentual", valor=Decimal("12.5")),
                                         Decimal("99.99")), Decimal("12.50"))

    def test_regras_do_cupom(self):
        self.cupom("VENCIDO", validade=date.today() - timedelta(days=2))
        self.cupom("OFF", ativo=False)
        self.cupom("GRANDE", minimo=Decimal("100000"))
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.assertIn("venceu em", self.aplicar(c, "VENCIDO"))
        self.assertIn("Cupom não encontrado", self.aplicar(c, "OFF"))
        self.assertIn("Cupom não encontrado", self.aplicar(c, "NAOEXISTE"))
        self.assertIn("a partir de R$ 100.000,00", self.aplicar(c, "GRANDE"))
        with app.app_context():
            self.assertEqual(Order.query.filter(Order.cupom != "").count(), 0)

    def test_uma_vez_por_cliente_e_limite_total(self):
        self.cupom("UMA", um_por_cliente=True)
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.aplicar(c, "UMA")
        self.fechar(c, "pix")
        self.adicionar(c, 1, 1)
        self.assertIn("Você já usou o cupom UMA", self.aplicar(c, "UMA"))
        # cancelado não conta: libera de novo
        oid = self.pedido()[0]
        self.post(self.admin(), f"/admin/orders/{oid}/status", {"status": "cancelado"}, "/admin/orders")
        self.assertIn("Cupom UMA", self.aplicar(c, "UMA"))

        self.cupom("LIMITE", limite_usos=1, um_por_cliente=False)
        outro = self.cadastrar("outro@teste.com")
        self.adicionar(outro, 1, 1)
        self.aplicar(outro, "LIMITE")
        self.fechar(outro, "pix")
        self.assertIn("o máximo de vezes", self.aplicar(c, "LIMITE"))

    def test_cupom_que_deixa_de_valer_trava_a_finalizacao(self):
        self.cupom("MIN", minimo=Decimal("1"))
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.aplicar(c, "MIN")
        self.ajustar_produto(1, price=Decimal("0.50"))  # o carrinho ficou abaixo do mínimo do cupom
        r = self.fechar(c, "pix")
        self.assertIn("/carrinho", r.headers["Location"])
        self.post(c, "/carrinho/cupom", {"acao": "remover"}, "/carrinho")
        self.assertIn("/pedido/", self.fechar(c, "pix").headers["Location"])
        self.assertEqual(self.pedido()[1:3], ("", 0))

    def test_frete_por_cep_considera_o_desconto(self):
        self.cupom("FIXO", tipo="valor", valor="5")
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.aplicar(c, "FIXO")
        with app.app_context():
            preco = db.session.get(Product, 1).price
        total = c.get("/carrinho/frete?cep=01001000").get_json()["total"]
        self.assertEqual(total, "R$ " + f"{preco - 5:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))

    def test_orcamento_recalcula_o_percentual(self):
        self.cupom()
        c = self.cadastrar()
        self.adicionar(c, 1, 2)
        self.aplicar(c, "DESC10")
        self.fechar(c, "orcamento")
        with app.app_context():
            o = Order.query.order_by(Order.id.desc()).first()
            oid, item = o.id, o.items[0].id
        self.post(self.admin(), f"/admin/orders/{oid}/orcamento",
                  {"acao": "aprovar", f"preco_{item}": "100,00", "pagamento": "pix", "frete": "20,00"},
                  f"/admin/orders/{oid}/orcamento")
        _, _, desconto, subtotal, frete, total = self.pedido()
        self.assertEqual((subtotal, desconto, total), (Decimal("200.00"), Decimal("20.00"), Decimal("200.00")))

    def test_painel_cria_edita_e_exclui(self):
        a = self.admin()
        base = {"codigo": "bem vindo", "tipo": "percentual", "valor": "10", "minimo": "150,00",
                "validade": "2030-01-31", "limite_usos": "", "um_por_cliente": "on"}
        self.post(a, "/admin/cupons", base, "/admin/cupons")
        with app.app_context():
            c = Cupom.query.one()
            self.assertEqual((c.codigo, c.valor, c.minimo, c.validade, c.limite_usos, c.um_por_cliente, c.ativo),
                             ("BEMVINDO", Decimal("10.00"), Decimal("150.00"), date(2030, 1, 31), None, True, True))
            cid = c.id
        r = self.post(a, "/admin/cupons", {**base, "codigo": "OUTRO", "valor": "150"}, "/admin/cupons")
        self.assertIn("vai de 0,01 a 100", a.get("/admin/cupons").get_data(as_text=True))
        self.post(a, "/admin/cupons", {**base, "cid": cid, "tipo": "valor", "valor": "25,50"}, "/admin/cupons")
        with app.app_context():
            c = db.session.get(Cupom, cid)
            self.assertEqual((c.tipo, c.valor, c.ativo), ("valor", Decimal("25.50"), False))
        self.assertIn("BEMVINDO", a.get("/admin/cupons").get_data(as_text=True))
        self.post(a, f"/admin/cupons/{cid}/excluir", {}, "/admin/cupons")
        with app.app_context():
            self.assertIsNone(db.session.get(Cupom, cid))
        self.assertEqual(r.status_code, 302)

    def test_cupom_usado_e_desativado_em_vez_de_excluido(self):
        self.cupom()
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.aplicar(c, "DESC10")
        self.fechar(c, "pix")
        a = self.admin()
        with app.app_context():
            cid = Cupom.query.one().id
        self.post(a, f"/admin/cupons/{cid}/excluir", {}, "/admin/cupons")
        with app.app_context():
            self.assertFalse(db.session.get(Cupom, cid).ativo)
        html = a.get("/admin/cupons").get_data(as_text=True)
        self.assertIn("1 uso(s)", html)


class TesteEstoqueBaixo(BaseCupom):
    def avisos_de_estoque(self):
        return [e for e in self.emails if e["para"] == "admin@exemplo.com" and "Estoque baixo" in e["assunto"]]

    def test_avisa_uma_vez_ao_cruzar_o_nivel(self):
        self.ajustar_produto(1, stock=12, estoque_alerta=10, qtd_minima=1, multiplo=1)
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        self.assertEqual(self.avisos_de_estoque(), [], "11 ainda está acima do alerta")
        self.adicionar(c, 1, 2)
        self.fechar(c, "pix")
        aviso = self.avisos_de_estoque()
        self.assertEqual(len(aviso), 1)
        self.assertIn("restam 9 un (alerta em 10)", aviso[0]["texto"])
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        self.assertEqual(len(self.avisos_de_estoque()), 1, "não repete a cada venda")
        html = self.admin().get("/admin/").get_data(as_text=True)
        self.assertIn("Estoque baixo (1)", html)

    def test_orcamento_nao_avisa_ate_aprovar(self):
        self.ajustar_produto(1, stock=3, estoque_alerta=2, qtd_minima=1, multiplo=1)
        c = self.cadastrar()
        self.adicionar(c, 1, 2)
        self.fechar(c, "orcamento")
        self.assertEqual(self.avisos_de_estoque(), [])
        with app.app_context():
            o = Order.query.order_by(Order.id.desc()).first()
            oid, item = o.id, o.items[0].id
        self.post(self.admin(), f"/admin/orders/{oid}/orcamento",
                  {"acao": "aprovar", f"preco_{item}": "10,00", "pagamento": "pix", "frete": "0"},
                  f"/admin/orders/{oid}/orcamento")
        self.assertEqual(len(self.avisos_de_estoque()), 1)

    def test_alerta_no_cadastro_do_produto(self):
        a = self.admin()
        with app.app_context():
            p = db.session.get(Product, 2)
            form = {"name": p.name, "category_id": p.category_id, "price": "149,90", "stock": 50, "active": "on",
                    "qtd_minima": 1, "multiplo": 1, "estoque_alerta": "15"}
        self.post(a, "/admin/products/2/edit", form, "/admin/products/2/edit")
        with app.app_context():
            self.assertEqual(db.session.get(Product, 2).estoque_alerta, 15)
        self.post(a, "/admin/products/2/edit", {**form, "estoque_alerta": "-1"}, "/admin/products/2/edit")
        with app.app_context():
            self.assertEqual(db.session.get(Product, 2).estoque_alerta, 15)


if __name__ == "__main__":
    unittest.main()
