"""Testes do Pix pela Efí (com conferência do pagador) e da confirmação do celular por SMS (Twilio).
A Efí e a Twilio são simuladas: nenhum teste acessa a internet nem envia SMS."""
import io
import json
import unittest
from decimal import Decimal
from unittest import mock

from test_site import BaseTeste

from app import app  # noqa: E402
from models import Order, User, db  # noqa: E402
import efi  # noqa: E402
import verificacao  # noqa: E402

DADOS = {"name": "Cliente Teste", "email": "cli@teste.com", "phone": "11999", "address": "Rua A, 1", "cep": "01001-000", "payment": "pix"}
TWILIO = dict(TWILIO_ACCOUNT_SID="AC123", TWILIO_AUTH_TOKEN="tok", TWILIO_VERIFY_SID="VA123")
EFI = dict(EFI_CLIENT_ID="id", EFI_CLIENT_SECRET="segredo", EFI_CERTIFICADO="/tmp/efi.pem", EFI_CHAVE_PIX="chave@ex.com")


def ligar(teste, cfg):
    antes = {k: app.config.get(k) for k in cfg}
    app.config.update(cfg)
    teste.addCleanup(app.config.update, antes)


class TesteRegras(unittest.TestCase):
    def test_normalizar_celular(self):
        self.assertEqual(verificacao.normalizar_celular("(11) 98765-4321"), "+5511987654321")
        self.assertEqual(verificacao.normalizar_celular("+55 11 98765 4321"), "+5511987654321")
        self.assertEqual(verificacao.normalizar_celular("011987654321"), "+5511987654321")
        for ruim in ("1187654321", "(11) 8765-4321", "123", ""):
            self.assertIsNone(verificacao.normalizar_celular(ruim), ruim)

    def test_pagador_confere(self):
        cpf = "52998224725"
        self.assertTrue(efi.pagador_confere({"documento": "***.982.247-**", "nome": "X"}, cpf, []))
        self.assertFalse(efi.pagador_confere({"documento": "***.111.222-**", "nome": "Ana"}, cpf, ["Ana"]))
        self.assertTrue(efi.pagador_confere({"documento": "11.222.333/0001-81"}, "11222333000181", []))
        self.assertFalse(efi.pagador_confere({"documento": "***.982.247-**"}, "11222333000181", []))
        # sem documento: o nome decide (sem acento, sem "LTDA", maiúsculas)
        self.assertTrue(efi.pagador_confere({"nome": "METALURGICA SAO JOAO"}, "", ["Metalúrgica São João Ltda"]))
        self.assertFalse(efi.pagador_confere({"nome": "OUTRA PESSOA"}, "", ["Ana Souza"]))
        self.assertFalse(efi.pagador_confere(None, cpf, ["Ana"]))


