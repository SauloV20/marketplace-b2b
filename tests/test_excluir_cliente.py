"""Testes da exclusão de clientes pelo painel."""
import unittest

from test_b2b import BaseB2B

from app import app  # noqa: E402
from models import Order, User, db  # noqa: E402


class TesteExcluirCliente(BaseB2B):
    def uid(self, email="cli@teste.com"):
        with app.app_context():
            u = User.query.filter_by(email=email).first()
            return u.id if u else None

    def excluir(self, a, uid):
        return self.post(a, f"/admin/clientes/{uid}/excluir", {})

    def test_exclui_e_mantem_os_pedidos(self):
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        oid = self.ultimo_pedido()[0]
        uid = self.uid()
        a = self.admin()
        self.post(a, f"/admin/orders/{oid}/status", {"status": "pago"}, "/admin/orders")
        r = self.excluir(a, uid)
        self.assertTrue(r.headers["Location"].endswith("/admin/clientes"))
        self.assertIsNone(self.uid())
        with app.app_context():
            o = db.session.get(Order, oid)
            self.assertEqual((o.user_id, o.status, o.customer_email), (None, "pago", "compras@ex.com"))
        self.assertEqual(c.get("/conta").status_code, 302, "a sessão da conta apagada deixa de valer")
        self.assertIn("Entre para ver o preço", c.get("/").get_data(as_text=True))

    def test_nao_exclui_com_pedido_em_aberto(self):
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "orcamento")
        a = self.admin()
        self.excluir(a, self.uid())
        self.assertIsNotNone(self.uid())
        self.assertIn("em aberto", a.get(f"/admin/clientes/{self.uid()}").get_data(as_text=True))

    def test_pode_se_cadastrar_de_novo_com_o_mesmo_email(self):
        self.cadastrar()
        self.excluir(self.admin(), self.uid())
        self.cadastrar()
        self.assertIsNotNone(self.uid())

    def test_admin_nao_e_excluido_e_cliente_nao_exclui(self):
        with app.app_context():
            admin_id = User.query.filter_by(is_admin=True).one().id
        self.assertEqual(self.excluir(self.admin(), admin_id).status_code, 404)
        c = self.cadastrar()
        self.excluir(c, self.uid())
        self.assertIsNotNone(self.uid())


if __name__ == "__main__":
    unittest.main()
