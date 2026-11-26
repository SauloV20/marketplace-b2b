"""Produtos com foto vinda de outro site (link): lista no painel e aviso ao salvar."""
import unittest

from test_b2b import BaseB2B

from app import app  # noqa: E402
from models import Product, db  # noqa: E402
import produtos  # noqa: E402

LINK = "https://encrypted-tbn0.gstatic.com/images?q=foto-de-outro-site"
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
       b"\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


class TesteFotosDeOutroSite(BaseB2B):
    def com_link(self, pid, foto_propria=False):
        self.ajustar_produto(pid, image_url=LINK)
        if foto_propria:
            with app.app_context():
                p = db.session.get(Product, pid)
                db.session.add(produtos.nova_foto(p, PNG, "image/png", 1))
                db.session.commit()

    def formulario(self, pid, **mudar):
        with app.app_context():
            p = db.session.get(Product, pid)
            return {"name": p.name, "category_id": p.category_id, "price": "149,90", "stock": 50, "active": "on",
                    "qtd_minima": 1, "multiplo": 1, "image_url": p.image_url or "", **mudar}

    def test_sem_fotos_de_fora_nao_mostra_aviso(self):
        html = self.admin().get("/admin/products").get_data(as_text=True)
        self.assertNotIn("Foto de outro site", html)

    def test_lista_so_os_produtos_com_link(self):
        self.com_link(2)
        self.com_link(3, foto_propria=True)
        a = self.admin()
        html = a.get("/admin/products").get_data(as_text=True)
        self.assertIn("<b>2 produto(s)</b> usam foto de outro site", html)
        self.assertIn("Foto de outro site (2)", html)
        lista = a.get("/admin/products?foto=link").get_data(as_text=True)
        with app.app_context():
            nomes = {p.id: p.name for p in Product.query.all()}
        self.assertIn(nomes[2], lista)
        self.assertIn(nomes[3], lista)
        self.assertNotIn(nomes[1], lista, "produto sem link fica fora da lista")
        self.assertIn("foto de outro site", lista)
        self.assertIn("tem foto e também link de fora", lista, "já tem foto: só falta apagar o link")
        self.assertIn("/admin/products/2/edit", lista)

    def test_pagina_de_edicao_explica_o_que_fazer(self):
        self.com_link(2)
        html = self.admin().get("/admin/products/2/edit").get_data(as_text=True)
        self.assertIn("A foto deste produto vem de outro site", html)

    def test_salvar_com_link_avisa(self):
        a = self.admin()
        r = self.post(a, "/admin/products/2/edit", self.formulario(2, image_url=LINK), "/admin/products/2/edit")
        self.assertEqual(r.status_code, 302)
        self.assertIn("vem de outro site", a.get("/admin/products").get_data(as_text=True))

    def test_apagar_o_link_tira_da_lista(self):
        self.com_link(2, foto_propria=True)
        a = self.admin()
        self.post(a, "/admin/products/2/edit", self.formulario(2, image_url=""), "/admin/products/2/edit")
        with app.app_context():
            self.assertEqual(db.session.get(Product, 2).image_url, "")
        html = a.get("/admin/products").get_data(as_text=True)
        self.assertNotIn("usam foto de outro site", html)

    def test_cadastro_novo_com_link_avisa(self):
        a = self.admin()
        with app.app_context():
            cat = db.session.get(Product, 1).category_id
        self.post(a, "/admin/products", {"name": "Produto com link", "category_id": cat, "price": "10,00",
                                         "stock": 5, "image_url": LINK}, "/admin/products")
        self.assertIn("vem de outro site", a.get("/admin/products").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
