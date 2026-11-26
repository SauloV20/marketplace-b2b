"""Testes da exclusão de pedidos pelo painel (um ou vários), devolvendo o estoque quando precisa."""
import io
import unittest

from test_b2b import BaseB2B

from app import app  # noqa: E402
from models import Order, OrderItem, PedidoArquivo, db  # noqa: E402


class TesteExcluirPedido(BaseB2B):
    def novo_pedido(self, c, pid=1, qtd=2, pagamento="pix"):
        self.adicionar(c, pid, qtd)
        self.fechar(c, pagamento)
        with app.app_context():
            return Order.query.order_by(Order.id.desc()).first().id

    def excluir(self, a, *oids, filtro=""):
        return self.post(a, "/admin/orders/excluir", {"oid": [str(o) for o in oids], "filtro": filtro}, "/admin/orders")

    def existe(self, oid):
        with app.app_context():
            return db.session.get(Order, oid) is not None

    def test_excluir_pedido_em_aberto_devolve_estoque(self):
        self.ajustar_produto(1, qtd_minima=1, multiplo=1)
        antes = self.estoque(1)
        c = self.cadastrar()
        oid = self.novo_pedido(c)
        self.assertEqual(self.estoque(1), antes - 2)
        a = self.admin()
        r = self.excluir(a, oid)
        self.assertIn("/admin/orders", r.headers["Location"])
        self.assertFalse(self.existe(oid))
        self.assertEqual(self.estoque(1), antes)
        with app.app_context():
            self.assertEqual(OrderItem.query.filter_by(order_id=oid).count(), 0)
        self.assertIn(f"Pedido excluído: #{oid}", a.get("/admin/orders").get_data(as_text=True))
        self.assertEqual(c.get(f"/conta/pedidos/{oid}").status_code, 404)

    def test_cancelado_e_orcamento_nao_mexem_no_estoque(self):
        self.ajustar_produto(1, qtd_minima=1, multiplo=1)
        antes = self.estoque(1)
        c = self.cadastrar()
        cancelado = self.novo_pedido(c)
        a = self.admin()
        self.post(a, f"/admin/orders/{cancelado}/status", {"status": "cancelado"}, "/admin/orders")
        orcamento = self.novo_pedido(c, pagamento="orcamento")
        self.assertEqual(self.estoque(1), antes)
        self.excluir(a, cancelado, orcamento)
        self.assertEqual(self.estoque(1), antes, "não devolve de novo o que já tinha voltado")
        self.assertFalse(self.existe(cancelado) or self.existe(orcamento))

    def test_varios_de_uma_vez_e_documentos_somem(self):
        c = self.cadastrar()
        o1, o2 = self.novo_pedido(c), self.novo_pedido(c)
        a = self.admin()
        a.post(f"/admin/orders/{o1}/arquivos", data={"_csrf": self.token(a, f"/admin/orders/{o1}"), "tipo": "nota",
                                                     "arquivo": (io.BytesIO(b"%PDF-1.4 x"), "nota.pdf")},
               content_type="multipart/form-data")
        r = self.excluir(a, o1, o2, 999999, filtro="pendente")
        self.assertIn("status=pendente", r.headers["Location"])
        self.assertFalse(self.existe(o1) or self.existe(o2))
        with app.app_context():
            self.assertEqual(PedidoArquivo.query.count(), 0)
        self.assertIn("2 pedidos excluídos", a.get("/admin/orders").get_data(as_text=True))

    def test_nada_marcado_e_sem_login(self):
        c = self.cadastrar()
        oid = self.novo_pedido(c)
        a = self.admin()
        self.excluir(a)
        self.assertIn("Marque os pedidos", a.get("/admin/orders").get_data(as_text=True))
        r = self.post(self.cliente(), "/admin/orders/excluir", {"oid": str(oid)}, "/")
        self.assertNotEqual(r.status_code, 200)
        self.post(c, "/admin/orders/excluir", {"oid": str(oid)}, "/conta")
        self.assertTrue(self.existe(oid), "cliente comum não exclui pedido")

    def test_botoes_no_painel(self):
        c = self.cadastrar()
        oid = self.novo_pedido(c)
        a = self.admin()
        self.assertIn('form="excluir-pedidos"', a.get("/admin/orders").get_data(as_text=True))
        self.assertIn(f"Excluir pedido #{oid}", a.get(f"/admin/orders/{oid}").get_data(as_text=True))
        orc = self.novo_pedido(c, pagamento="orcamento")
        self.assertIn("Excluir este orçamento", a.get(f"/admin/orders/{orc}/orcamento").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
