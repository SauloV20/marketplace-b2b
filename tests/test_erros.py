"""Páginas de erro com a cara da loja (erros.py)."""
import unittest
from unittest import mock

from test_b2b import BaseB2B

from app import app  # noqa: E402


class TestePaginasDeErro(BaseB2B):
    def test_link_quebrado_mostra_pagina_da_loja(self):
        r = self.cliente().get("/nao-existe-esta-pagina")
        self.assertEqual(r.status_code, 404)
        html = r.get_data(as_text=True)
        self.assertIn("Página não encontrada", html)
        self.assertIn('class="busca erro-busca"', html, "busca para achar o produto")
        self.assertIn('class="barra-baixo"', html, "com o topo e a barra da loja")
        self.assertIn("Voltar ao início", html)
        self.assertIn('name="robots" content="noindex', html, "o Google não guarda a página de erro")
        self.assertNotIn("Not Found", html)

    def test_produto_que_nao_existe(self):
        r = self.cliente().get("/produto/999999")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Página não encontrada", r.get_data(as_text=True))

    def test_erro_interno_mostra_pagina_amigavel(self):
        antes = app.config.get("PROPAGATE_EXCEPTIONS")
        app.config["PROPAGATE_EXCEPTIONS"] = False
        try:
            with mock.patch("blueprints.account.render_template", side_effect=RuntimeError("falha de teste")):
                r = self.cliente().get("/cadastro")
        finally:
            app.config["PROPAGATE_EXCEPTIONS"] = antes
        self.assertEqual(r.status_code, 500)
        html = r.get_data(as_text=True)
        self.assertIn("Algo deu errado do nosso lado", html)
        self.assertNotIn("falha de teste", html, "não mostra detalhes do erro ao visitante")

    def test_se_a_pagina_bonita_falhar_mostra_a_simples(self):
        with mock.patch("erros.render_template", side_effect=RuntimeError("banco fora")):
            r = self.cliente().get("/nao-existe")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Página não encontrada", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
