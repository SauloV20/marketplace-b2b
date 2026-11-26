"""Testes do diagnóstico de e-mail: falha de envio aparece para o cliente e para o admin (sem rede de verdade)."""
import io
import json
import smtplib
import unittest
import urllib.error
from unittest import mock

from test_site import SENHA, BaseTeste, novo_cpf

from app import app  # noqa: E402
from email_utils import enviar_email_detalhado  # noqa: E402


class TesteEmail(BaseTeste):
    def setUp(self):
        super().setUp()
        antes = {k: app.config.get(k) for k in ("EMAILS_ENVIADOS", "PRODUCAO", "RESEND_API_KEY", "MAIL_SERVER",
                                                 "BREVO_API_KEY", "MAIL_FROM", "MAIL_PORT")}
        self.addCleanup(app.config.update, antes)
        app.config.update(EMAILS_ENVIADOS=None, PRODUCAO=True, RESEND_API_KEY="", MAIL_SERVER="", BREVO_API_KEY="")

    def test_no_ar_sem_provedor_e_falha_e_nao_enviado(self):
        with app.test_request_context():
            ok, motivo = enviar_email_detalhado("a@b.com", "x", "y")
        self.assertFalse(ok)
        self.assertIn("RESEND_API_KEY", motivo)
        c = self.cliente()
        r = self.post(c, "/cadastro", {"nome": "Ana", "email": "ana@ex.com", "senha": SENHA, "senha2": SENHA, "aceite": "1",
                                       "tipo": "pf", "documento": novo_cpf()})
        self.assertIn("não conseguimos enviar o e-mail de confirmação", c.get(r.headers["Location"]).get_data(as_text=True))

    def test_fora_do_ar_sem_provedor_so_mostra_no_terminal(self):
        app.config["PRODUCAO"] = False
        with app.test_request_context():
            self.assertEqual(enviar_email_detalhado("a@b.com", "x", "y"), (True, ""))

    def test_motivo_da_resend_aparece(self):
        app.config["RESEND_API_KEY"] = "re_teste"
        erro = urllib.error.HTTPError("u", 403, "x", {}, io.BytesIO(b'{"message":"The domain is not verified"}'))
        with app.test_request_context(), mock.patch("email_utils.urllib.request.urlopen", side_effect=erro):
            ok, motivo = enviar_email_detalhado("a@b.com", "x", "y")
        self.assertFalse(ok)
        self.assertIn("domain is not verified", motivo)

    def test_senha_smtp_errada_explica_a_senha_de_app(self):
        app.config.update(MAIL_SERVER="smtp.gmail.com", MAIL_PORT=465)
        with app.test_request_context(), mock.patch("email_utils.smtplib.SMTP_SSL",
                                                    side_effect=smtplib.SMTPAuthenticationError(535, b"bad")):
            ok, motivo = enviar_email_detalhado("a@b.com", "x", "y")
        self.assertFalse(ok)
        self.assertIn("senha de app", motivo)

    def test_painel_avisa_e_testa_o_envio(self):
        a = self.admin()
        self.assertIn("nenhum serviço de e-mail está configurado", a.get("/admin/").get_data(as_text=True))
        self.post(a, "/admin/conta/email-teste", {}, "/admin/conta")
        self.assertIn("O e-mail de teste falhou", a.get("/admin/conta").get_data(as_text=True))
        app.config["EMAILS_ENVIADOS"] = []
        app.config["RESEND_API_KEY"] = "re_teste"
        self.post(a, "/admin/conta/email-teste", {}, "/admin/conta")
        self.assertEqual(app.config["EMAILS_ENVIADOS"][0]["para"], "admin@exemplo.com")
        self.assertNotIn("nenhum serviço de e-mail", a.get("/admin/").get_data(as_text=True))

    def test_brevo_pela_api(self):
        app.config.update(BREVO_API_KEY="xkeysib-teste", MAIL_FROM="Marketplace B2B <loja@ex.com>")
        chamadas = []

        def urlopen(req, timeout):
            chamadas.append(req)
            return io.BytesIO(b'{"messageId": "1"}')

        with app.test_request_context(), mock.patch("email_utils.urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(enviar_email_detalhado("a@b.com", "Assunto", "Texto"), (True, ""))
        req = chamadas[0]
        self.assertEqual(req.full_url, "https://api.brevo.com/v3/smtp/email")
        self.assertEqual(req.get_header("Api-key"), "xkeysib-teste")
        dados = json.loads(req.data)
        self.assertIn("<!doctype html>", dados.pop("htmlContent"), "vai também a versão bonita do e-mail")
        self.assertEqual(dados, {"sender": {"name": "Marketplace B2B", "email": "loja@ex.com"},
                                 "to": [{"email": "a@b.com"}], "subject": "Assunto", "textContent": "Texto"})
        erro = urllib.error.HTTPError("u", 400, "x", {}, io.BytesIO(b'{"message":"sender not valid"}'))
        with app.test_request_context(), mock.patch("email_utils.urllib.request.urlopen", side_effect=erro):
            ok, motivo = enviar_email_detalhado("a@b.com", "x", "y")
        self.assertFalse(ok)
        self.assertIn("sender not valid", motivo)

    def test_timeout_de_smtp_explica_o_bloqueio_do_render(self):
        app.config.update(MAIL_SERVER="smtp-relay.brevo.com", MAIL_PORT=587)
        with app.test_request_context(), mock.patch("email_utils.smtplib.SMTP", side_effect=TimeoutError("timed out")):
            ok, motivo = enviar_email_detalhado("a@b.com", "x", "y")
        self.assertFalse(ok)
        self.assertIn("BREVO_API_KEY", motivo)


if __name__ == "__main__":
    unittest.main()
