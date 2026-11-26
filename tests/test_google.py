"""Como o Google enxerga as páginas: título principal, descrição, link oficial e texto das fotos."""
import re
import unittest

from test_b2b import BaseB2B

from app import app  # noqa: E402
from models import Product, db  # noqa: E402

LINK = "https://exemplo.com/foto.png"


class TesteGoogle(BaseB2B):
    def html(self, caminho):
        r = self.cliente().get(caminho)
        self.assertEqual(r.status_code, 200, caminho)
        return r.get_data(as_text=True)

    def test_pagina_de_categoria_tem_titulo_principal(self):
        with app.app_context():
            p = db.session.get(Product, 1)
            cat, nome = p.category_id, p.category.name
        html = self.html(f"/?cat={cat}")
        self.assertEqual(len(re.findall(r"<h1[\s>]", html)), 1)
        self.assertRegex(html, rf"<h1>[^<]*{re.escape(nome)}")

    def test_busca_tem_titulo_principal(self):
        self.assertEqual(len(re.findall(r"<h1[\s>]", self.html("/?q=disco"))), 1)

    def test_fotos_dos_produtos_tem_o_nome_como_texto(self):
        self.ajustar_produto(2, image_url=LINK)
        with app.app_context():
            p = db.session.get(Product, 2)
            nome, cat = p.name, p.category_id
        html = self.html(f"/?cat={cat}")
        self.assertIn(f'<img src="{LINK}" alt="{nome}"', html)
        self.assertNotRegex(html, r'<a class="thumb"[^>]*><img [^>]*alt=""')

    def test_paginas_fixas_tem_descricao_e_link_oficial(self):
        for caminho in ("/cadastro", "/privacidade", "/termos"):
            html = self.html(caminho)
            self.assertRegex(html, r'<meta name="description" content="[^"]{40,}">', caminho)
            self.assertRegex(html, rf'<link rel="canonical" href="[^"]*{caminho}">', caminho)


if __name__ == "__main__":
    unittest.main()
