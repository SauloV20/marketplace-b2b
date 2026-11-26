"""Rastreio automático das etiquetas do Melhor Envio: tarefa agendada e webhook, com a API simulada."""
import base64
import hashlib
import hmac
import json
import unittest
from datetime import timedelta

from test_etiquetas import BaseEtiqueta

from app import app  # noqa: E402
from models import EtiquetaEnvio, Order, PedidoEvento, agora_utc, db  # noqa: E402

TOKEN = "token-das-tarefas"
SEGREDO = "segredo-do-app"


class BaseRastreio(BaseEtiqueta):
    def setUp(self):
        super().setUp()
        app.config.update(TAREFAS_TOKEN=TOKEN, MELHOR_ENVIO_SECRET=SEGREDO)
        self.oid = self.pedido_com_sedex()
        self.comprar(self.oid)
        with app.app_context():
            self.me_id = EtiquetaEnvio.query.filter_by(order_id=self.oid).one().me_id

    def tearDown(self):
        app.config.update(TAREFAS_TOKEN="", MELHOR_ENVIO_SECRET="")
        super().tearDown()

    def tarefa(self, token=TOKEN):
        return self.cliente().post("/tarefas/rastreio", headers={"Authorization": f"Bearer {token}"})

    def webhook(self, dados, segredo=SEGREDO, evento="order.posted"):
        corpo = json.dumps({"event": evento, "data": dados}).encode()
        assinatura = base64.b64encode(hmac.new(segredo.encode(), corpo, hashlib.sha256).digest()).decode()
        return self.cliente().post("/webhooks/melhor-envio", data=corpo, content_type="application/json",
                                   headers={"X-ME-Signature": assinatura})

    def pedido(self):
        with app.app_context():
            o = db.session.get(Order, self.oid)
            return o.envio, o.rastreio, [e.situacao for e in o.etiquetas]


class TesteTarefa(BaseRastreio):
    def test_postada_e_entregue_pela_tarefa(self):
        emails = len(self.emails)
        r = self.tarefa()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["pedidos_atualizados"], 1)
        self.assertEqual(self.pedido(), ("enviado", "ME123456BR", ["postada"]))
        self.assertEqual(len(self.emails), emails + 1)
        self.assertIn("ME123456BR", self.emails[-1]["texto"])
        self.tarefa()  # nada mudou: não manda outro e-mail
        self.assertEqual(len(self.emails), emails + 1)
        self.rastreio = {"status": "delivered", "tracking": "ME123456BR"}
        self.tarefa()
        self.assertEqual(self.pedido()[0], "entregue")
        self.assertEqual(self.pedido()[2], ["entregue"])
        self.chamadas.clear()
        self.tarefa()  # entregue não é mais consultada
        self.assertNotIn("/api/v2/me/shipment/tracking", self.caminhos())
        html = self.a.get(f"/admin/orders/{self.oid}").get_data(as_text=True)
        self.assertIn("última vez:", html)

    def test_autorizacao(self):
        self.assertEqual(self.tarefa("errado").status_code, 403)
        app.config.update(TAREFAS_TOKEN="")
        self.assertEqual(self.tarefa("").status_code, 404)

    def test_nao_volta_etapa(self):
        self.tarefa()
        self.rastreio = {"status": "released", "tracking": "ME123456BR"}  # aviso atrasado
        self.tarefa()
        self.assertEqual(self.pedido()[2], ["postada"])

    def test_entrega_interrompida_avisa(self):
        self.rastreio = {"status": "paused", "tracking": "ME123456BR"}
        self.tarefa()
        with app.app_context():
            e = EtiquetaEnvio.query.filter_by(order_id=self.oid).one()
            self.assertEqual(e.situacao, "postada")
            self.assertIn("destinatário precisa agir", e.erro)
            self.assertTrue(PedidoEvento.query.filter(PedidoEvento.order_id == self.oid,
                                                      PedidoEvento.texto.contains("interrompida")).first())
        self.assertIn("destinatário precisa agir", self.a.get(f"/admin/orders/{self.oid}").get_data(as_text=True))
        self.rastreio = {"status": "delivered", "tracking": "ME123456BR"}
        self.tarefa()
        with app.app_context():
            self.assertEqual(EtiquetaEnvio.query.filter_by(order_id=self.oid).one().erro, "")

    def test_etiqueta_antiga_fica_de_fora(self):
        with app.app_context():
            e = EtiquetaEnvio.query.filter_by(order_id=self.oid).one()
            e.criado_em = agora_utc() - timedelta(days=120)
            db.session.commit()
        self.chamadas.clear()
        self.assertEqual(self.tarefa().get_json()["etiquetas"], 0)
        self.assertNotIn("/api/v2/me/shipment/tracking", self.caminhos())

    def test_api_fora_do_ar(self):
        self.recusar["/api/v2/me/shipment/tracking"] = "fora do ar"
        r = self.tarefa()
        self.assertEqual(r.status_code, 200)
        self.assertIn("fora do ar", r.get_json()["erro"])
        self.assertEqual(self.pedido()[0], "")


class TesteWebhook(BaseRastreio):
    def test_aviso_de_postagem(self):
        r = self.webhook({"id": self.me_id, "status": "posted", "tracking": "ME999BR"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.pedido(), ("enviado", "ME999BR", ["postada"]))
        self.webhook({"id": self.me_id, "status": "delivered", "tracking": "ME999BR"}, evento="order.delivered")
        self.assertEqual(self.pedido()[0], "entregue")

    def test_assinatura_errada_e_recusada(self):
        r = self.webhook({"id": self.me_id, "status": "posted"}, segredo="outro")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.pedido()[0], "")
        r = self.cliente().post("/webhooks/melhor-envio", data=b"{}", content_type="application/json")
        self.assertEqual(r.status_code, 401)

    def test_etiqueta_desconhecida_e_corpo_invalido(self):
        self.assertEqual(self.webhook({"id": "nao-e-nossa", "status": "posted"}).status_code, 200)
        corpo = b"isto nao e json"
        assinatura = base64.b64encode(hmac.new(SEGREDO.encode(), corpo, hashlib.sha256).digest()).decode()
        r = self.cliente().post("/webhooks/melhor-envio", data=corpo, headers={"X-ME-Signature": assinatura})
        self.assertEqual(r.status_code, 400)

    def test_sem_secret_nao_existe(self):
        app.config.update(MELHOR_ENVIO_SECRET="")
        self.assertEqual(self.webhook({"id": self.me_id, "status": "posted"}).status_code, 404)

    def test_assinatura_em_hexadecimal(self):
        corpo = json.dumps({"event": "order.posted", "data": {"id": self.me_id, "status": "posted"}}).encode()
        assinatura = hmac.new(SEGREDO.encode(), corpo, hashlib.sha256).hexdigest()
        r = self.cliente().post("/webhooks/melhor-envio", data=corpo, headers={"X-ME-Signature": assinatura})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.pedido()[0], "enviado")


if __name__ == "__main__":
    unittest.main()
