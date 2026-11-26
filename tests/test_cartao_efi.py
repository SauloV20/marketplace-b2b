"""Cartão de crédito pela Efí (link de pagamento): criação, aviso de pago, recusa, novo link e cancelamento."""
import unittest
from unittest import mock

from test_b2b import BaseB2B

from app import app  # noqa: E402
import boleto_efi  # noqa: E402
from models import Lancamento, Order, PedidoEvento, db  # noqa: E402


class EfiCartaoFalsa:
    """Imita a API de Cobranças da Efí: link de pagamento, consulta, cancelamento e notificações (histórico inteiro)."""
    def __init__(self):
        self.links, self.situacao, self.canceladas, self.historico = {}, {}, [], []
        self.proximo = 900

    def __call__(self, metodo, caminho, corpo=None):
        if caminho == "/v1/charge/one-step/link":
            self.proximo += 1
            cid = self.proximo
            self.links[cid], self.situacao[cid] = corpo, "link"
            return {"code": 200, "data": {"charge_id": cid, "status": "link", "total": corpo["items"][0]["value"],
                                          "payment_url": f"https://pagamento.sejaefi.com.br/{cid}"}}
        if caminho.startswith("/v1/notification/"):
            return {"data": list(self.historico)}
        if caminho.endswith("/cancel"):
            self.canceladas.append(int(caminho.split("/")[3]))
            return {"code": 200}
        if caminho.startswith("/v1/charge/"):
            cid = int(caminho.split("/")[3])
            return {"data": {"status": self.situacao[cid], "total": self.links[cid]["items"][0]["value"]}}
        raise AssertionError(caminho)

    def evento(self, cid, situacao):
        self.situacao[cid] = situacao
        self.historico.append({"id": len(self.historico) + 1, "identifiers": {"charge_id": cid},
                               "status": {"current": situacao}, "value": self.links[cid]["items"][0]["value"],
                               "received_by_bank_at": "2026-11-24"})


