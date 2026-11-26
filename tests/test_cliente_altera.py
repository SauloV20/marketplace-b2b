"""Testes do cliente alterando o próprio pedido (aguardando pagamento) ou orçamento."""
import unittest
from decimal import Decimal

from test_b2b import BaseB2B

from app import app  # noqa: E402
from models import Order, Product, db  # noqa: E402


class TesteClienteAltera(BaseB2B):
    def setUp(self):
        super().setUp()
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50, codigo="FRESA-10")
        self.ajustar_produto(2, qtd_minima=1, multiplo=1, stock=50, codigo="SERRA-X")

    def pedido(self, c, pagamento="boleto", qtd=5):
        self.adicionar(c, 1, qtd)
        self.fechar(c, pagamento)
        with app.app_context():
            return Order.query.order_by(Order.id.desc()).first().id

    def alterar(self, c, oid, dados):
        return self.post(c, f"/conta/pedidos/{oid}/alterar", dados, f"/conta/pedidos/{oid}")

    def ver(self, oid):
        with app.app_context():
            o = db.session.get(Order, oid)
            return o.status, sorted((i.product_id, i.quantity, i.unit_price) for i in o.items), o.total, o.subtotal

    def test_mudar_quantidade_incluir_e_tirar(self):
        c = self.cadastrar()
        oid = self.pedido(c)
        html = c.get(f"/conta/pedidos/{oid}").get_data(as_text=True)
        self.assertIn("Alterar este pedido", html)
        self.assertIn(f'name="qtd_1" type="number" min="0" value="5"', html)
        del self.emails[:]
        self.alterar(c, oid, {"qtd_1": "2", "novo_produto": "serra-x", "novo_qtd": "3"})
        self.assertEqual((self.estoque(1), self.estoque(2)), (48, 47))
        with app.app_context():
            p1, p2 = db.session.get(Product, 1), db.session.get(Product, 2)
            esperado = [(1, 2, p1.price), (2, 3, p2.price)]
        status, itens, total, subtotal = self.ver(oid)
        self.assertEqual(itens, esperado)
        self.assertEqual(total, subtotal)  # sem regra de frete: a combinar
        self.assertTrue(any("Cliente alterou o pedido" in e["assunto"] for e in self.emails if e["para"] == "admin@exemplo.com"))
        self.alterar(c, oid, {"qtd_1": "0", "qtd_2": "3"})
        self.assertEqual(self.estoque(1), 50, "tirar devolve o estoque")
        self.assertEqual([i[0] for i in self.ver(oid)[1]], [2])

    def test_preco_combinado_fica_nos_itens_que_nao_mudaram(self):
        c = self.cadastrar()
        oid = self.pedido(c)
        with app.app_context():
            o = db.session.get(Order, oid)
            o.items[0].unit_price = Decimal("99.00")  # preço combinado pela loja
            db.session.commit()
        self.alterar(c, oid, {"qtd_1": "5", "novo_produto": "SERRA-X", "novo_qtd": "1"})
        self.assertIn((1, 5, Decimal("99.00")), self.ver(oid)[1])

    def test_estoque_que_o_pedido_ja_segura_conta(self):
        self.ajustar_produto(1, stock=6)
        c = self.cadastrar()
        oid = self.pedido(c, qtd=5)  # sobra 1 na loja
        self.alterar(c, oid, {"qtd_1": "6"})
        self.assertEqual((self.ver(oid)[1][0][1], self.estoque(1)), (6, 0))
        r = self.alterar(c, oid, {"qtd_1": "50"})  # ajusta ao máximo possível
        self.assertEqual(self.ver(oid)[1][0][1], 6)
        self.assertEqual(r.status_code, 302)

    def test_multiplo_ajusta(self):
        self.ajustar_produto(2, multiplo=10, qtd_minima=10, stock=100)
        c = self.cadastrar()
        oid = self.pedido(c)
        self.alterar(c, oid, {"novo_produto": "SERRA-X", "novo_qtd": "13"})
        self.assertIn(20, [q for pid, q, _ in self.ver(oid)[1] if pid == 2])
        self.assertIn("ajustado para 20 un", c.get(f"/conta/pedidos/{oid}").get_data(as_text=True))

    def test_bloqueios(self):
        c = self.cadastrar()
        oid = self.pedido(c)
        r = self.alterar(c, oid, {"qtd_1": "0"})
        self.assertIn("ficaria sem itens", c.get(f"/conta/pedidos/{oid}").get_data(as_text=True))
        self.post(self.admin(), f"/admin/orders/{oid}/status", {"status": "pago"}, "/admin/orders")
        html = c.get(f"/conta/pedidos/{oid}").get_data(as_text=True)
        self.assertNotIn("Alterar este pedido", html)
        self.alterar(c, oid, {"qtd_1": "1"})
        self.assertEqual(self.ver(oid)[1][0][1], 5)
        oid2 = self.pedido(c)
        with app.app_context():
            db.session.get(Order, oid2).pix_txid = "abc"
            db.session.commit()
        self.alterar(c, oid2, {"qtd_1": "1"})
        self.assertEqual(self.ver(oid2)[1][0][1], 5)
        outro = self.cadastrar("outro@teste.com")
        oid3 = self.pedido(outro)
        self.assertEqual(self.post(c, f"/conta/pedidos/{oid3}/alterar", {"qtd_1": "1"}, "/conta").status_code, 404)
        self.assertEqual(r.status_code, 302)

    def test_orcamento_nao_mexe_no_estoque_e_pedido_minimo(self):
        c = self.cadastrar()
        oid = self.pedido(c, pagamento="orcamento")
        self.assertIn("Alterar este orçamento", c.get(f"/conta/pedidos/{oid}").get_data(as_text=True))
        self.alterar(c, oid, {"qtd_1": "8"})
        self.assertEqual((self.ver(oid)[1][0][1], self.estoque(1)), (8, 50))
        with app.app_context():
            from b2b import salvar_config
            salvar_config("pedido_minimo", Decimal("100000"))
            db.session.commit()
        self.alterar(c, oid, {"qtd_1": "1"})
        self.assertEqual(self.ver(oid)[1][0][1], 8)
        self.assertIn("pedido mínimo", c.get(f"/conta/pedidos/{oid}").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
