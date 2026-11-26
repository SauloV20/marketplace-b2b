"""Testes do que vai para o Google: robots.txt, sitemap.xml, títulos, dados estruturados e páginas privadas."""
import json
import re
import unittest

from test_b2b import BaseB2B

from app import app  # noqa: E402


class TesteSeo(BaseB2B):
    def setUp(self):
        super().setUp()
        app.config["SITE_URL"] = "https://loja.exemplo"
        self.addCleanup(app.config.update, SITE_URL="")

    def test_robots_nao_revela_o_painel(self):
        texto = self.cliente().get("/robots.txt").get_data(as_text=True)
        self.assertIn("Sitemap: https://loja.exemplo/sitemap.xml", texto)
        self.assertIn("Disallow: /checkout", texto)
        self.assertNotIn("admin", texto.lower())

    def test_sitemap_tem_produtos_ativos_e_categorias(self):
        self.ajustar_produto(18, active=False)
        r = self.cliente().get("/sitemap.xml")
        self.assertEqual(r.mimetype, "application/xml")
        xml = r.get_data(as_text=True)
        self.assertIn("<loc>https://loja.exemplo/produto/14-disco-de-desbaste-7-pol</loc>", xml)
        self.assertIn("<loc>https://loja.exemplo/?cat=", xml)
        self.assertNotIn("torno-de-bancada", xml, "produto desativado fica de fora")
        self.assertNotIn("/carrinho", xml)

    def test_titulos_e_descricao(self):
        c = self.cliente()
        self.assertIn("<title>Marketplace B2B · Ferramentas industriais com preço de atacado</title>", c.get("/").get_data(as_text=True))
        html = c.get("/?cat=5").get_data(as_text=True)
        self.assertIn("<title>Abrasivos com preço de atacado · Marketplace B2B</title>", html)
        self.assertIn('<link rel="canonical" href="https://loja.exemplo/?cat=5">', html)

    def test_dados_estruturados_do_produto_sem_preco(self):
        html = self.cliente().get("/produto/14-disco-de-desbaste-7-pol").get_data(as_text=True)
        dados = json.loads(re.search(r'application/ld\+json">(.*?)</script>', html).group(1))
        self.assertEqual((dados["@type"], dados["name"], dados["url"]),
                         ("Product", "Disco de Desbaste 7 pol", "https://loja.exemplo/produto/14-disco-de-desbaste-7-pol"))
        self.assertNotIn("offers", dados)
        self.assertIn('<link rel="canonical" href="https://loja.exemplo/produto/14', html)

    def test_paginas_privadas_nao_indexadas(self):
        app.config["SITE_URL"] = ""  # o link de confirmação do cadastro de teste precisa apontar para o próprio teste
        c = self.cadastrar()
        for url in ("/carrinho", "/conta", "/pedido-rapido", "/entrar", "/?q=disco"):
            self.assertIn('name="robots" content="noindex', c.get(url).get_data(as_text=True), url)
        for url in ("/", "/?cat=5", "/produto/14-disco-de-desbaste-7-pol"):
            self.assertNotIn('content="noindex', self.cliente().get(url).get_data(as_text=True), url)
        self.assertIn('content="noindex', self.cliente().get("/entrar").get_data(as_text=True))
        self.assertNotIn('content="noindex', self.cliente().get("/cadastro").get_data(as_text=True))
        self.assertEqual(self.cliente().get("/admin/login").headers["X-Robots-Tag"], "noindex, nofollow")

    def test_verificacao_do_search_console(self):
        app.config["GOOGLE_SITE_VERIFICATION"] = "abc123"
        self.addCleanup(app.config.update, GOOGLE_SITE_VERIFICATION="")
        self.assertIn('<meta name="google-site-verification" content="abc123">',
                      self.cliente().get("/").get_data(as_text=True))

    def test_endereco_do_render_vai_para_o_dominio(self):
        c = self.cliente()
        r = c.get("/produto/14-disco?x=1", base_url="https://marketplace-b2b.onrender.com")
        self.assertEqual((r.status_code, r.headers["Location"]), (301, "https://loja.exemplo/produto/14-disco?x=1"))
        r = c.get("/", base_url="https://marketplace-b2b.onrender.com")
        self.assertEqual(r.headers["Location"], "https://loja.exemplo/")
        # formulário enviado não é redirecionado; o próprio domínio e o localhost também não
        self.assertNotEqual(c.post("/entrar", base_url="https://marketplace-b2b.onrender.com").status_code, 301)
        self.assertEqual(c.get("/", base_url="https://loja.exemplo").status_code, 200)
        self.assertEqual(c.get("/").status_code, 200)
        # sem domínio próprio (SITE_URL ainda é o do Render), nada muda
        app.config["SITE_URL"] = "https://marketplace-b2b.onrender.com"
        self.assertEqual(c.get("/", base_url="https://marketplace-b2b.onrender.com").status_code, 200)


if __name__ == "__main__":
    unittest.main()