class TesteCelular(BaseTeste):
    def setUp(self):
        super().setUp()
        ligar(self, TWILIO)
        self.twilio = mock.patch("verificacao._chamar").start()
        self.addCleanup(mock.patch.stopall)

    def test_sem_twilio_configurada_nada_muda(self):
        app.config["TWILIO_VERIFY_SID"] = ""
        c = self.cadastrar()
        self.assertEqual(c.get("/").status_code, 200)
        self.assertEqual(c.get("/conta/celular").status_code, 302)

    def test_cadastro_exige_celular_antes_de_usar_a_conta(self):
        c = self.cadastrar()
        self.assertIn("/conta/celular", c.get("/").headers["Location"])
        self.assertIn("/conta/celular", c.get("/conta").headers["Location"])
        r = self.post(c, "/carrinho/add", {"pid": 1, "qty": 1}, "/conta/celular")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.cliente().get("/").status_code, 200, "visitante continua vendo a vitrine")

        self.twilio.return_value = {"status": "pending"}
        self.post(c, "/conta/celular", {"acao": "enviar", "celular": "(11) 98765-4321"}, "/conta/celular")
        self.assertEqual(self.twilio.call_args.args, ("Verifications", {"To": "+5511987654321", "Channel": "sms",
                                                                         "Locale": "pt-BR"}))
        self.post(c, "/conta/celular", {"acao": "conferir", "codigo": "000000"}, "/conta/celular")
        self.assertIn("Código incorreto", c.get("/conta/celular").get_data(as_text=True))
        self.twilio.return_value = {"status": "approved"}
        r = self.post(c, "/conta/celular", {"acao": "conferir", "codigo": "123456"}, "/conta/celular")
        self.assertTrue(r.headers["Location"].endswith("/conta"))
        self.assertEqual(c.get("/").status_code, 200)
        with app.app_context():
            u = User.query.filter_by(email="cli@teste.com").one()
            self.assertEqual((u.phone, u.telefone_verificado), ("+5511987654321", True))

    def test_codigo_vencido_pede_outro(self):
        c = self.cadastrar()
        self.twilio.return_value = {"status": "pending"}
        self.post(c, "/conta/celular", {"acao": "enviar", "celular": "11987654321"}, "/conta/celular")
        self.twilio.side_effect = __import__("urllib.error").error.HTTPError("u", 404, "x", {}, io.BytesIO(b""))
        self.post(c, "/conta/celular", {"acao": "conferir", "codigo": "123456"}, "/conta/celular")
        html = c.get("/conta/celular").get_data(as_text=True)
        self.assertIn("venceu", html)
        self.assertNotIn('name="codigo"', html)

    def test_limite_de_sms(self):
        c = self.cadastrar()
        self.twilio.return_value = {"status": "pending"}
        self.post(c, "/conta/celular", {"acao": "enviar", "celular": "11987654321"}, "/conta/celular")
        self.post(c, "/conta/celular", {"acao": "enviar", "celular": "11987654321"}, "/conta/celular")
        self.assertEqual(self.twilio.call_count, 1, "o segundo pedido em menos de 1 minuto não envia SMS")
        self.assertIn("Aguarde um minuto", c.get("/conta/celular").get_data(as_text=True))

    def test_celular_invalido_nao_gasta_sms(self):
        c = self.cadastrar()
        self.post(c, "/conta/celular", {"acao": "enviar", "celular": "1234"}, "/conta/celular")
        self.assertEqual(self.twilio.call_count, 0)

    def test_meus_dados_nao_troca_celular_sem_codigo(self):
        c = self.cadastrar()
        with app.app_context():
            u = User.query.filter_by(email="cli@teste.com").one()
            u.phone, u.telefone_verificado = "+5511987654321", True
            db.session.commit()
        self.post(c, "/conta/dados", {"nome": "Cli", "telefone": "21999999999"}, "/conta/dados")
        with app.app_context():
            self.assertEqual(User.query.filter_by(email="cli@teste.com").one().phone, "+5511987654321")


