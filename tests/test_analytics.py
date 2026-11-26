"""Google Analytics com aviso de cookies: desligado por padrão, liga pelo painel, CSP e evento de compra."""
import html
import json
import re
import unittest

from test_b2b import BaseB2B

from app import app  # noqa: E402

GA = "G-ABC123XYZ9"


class TesteAnalytics(BaseB2B):
    def ligar(self, valor=GA):
        a = self.admin()
        return self.post(a, "/admin/metricas", {"ga_id": valor}, "/admin/metricas")

    def test_desligado_nao_muda_nada(self):
        r = self.cliente().get("/")
        self.assertNotIn("aviso-cookies", r.get_data(as_text=True))
        self.assertNotIn("googletagmanager", r.headers["Content-Security-Policy"])
        self.assertIn("Não usamos cookies de publicidade nem de rastreamento",
                      self.cliente().get("/privacidade").get_data(as_text=True))

    def test_ligado_mostra_aviso_e_libera_o_google_so_na_loja(self):
        self.ligar(" g-abc123xyz9 ")
        r = self.cliente().get("/")
        pagina = r.get_data(as_text=True)
        self.assertIn(f'data-ga="{GA}"', pagina)
        self.assertIn("cookies.js", pagina)
        self.assertNotIn("googletagmanager.com/gtag", pagina, "o script do Google só entra depois do aceite (JS)")
        csp = r.headers["Content-Security-Policy"]
        self.assertIn("script-src 'self' https://www.googletagmanager.com", csp)
        self.assertIn("connect-src 'self' https://*.google-analytics.com", csp)
        for caminho in ("/entrar", "/cadastro", "/admin/login"):
            r = self.cliente().get(caminho)
            self.assertNotIn("aviso-cookies", r.get_data(as_text=True), caminho)
            self.assertNotIn("googletagmanager", r.headers.get("Content-Security-Policy", ""), caminho)
        politica = self.cliente().get("/privacidade").get_data(as_text=True)
        self.assertIn("Google Analytics", politica)

    def test_id_invalido_nao_salva(self):
        self.ligar("UA-1234")
        self.assertNotIn("aviso-cookies", self.cliente().get("/").get_data(as_text=True))
        self.ligar('G-1"><script>')
        self.assertNotIn("aviso-cookies", self.cliente().get("/").get_data(as_text=True))

    def test_compra_vai_uma_vez_e_sem_dados_pessoais(self):
        self.ligar()
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 2)
        r = self.fechar(c, "pix")
        pagina = c.get(r.headers["Location"]).get_data(as_text=True)
        bruto = re.search(r'data-ga-compra="([^"]+)"', pagina).group(1)
        compra = json.loads(html.unescape(bruto))
        oid = self.ultimo_pedido()[0]
        self.assertEqual((compra["transaction_id"], compra["currency"]), (str(oid), "BRL"))
        self.assertEqual(compra["items"][0]["quantity"], 2)
        self.assertNotIn("cli@teste.com", bruto)
        self.assertNotIn("data-ga-compra", c.get(f"/pedido/{oid}").get_data(as_text=True), "só na primeira vez")


if __name__ == "__main__":
    unittest.main()
