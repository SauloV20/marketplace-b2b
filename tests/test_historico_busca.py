"""Testes do histórico do pedido, do romaneio/etiqueta para imprimir e da busca com sugestões."""
import io
import unittest

from test_b2b import BaseB2B

from app import app  # noqa: E402
from models import Order, PedidoEvento, Product, db  # noqa: E402


class BaseHist(BaseB2B):
    def pedido(self, c=None, pagamento="boleto"):
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50, codigo="FRESA-10")
        c = c or self.cadastrar()
        self.adicionar(c, 1, 2)
        self.fechar(c, pagamento)
        with app.app_context():
            o = Order.query.order_by(Order.id.desc()).first()
            return c, o.id

    def eventos(self, oid):
        with app.app_context():
            return [(e.autor, e.texto) for e in PedidoEvento.query.filter_by(order_id=oid).order_by(PedidoEvento.id)]


class TesteHistorico(BaseHist):
    def test_linha_do_tempo_completa(self):
        c, oid = self.pedido()
        a = self.admin()
        self.post(c, f"/conta/pedidos/{oid}/alterar", {"qtd_1": "3"}, f"/conta/pedidos/{oid}")
        with app.app_context():
            item = db.session.get(Order, oid).items[0].id
        self.post(a, f"/admin/orders/{oid}/editar", {f"qtd_{item}": "4"}, f"/admin/orders/{oid}")
        self.post(a, f"/admin/orders/{oid}/status", {"status": "pago"}, "/admin/orders")
        self.post(a, f"/admin/orders/{oid}/envio", {"acao": "enviar", "transportadora": "Correios",
                                                     "rastreio": "BR123"}, f"/admin/orders/{oid}")
        a.post(f"/admin/orders/{oid}/arquivos", data={"_csrf": self.token(a, f"/admin/orders/{oid}"), "tipo": "nota",
                                                      "arquivo": (io.BytesIO(b"%PDF-1.4 x"), "nf.pdf")},
               content_type="multipart/form-data")
        self.post(a, f"/admin/orders/{oid}/envio", {"acao": "entregue"}, f"/admin/orders/{oid}")
        ev = self.eventos(oid)
        self.assertEqual([autor for autor, _ in ev], ["cliente", "loja", "loja", "loja", "loja", "loja"])
        textos = " | ".join(t for _, t in ev)
        for trecho in ("Alterado pelo cliente", "Alterado pela loja", "Pagamento confirmado",
                       "Enviado por Correios · rastreio BR123", "Anexou nota fiscal", "Entregue"):
            self.assertIn(trecho, textos)
        painel = a.get(f"/admin/orders/{oid}").get_data(as_text=True)
        self.assertIn("Histórico", painel)
        self.assertIn("Pedido feito pelo cliente", painel)
        conta = c.get(f"/conta/pedidos/{oid}").get_data(as_text=True)
        self.assertIn("Enviado por Correios", conta)
        self.assertIn("· Marketplace B2B", conta, "o cliente vê Marketplace B2B, não o nome de quem mexeu")

    def test_cancelamentos_e_orcamento(self):
        c, oid = self.pedido()
        self.post(c, f"/conta/pedidos/{oid}/cancelar", {}, f"/conta/pedidos/{oid}")
        self.assertEqual(self.eventos(oid), [("cliente", "Cancelado pelo cliente")])
        c2, orc = self.pedido(self.cadastrar("orc@teste.com"), pagamento="orcamento")
        a = self.admin()
        with app.app_context():
            item = db.session.get(Order, orc).items[0].id
        self.post(a, f"/admin/orders/{orc}/orcamento", {"acao": "aprovar", f"preco_{item}": "100,00",
                                                        "pagamento": "pix", "frete": "0"}, f"/admin/orders/{orc}/orcamento")
        self.assertIn("Orçamento aprovado: virou pedido (Pix, total R$ 200,00)", self.eventos(orc)[0][1])

    def test_excluir_pedido_leva_o_historico(self):
        c, oid = self.pedido()
        self.post(c, f"/conta/pedidos/{oid}/alterar", {"qtd_1": "3"}, f"/conta/pedidos/{oid}")
        self.post(self.admin(), "/admin/orders/excluir", {"oid": str(oid)}, "/admin/orders")
        self.assertEqual(self.eventos(oid), [])


class TesteImprimir(BaseHist):
    def test_romaneio_e_etiqueta(self):
        c, o1 = self.pedido()
        _, o2 = self.pedido(c)
        a = self.admin()
        html = a.get(f"/admin/orders/imprimir?oid={o1}&oid={o2}").get_data(as_text=True)
        self.assertIn(f"Romaneio de separação · Pedido #{o1}", html)
        self.assertIn(f"Pedido #{o2}", html)
        self.assertIn("FRESA-10", html)
        self.assertIn("Destinatário", html)
        self.assertIn("CEP 01001-000", html)
        self.assertIn("data-imprimir", html)
        self.assertIn("Imprimir romaneio e etiqueta", a.get(f"/admin/orders/{o1}").get_data(as_text=True))
        self.assertIn("Imprimir romaneio e etiqueta", a.get("/admin/orders").get_data(as_text=True))
        self.assertEqual(a.get("/admin/orders/imprimir").status_code, 302)
        self.assertEqual(self.cliente().get(f"/admin/orders/imprimir?oid={o1}").status_code, 302, "só o administrador")


class TesteSugestoes(BaseB2B):
    def sugestoes(self, q):
        return self.cliente().get("/busca/sugestoes", query_string={"q": q}).get_json()["itens"]

    def test_nome_codigo_sem_acento_e_sem_preco(self):
        self.ajustar_produto(2, codigo="ZX-900", name="Máscara de Solda Automática")
        nomes = [s["nome"] for s in self.sugestoes("mascara")]
        self.assertIn("Máscara de Solda Automática", nomes)
        r = self.sugestoes("zx-9")
        self.assertEqual(r[0]["codigo"], "ZX-900")
        self.assertNotIn("preco", r[0])
        self.assertTrue(r[0]["link"].startswith("/produto/2-"))
        self.assertEqual(self.sugestoes("a"), [], "pelo menos 2 letras")
        self.assertLessEqual(len(self.sugestoes("de")), 8)
        self.ajustar_produto(2, active=False)
        self.assertNotIn("Máscara de Solda Automática", [s["nome"] for s in self.sugestoes("mascara")])

    def test_header_tem_a_caixa(self):
        html = self.cliente().get("/").get_data(as_text=True)
        self.assertIn('data-busca="/busca/sugestoes"', html)
        self.assertIn("busca.js", html)


if __name__ == "__main__":
    unittest.main()
