"""E-mails com a cara da loja (email_html.py) e textos revisados."""
import os
import unittest

from test_b2b import BaseB2B

from app import app  # noqa: E402
import email_html  # noqa: E402

SALVAR = os.getenv("SALVAR_EMAIL")  # pasta para guardar um exemplo e abrir no navegador


class TesteEmailBonito(BaseB2B):
    def pedido_pix(self):
        c = self.empresa(aprovar=False)
        self.emails.clear()
        self.adicionar(c, 1, 2)
        self.fechar(c, "pix")
        return next(e for e in self.emails if "recebido" in e["assunto"])

    def test_pedido_pix_diz_onde_pagar(self):
        e = self.pedido_pix()
        self.assertIn("Veja como pagar: http", e["texto"])
        self.assertIn("Acompanhe em: http", e["texto"])
        if SALVAR:
            with open(os.path.join(SALVAR, "email_pedido.html"), "w", encoding="utf-8") as f:
                f.write(e["html"])

    def test_html_tem_botoes_lista_e_texto_escapado(self):
        e = self.pedido_pix()
        html = e["html"]
        self.assertIn(">Marketplace B2B", html)
        self.assertIn(">Ver como pagar</a>", html, "link principal vira botão")
        self.assertIn(">Acompanhar pedido</a>", html)
        self.assertIn("background:#1E4FD8;color:#ffffff", html, "o primeiro botão é o azul")
        self.assertIn("<li", html, "itens do pedido em lista")
        self.assertIn("<b>Total:", html)

    def test_nada_do_texto_vira_codigo(self):
        with app.app_context():
            html = email_html.montar("Teste", "Olá, <script>alert(1)</script> & cia!\n\nhttps://x.com/a?b=1&c=2")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn('href="https://x.com/a?b=1&amp;c=2"', html)

    def test_rodape_com_whatsapp_quando_configurado(self):
        antes = app.config.get("WHATSAPP_NUMBER")
        app.config["WHATSAPP_NUMBER"] = "5524999998888"
        try:
            with app.app_context():
                html = email_html.montar("Teste", "Olá")
        finally:
            app.config["WHATSAPP_NUMBER"] = antes
        self.assertIn("https://wa.me/5524999998888", html)

    def test_confirmacao_de_email_tem_botao_e_assinatura(self):
        self.emails.clear()
        self.cadastrar("novo@teste.com", tipo="pj")
        e = next(e for e in self.emails if e["para"] == "novo@teste.com")
        self.assertIn("Confirmar e-mail: http", e["texto"])
        self.assertIn(">Confirmar e-mail</a>", e["html"])
        self.assertTrue(e["texto"].endswith("Equipe Marketplace B2B"))


if __name__ == "__main__":
    unittest.main()