class TesteCartaoEfi(BaseB2B):
    def setUp(self):
        super().setUp()
        app.config.update(EFI_CLIENT_ID="id", EFI_CLIENT_SECRET="segredo")
        self.addCleanup(app.config.update, EFI_CLIENT_ID="", EFI_CLIENT_SECRET="")
        self.efi = EfiCartaoFalsa()
        trava = mock.patch.object(boleto_efi, "_chamar", side_effect=self.efi)
        trava.start()
        self.addCleanup(trava.stop)
        self.a = self.admin()
        self.ligar()

    def ligar(self, ativo=True):
        dados = {"acao": "boletos_config", "boleto_multa": "2", "boleto_juros": "1"}
        if ativo:
            dados["cartao_efi"] = "on"
        self.post(self.a, "/admin/financeiro", dados, "/admin/financeiro")

    def pedido_no_cartao(self, email="cli@teste.com"):
        c = self.cadastrar(email)
        self.adicionar(c, 1, 10)
        self.fechar(c, "cartao")
        return self.ultimo_pedido()[0], c

    def pedido(self, oid):
        with app.app_context():
            o = db.session.get(Order, oid)
            return o.status, o.cartao_id, o.cartao_situacao, o.total

    def avisar(self):
        return self.cliente().post("/efi/boleto", data={"notification": "tok-1"})

    def test_cria_link_no_valor_total_e_mostra_o_botao(self):
        oid, c = self.pedido_no_cartao()
        status, cid, situacao, total = self.pedido(oid)
        self.assertEqual((status, situacao), ("pendente", "link"))
        corpo = self.efi.links[int(cid)]
        self.assertEqual(corpo["items"][0]["value"], int(total * 100))
        self.assertEqual(corpo["settings"]["payment_method"], "credit_card")
        self.assertTrue(corpo["metadata"]["notification_url"].endswith("/efi/boleto"))
        pagina = c.get(f"/pedido/{oid}").get_data(as_text=True)
        self.assertIn("Pagar com cartão", pagina)
        self.assertIn(f"https://pagamento.sejaefi.com.br/{cid}", pagina)
        self.assertNotIn("Alterar pedido", pagina, "o link já tem o valor: o pedido não muda mais")

    def test_aviso_de_pago_quita_o_pedido_e_nao_volta_atras(self):
        oid, _ = self.pedido_no_cartao()
        cid = int(self.pedido(oid)[1])
        self.emails.clear()
        self.efi.evento(cid, "waiting")
        self.efi.evento(cid, "paid")
        self.assertEqual(self.avisar().status_code, 200)
        self.assertEqual(self.pedido(oid)[:3:2], ("pago", "paid"))
        with app.app_context():
            ls = Lancamento.query.filter_by(order_id=oid).all()
            self.assertTrue(ls and all(l.pago_em and l.forma == "cartao" for l in ls))
        self.assertTrue(self.emails, "o cliente recebe o e-mail de pagamento confirmado")
        self.efi.historico.insert(0, dict(self.efi.historico[0], id=0))  # aviso antigo repetido e fora de ordem
        self.avisar()
        self.assertEqual(self.pedido(oid)[:3:2], ("pago", "paid"))

    def test_recusado_deixa_gerar_outro_link(self):
        oid, c = self.pedido_no_cartao()
        cid = int(self.pedido(oid)[1])
        self.post(c, f"/pedido/{oid}/cartao", {}, f"/pedido/{oid}")
        self.assertEqual(len(self.efi.links), 1, "com o link valendo, não cria outro")
        self.efi.evento(cid, "unpaid")
        self.avisar()
        self.assertEqual(self.pedido(oid)[:3], ("pendente", str(cid), "unpaid"))
        pagina = c.get(f"/pedido/{oid}").get_data(as_text=True)
        self.assertIn("Cartão recusado", pagina)
        self.assertIn("Gerar novo link de pagamento", pagina)
        self.post(c, f"/pedido/{oid}/cartao", {}, f"/pedido/{oid}")
        self.assertEqual(len(self.efi.links), 2)
        self.assertNotEqual(self.pedido(oid)[1], str(cid))

    def test_outro_cliente_nao_gera_link(self):
        oid, _ = self.pedido_no_cartao()
        outro = self.cadastrar("outro@teste.com")
        self.assertEqual(self.post(outro, f"/pedido/{oid}/cartao", {}).status_code,
                         404)

    def test_cancelar_pedido_cancela_o_link(self):
        oid, _ = self.pedido_no_cartao()
        cid = int(self.pedido(oid)[1])
        self.post(self.a, f"/admin/orders/{oid}/status", {"status": "cancelado"}, "/admin/orders")
        self.assertEqual(self.efi.canceladas, [cid])
        self.assertEqual(self.pedido(oid)[:3:2], ("cancelado", "canceled"))

    def test_painel_consulta_quando_o_aviso_nao_chega(self):
        oid, _ = self.pedido_no_cartao()
        self.efi.situacao[int(self.pedido(oid)[1])] = "paid"
        self.post(self.a, "/admin/financeiro", {"acao": "boletos_atualizar"}, "/admin/financeiro")
        self.assertEqual(self.pedido(oid)[0], "pago")

    def test_pago_depois_de_cancelado_avisa_a_loja(self):
        oid, _ = self.pedido_no_cartao()
        cid = int(self.pedido(oid)[1])
        with app.app_context():  # cancelado sem conseguir cancelar o link na Efí
            o = db.session.get(Order, oid)
            o.status = "cancelado"
            db.session.commit()
        self.efi.evento(cid, "paid")
        self.avisar()
        with app.app_context():
            textos = [e.texto for e in PedidoEvento.query.filter_by(order_id=oid)]
        self.assertTrue(any("devolva o valor" in t for t in textos))

    def test_desligado_nao_cria_link(self):
        self.ligar(False)
        oid, c = self.pedido_no_cartao()
        self.assertEqual(self.efi.links, {})
        self.assertIn("Enviaremos um link seguro", c.get(f"/pedido/{oid}").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