class TestePixEfi(BaseTeste):
    def setUp(self):
        super().setUp()
        ligar(self, EFI)
        self.criar = mock.patch("efi.criar_cobranca", return_value=("Marketplace B2B" + "0" * 25, "00020126PIXEFI")).start()
        self.consultar = mock.patch("efi.consultar_cobranca", return_value=(False, None)).start()
        self.addCleanup(mock.patch.stopall)
        self.c = self.cadastrar()
        with app.app_context():
            u = User.query.filter_by(email="cli@teste.com").one()
            self.doc = u.documento
        self.post(self.c, "/carrinho/add", {"pid": 1, "qty": 1})
        self.oid = int(self.post(self.c, "/checkout", DADOS).headers["Location"].rsplit("/", 1)[1])

    def pedido(self):
        with app.app_context():
            return db.session.get(Order, self.oid)

    def test_cria_cobranca_e_confirma_sozinho_quando_paga(self):
        html = self.c.get(f"/pedido/{self.oid}").get_data(as_text=True)
        self.assertIn("00020126PIXEFI", html)
        self.assertIn("confirma sozinha", html)
        self.c.get(f"/pedido/{self.oid}")
        self.assertEqual(self.criar.call_count, 1, "a cobrança é criada uma vez só")
        self.assertEqual(self.c.get(f"/pedido/{self.oid}/pix").get_json(), {"pago": False})
        mascarado = f"***.{self.doc[3:6]}.{self.doc[6:9]}-**"
        self.consultar.return_value = (True, {"nome": "CLIENTE TESTE", "documento": mascarado})
        with mock.patch("blueprints.shop._ultima_consulta", {}):
            self.assertEqual(self.c.get(f"/pedido/{self.oid}/pix").get_json(), {"pago": True})
        o = self.pedido()
        self.assertEqual((o.status, o.pagador_confere, o.pagador_documento), ("pago", True, mascarado))
        self.assertIn("Pagamento confirmado", self.c.get(f"/pedido/{self.oid}").get_data(as_text=True))

    def test_pagador_diferente_fica_marcado_para_o_admin(self):
        self.c.get(f"/pedido/{self.oid}")
        self.consultar.return_value = (True, {"nome": "FULANO DE TAL", "documento": "***.000.111-**"})
        a = self.admin()
        self.post(a, f"/admin/orders/{self.oid}/pix", {}, "/admin/orders")
        o = self.pedido()
        self.assertEqual((o.status, o.pagador_confere), ("pago", False))
        html = a.get("/admin/orders").get_data(as_text=True)
        self.assertIn("pagador diferente", html)
        self.assertIn("FULANO DE TAL", html)

    def test_consulta_limitada_a_cada_5_segundos(self):
        self.c.get(f"/pedido/{self.oid}")
        for _ in range(3):
            self.c.get(f"/pedido/{self.oid}/pix")
        self.assertEqual(self.consultar.call_count, 1)

    def test_efi_fora_do_ar_nao_quebra_a_pagina(self):
        self.criar.side_effect = efi.ErroEfi("fora do ar")
        self.assertEqual(self.c.get(f"/pedido/{self.oid}").status_code, 200)
        self.assertEqual(self.pedido().pix_txid, "")
        self.consultar.side_effect = efi.ErroEfi("fora do ar")
        self.assertEqual(self.c.get(f"/pedido/{self.oid}/pix").get_json(), {"pago": False})

    def test_outra_pessoa_nao_consulta_o_pix(self):
        self.assertEqual(self.cliente().get(f"/pedido/{self.oid}/pix").status_code, 404)


class TesteChamadasEfi(BaseTeste):
    """Confere o formato das chamadas à API da Efí (token OAuth e cobrança)."""

    def test_token_e_cobranca(self):
        ligar(self, EFI)
        efi._token.update(valor="", expira=0)
        chamadas = []

        def urlopen(req, timeout, context):
            chamadas.append(req)
            corpo = ({"access_token": "TK", "expires_in": 3600} if req.full_url.endswith("/oauth/token")
                     else {"pixCopiaECola": "000201COPIA", "status": "ATIVA"})
            r = io.BytesIO(json.dumps(corpo).encode())
            r.__enter__, r.__exit__ = (lambda s=r: s), (lambda *a: None)
            return r

        with app.app_context(), mock.patch("efi._ssl", return_value=None), \
                mock.patch("efi.urllib.request.urlopen", side_effect=urlopen):
            pedido = Order(id=42, customer_name="Ana", total=Decimal("150.5"), documento="11222333000181",
                           razao_social="ACME LTDA")
            self.assertEqual(efi.criar_cobranca(pedido), ("Marketplace B2B" + "42".zfill(25), "000201COPIA"))
        token, cob = chamadas
        self.assertTrue(token.get_header("Authorization").startswith("Basic "))
        self.assertEqual(cob.get_method(), "PUT")
        self.assertEqual(cob.get_header("Authorization"), "Bearer TK")
        corpo = json.loads(cob.data)
        self.assertEqual(corpo["valor"]["original"], "150.50")
        self.assertEqual(corpo["devedor"], {"cnpj": "11222333000181", "nome": "ACME LTDA"})
        self.assertEqual(corpo["chave"], "chave@ex.com")


if __name__ == "__main__":
    unittest.main()
