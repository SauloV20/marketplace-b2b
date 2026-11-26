"""Devolução e troca: pedido do cliente, aprovação, estoque de volta, reembolso no Financeiro e estorno no cartão."""
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from test_b2b import BaseB2B

from app import app  # noqa: E402
import boleto_efi  # noqa: E402
from models import Devolucao, Lancamento, Order, Product, agora_utc, db  # noqa: E402


class TesteDevolucoes(BaseB2B):
    def setUp(self):
        super().setUp()
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)
        self.ajustar_produto(2, qtd_minima=1, multiplo=1, stock=50)
        self.a = self.admin()

    def pedido_entregue(self, metodo="pix"):
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 4)
        self.adicionar(c, 2, 1)
        self.fechar(c, metodo)
        oid = self.ultimo_pedido()[0]
        if metodo != "cartao":
            self.post(self.a, f"/admin/orders/{oid}/status", {"status": "pago"}, "/admin/orders")
        self.post(self.a, f"/admin/orders/{oid}/envio", {"acao": "enviar", "transportadora": "Correios"},
                  f"/admin/orders/{oid}")
        self.post(self.a, f"/admin/orders/{oid}/envio", {"acao": "entregue"}, f"/admin/orders/{oid}")
        return oid, c

    def item(self, oid, pid):
        with app.app_context():
            return next(i.id for i in db.session.get(Order, oid).items if i.product_id == pid)

    def pedir(self, c, oid, qtds, tipo="devolucao", motivo="Veio com defeito"):
        dados = {"tipo": tipo, "motivo": motivo, **{f"qtd_{self.item(oid, pid)}": q for pid, q in qtds.items()}}
        return self.post(c, f"/conta/pedidos/{oid}/devolucao", dados, f"/conta/pedidos/{oid}")

    def devolucao(self):
        with app.app_context():
            d = Devolucao.query.order_by(Devolucao.id.desc()).first()
            return d.id, d.status, d.valor, d.estoque_devolvido

    def estoque(self, pid):
        with app.app_context():
            return db.session.get(Product, pid).stock

    def acao(self, did, **dados):
        return self.post(self.a, f"/admin/devolucoes/{did}", dados, f"/admin/devolucoes/{did}")

    def test_fluxo_completo_com_reembolso_por_pix(self):
        oid, c = self.pedido_entregue()
        pagina = c.get(f"/conta/pedidos/{oid}").get_data(as_text=True)
        self.assertIn(f"/conta/pedidos/{oid}/devolucao", pagina)
        self.emails.clear()
        self.pedir(c, oid, {1: 2})
        did, status, _, _ = self.devolucao()
        self.assertEqual(status, "solicitada")
        self.assertTrue(any("devolução com reembolso pedida" in e["assunto"].lower() for e in self.emails),
                        "a loja recebe o aviso")
        self.pedir(c, oid, {2: 1})
        with app.app_context():
            self.assertEqual(Devolucao.query.count(), 1, "um pedido de devolução em andamento por vez")
        self.acao(did, acao="aprovar", resposta="Envie para Rua X, 100")
        self.assertTrue(any("Rua X, 100" in e["texto"] for e in self.emails))
        antes = self.estoque(1)
        self.acao(did, acao="receber", estoque="on")
        self.assertEqual(self.estoque(1), antes + 2)
        self.acao(did, acao="concluir", valor="379,80", forma="pix")
        _, status, valor, voltou = self.devolucao()
        self.assertEqual((status, valor, voltou), ("concluida", Decimal("379.80"), True))
        with app.app_context():
            l = Lancamento.query.filter_by(categoria="Devolução a cliente").one()
            self.assertEqual((l.tipo, l.valor, l.forma), ("pagar", Decimal("379.80"), "pix"))
            self.assertIsNotNone(l.pago_em)
            self.assertEqual(db.session.get(Order, oid).status, "pago", "o pedido continua pago")
        self.assertIn("Reembolso: R$ 379,80", c.get(f"/conta/pedidos/{oid}").get_data(as_text=True))
        self.assertIn(f"/admin/devolucoes/{did}", self.a.get(f"/admin/orders/{oid}").get_data(as_text=True))
        self.assertIn("Concluída", self.a.get("/admin/devolucoes?status=todas").get_data(as_text=True))
        self.assertIn("R$ 379,80", self.a.get(f"/admin/devolucoes/{did}").get_data(as_text=True))

    def test_regras_do_pedido(self):
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 2)
        self.fechar(c, "pix")
        oid = self.ultimo_pedido()[0]
        self.assertNotIn(f"/conta/pedidos/{oid}/devolucao", c.get(f"/conta/pedidos/{oid}").get_data(as_text=True),
                         "antes do envio não pode")
        self.pedir(c, oid, {1: 1})
        with app.app_context():
            self.assertEqual(Devolucao.query.count(), 0)

    def test_nao_devolve_mais_que_comprou_e_prazo(self):
        oid, c = self.pedido_entregue()
        self.pedir(c, oid, {1: 5})
        self.pedir(c, oid, {1: 0, 2: 0})
        self.pedir(c, oid, {1: 1}, motivo="")
        with app.app_context():
            self.assertEqual(Devolucao.query.count(), 0)
            o = db.session.get(Order, oid)
            o.entregue_em = agora_utc() - timedelta(days=31)
            db.session.commit()
        self.assertIn("prazo", c.get(f"/conta/pedidos/{oid}").get_data(as_text=True).lower())
        self.pedir(c, oid, {1: 1})
        with app.app_context():
            self.assertEqual(Devolucao.query.count(), 0)

    def test_recusa_libera_novo_pedido_e_troca(self):
        oid, c = self.pedido_entregue()
        self.pedir(c, oid, {1: 4})
        did = self.devolucao()[0]
        self.acao(did, acao="recusar", resposta="")
        self.assertEqual(self.devolucao()[1], "solicitada", "recusa exige o motivo")
        self.acao(did, acao="recusar", resposta="Produto usado")
        self.assertEqual(self.devolucao()[1], "recusada")
        self.pedir(c, oid, {1: 4}, tipo="troca")
        did2, status, _, _ = self.devolucao()
        self.assertNotEqual(did, did2)
        self.acao(did2, acao="aprovar", resposta="")
        self.acao(did2, acao="receber")  # sem voltar ao estoque (avariado)
        antes = self.estoque(1)
        self.acao(did2, acao="concluir")
        self.assertEqual(self.devolucao()[1:], ("concluida", None, False))
        self.assertEqual(self.estoque(1), antes)
        pagina = c.get(f"/conta/pedidos/{oid}").get_data(as_text=True)
        formulario = pagina.split(f"/conta/pedidos/{oid}/devolucao")[1].split("</form>")[0]
        self.assertIn(f'name="qtd_{self.item(oid, 2)}"', formulario, "o item 2 ainda pode")
        self.assertNotIn(f'name="qtd_{self.item(oid, 1)}"', formulario, "o item 1 já foi todo trocado")

    def test_reembolso_nao_passa_do_total(self):
        oid, c = self.pedido_entregue()
        self.pedir(c, oid, {1: 1})
        did = self.devolucao()[0]
        self.acao(did, acao="aprovar", resposta="")
        self.acao(did, acao="concluir", valor="99999", forma="pix")
        self.assertEqual(self.devolucao()[1], "aprovada")

    def test_outro_cliente_nao_pede(self):
        oid, _ = self.pedido_entregue()
        outro = self.cadastrar("outro@teste.com")
        self.assertEqual(self.pedir(outro, oid, {1: 1}).status_code, 404)

    def test_estorno_no_cartao_pela_efi(self):
        app.config.update(EFI_CLIENT_ID="id", EFI_CLIENT_SECRET="segredo")
        self.addCleanup(app.config.update, EFI_CLIENT_ID="", EFI_CLIENT_SECRET="")
        chamadas = []

        def efi(metodo, caminho, corpo=None):
            chamadas.append((metodo, caminho, corpo))
            if caminho == "/v1/charge/one-step/link":
                return {"data": {"charge_id": 777, "status": "link", "payment_url": "https://pagamento.efi/777"}}
            if caminho.startswith("/v1/charge/777") and metodo == "GET":
                return {"data": {"status": "paid"}}
            return {"code": 200, "data": "Reembolso em processamento"}
        trava = mock.patch.object(boleto_efi, "_chamar", side_effect=efi)
        trava.start()
        self.addCleanup(trava.stop)
        self.post(self.a, "/admin/financeiro", {"acao": "boletos_config", "boleto_multa": "2", "boleto_juros": "1",
                                                "cartao_efi": "on"}, "/admin/financeiro")
        oid, c = self.pedido_entregue("cartao")
        self.post(self.a, f"/admin/pedidos/{oid}/cartao", {"acao": "consultar"}, "/admin/orders")
        self.assertEqual(self.ultimo_pedido()[1], "pago")
        self.pedir(c, oid, {2: 1})
        did = self.devolucao()[0]
        self.acao(did, acao="aprovar", resposta="")
        self.acao(did, acao="concluir", valor="10,50", forma="cartao")
        self.assertIn(("POST", "/v1/charge/card/777/refund", {"amount": 1050}), chamadas)
        self.assertEqual(self.devolucao()[1:3], ("concluida", Decimal("10.50")))


if __name__ == "__main__":
    unittest.main()
