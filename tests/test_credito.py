"""Testes do limite de crédito do boleto faturado."""
import unittest
from decimal import Decimal

from test_b2b import BaseB2B

from app import app  # noqa: E402
import credito  # noqa: E402
from models import Order, Product, User, db  # noqa: E402


class TesteCredito(BaseB2B):
    def setUp(self):
        super().setUp()
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=100)
        with app.app_context():
            self.preco = db.session.get(Product, 1).price

    def limite(self, valor):
        with app.app_context():
            u = User.query.filter_by(email="empresa@teste.com").one()
            u.limite_credito = Decimal(valor) if valor is not None else None
            db.session.commit()

    def ultimo(self):
        with app.app_context():
            o = Order.query.order_by(Order.id.desc()).first()
            return o.id, o.payment_method, o.status, o.total

    def test_faturado_respeita_o_limite(self):
        c = self.empresa(desconto="0", prazos="30")
        self.limite(self.preco * 2 + Decimal("0.01"))  # cabem 2 unidades em aberto
        self.adicionar(c, 1, 2)
        self.fechar(c, "faturado", prazo="30")
        oid, metodo, status, _ = self.ultimo()
        self.assertEqual((metodo, status), ("faturado", "pendente"))
        # o próximo não cabe: a opção aparece bloqueada e o envio é recusado
        self.adicionar(c, 1, 1)
        html = c.get("/checkout").get_data(as_text=True)
        self.assertIn("crédito disponível: R$ 0,01 (menor que este pedido)", html)
        self.assertRegex(html, r'value="faturado"[^>]*disabled')
        r = self.fechar(c, "faturado", prazo="30")
        self.assertIn("passa do limite de crédito", r.get_data(as_text=True))
        self.assertEqual(self.ultimo()[0], oid, "nenhum pedido novo")
        # pago libera o crédito
        self.post(self.admin(), f"/admin/orders/{oid}/status", {"status": "pago"}, "/admin/orders")
        self.fechar(c, "faturado", prazo="30")
        self.assertEqual(self.ultimo()[1:3], ("faturado", "pendente"))
        self.assertIn("Limite de crédito do faturado", c.get("/conta").get_data(as_text=True))

    def test_sem_limite_e_outras_formas(self):
        c = self.empresa(desconto="0", prazos="30")
        self.adicionar(c, 1, 50)
        self.fechar(c, "faturado", prazo="30")
        self.assertEqual(self.ultimo()[1], "faturado", "sem limite cadastrado: sem teto")
        self.limite("1")
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        self.assertEqual(self.ultimo()[1], "pix", "o limite só vale para o faturado")

    def test_painel_cadastra_e_mostra(self):
        c = self.empresa(desconto="0", prazos="30")
        self.adicionar(c, 1, 1)
        self.fechar(c, "faturado", prazo="30")
        a = self.admin()
        with app.app_context():
            uid = User.query.filter_by(email="empresa@teste.com").one().id
        base = {"aprovada": "on", "desconto": "0", "prazos": "30", "razao_social": "Metalúrgica Teste Ltda"}
        self.post(a, f"/admin/clientes/{uid}", {**base, "limite_credito": "10.000,00"}, f"/admin/clientes/{uid}")
        with app.app_context():
            self.assertEqual(db.session.get(User, uid).limite_credito, Decimal("10000.00"))
        html = a.get(f"/admin/clientes/{uid}").get_data(as_text=True)
        self.assertIn("Em aberto no faturado agora: <b>R$", html)
        self.assertIn("disponível: <b>R$", html)
        self.post(a, f"/admin/clientes/{uid}", {**base, "limite_credito": "abc"}, f"/admin/clientes/{uid}")
        with app.app_context():
            self.assertEqual(db.session.get(User, uid).limite_credito, Decimal("10000.00"))
        self.post(a, f"/admin/clientes/{uid}", {**base, "limite_credito": ""}, f"/admin/clientes/{uid}")
        with app.app_context():
            self.assertIsNone(db.session.get(User, uid).limite_credito)

    def test_alterar_pedido_e_orcamento_respeitam(self):
        c = self.empresa(desconto="0", prazos="30")
        self.limite(self.preco * 3)
        self.adicionar(c, 1, 2)
        self.fechar(c, "faturado", prazo="30")
        oid = self.ultimo()[0]
        self.post(c, f"/conta/pedidos/{oid}/alterar", {"qtd_1": "5"}, f"/conta/pedidos/{oid}")
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).items[0].quantity, 2, "aumentar além do limite é recusado")
        self.assertEqual(self.estoque(1), 98, "e o estoque volta como estava")
        self.post(c, f"/conta/pedidos/{oid}/alterar", {"qtd_1": "3"}, f"/conta/pedidos/{oid}")
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).items[0].quantity, 3, "dentro do limite pode")
            self.assertEqual(credito.disponivel(User.query.filter_by(email="empresa@teste.com").one()), Decimal("0.00"))
        # orçamento aprovado no faturado também respeita
        self.adicionar(c, 1, 1)
        self.fechar(c, "orcamento")
        orc = self.ultimo()[0]
        with app.app_context():
            item = db.session.get(Order, orc).items[0].id
        a = self.admin()
        self.post(a, f"/admin/orders/{orc}/orcamento", {"acao": "aprovar", f"preco_{item}": "100,00",
                                                        "pagamento": "faturado", "prazo": "30", "frete": "0"},
                  f"/admin/orders/{orc}/orcamento")
        self.assertEqual(self.ultimo()[2], "orcamento")
        self.assertIn("passa do limite de crédito", a.get(f"/admin/orders/{orc}/orcamento").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
