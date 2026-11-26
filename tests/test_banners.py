"""Testes dos banners da página inicial (carrossel) e do cadastro no painel."""
import io
import unittest

from test_b2b import BaseB2B

from app import app  # noqa: E402
from models import Banner, Product, db  # noqa: E402

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


class TesteBanners(BaseB2B):
    def salvar(self, a, dados, imagem=None, bid=None):
        url = f"/admin/banners/{bid}" if bid else "/admin/banners/novo"
        campos = {"_csrf": self.token(a, "/admin/banners"), "ativo": "on", "cor": "azul", **dados}
        if imagem is not None:
            campos["imagem"] = (io.BytesIO(imagem[0]), imagem[1])
        return a.post(url, data=campos, content_type="multipart/form-data")

    def banners(self):
        with app.app_context():
            return Banner.query.order_by(Banner.ordem, Banner.id).all()

    def test_sem_banner_mostra_o_padrao(self):
        html = self.cliente().get("/").get_data(as_text=True)
        self.assertIn("Tudo para usinagem, solda e segurança", html)
        self.assertNotIn('class="carrossel"', html)

    def test_banner_de_produto_e_preco_so_para_quem_entrou(self):
        a = self.admin()
        self.salvar(a, {"product_id": "1"})
        visitante = self.cliente().get("/").get_data(as_text=True)
        self.assertIn('class="carrossel"', visitante)
        self.assertIn("Fresa de Topo Metal Duro 10 mm 4 Cortes", visitante)
        self.assertIn("Ver produto", visitante)
        self.assertIn('href="/produto/1-fresa-de-topo-metal-duro-10-mm-4-cortes"', visitante)
        self.assertNotIn("R$", visitante, "visitante não vê preço nem no banner")
        logado = self.cadastrar().get("/").get_data(as_text=True)
        self.assertIn("slide-preco", logado)
        self.assertNotIn('class="carrossel"', self.cliente().get("/?q=disco").get_data(as_text=True),
                         "na busca o carrossel some")

    def test_imagem_enviada_fica_no_banco(self):
        a = self.admin()
        self.salvar(a, {"link": "/?cat=5"}, imagem=(PNG, "promo.png"))
        b = self.banners()[0]
        r = self.cliente().get(f"/banner/{b.id}/imagem")
        self.assertEqual((r.status_code, r.mimetype, r.data), (200, "image/png", PNG))
        html = self.cliente().get("/").get_data(as_text=True)
        self.assertIn("so-imagem", html)
        self.assertIn('href="/?cat=5"', html)

    def test_valida_imagem_e_link(self):
        a = self.admin()
        r = self.salvar(a, {"titulo": "X"}, imagem=(b"<script>alert(1)</script>", "foto.png"))
        self.assertIn("Envie uma imagem PNG", r.get_data(as_text=True))
        r = self.salvar(a, {"titulo": "X"}, imagem=(PNG + b"0" * 10_000_001, "grande.png"))
        self.assertIn("passa de 10 MB", r.get_data(as_text=True))
        r = self.salvar(a, {"titulo": "X", "link": "javascript:alert(1)"})
        self.assertIn("precisa começar com /", r.get_data(as_text=True))
        r = self.salvar(a, {})
        self.assertIn("Escolha um produto, envie uma imagem ou escreva um título", r.get_data(as_text=True))
        self.assertEqual(self.banners(), [])

    def test_ordem_ativar_e_excluir(self):
        a = self.admin()
        self.salvar(a, {"titulo": "Primeiro", "ordem": "1"})
        self.salvar(a, {"titulo": "Segundo", "ordem": "2"})
        segundo = self.banners()[1].id
        self.post(a, f"/admin/banners/{segundo}/mover", {"para": "cima"}, "/admin/banners")
        self.assertEqual([b.titulo for b in self.banners()], ["Segundo", "Primeiro"])
        self.post(a, f"/admin/banners/{segundo}/ativar", {}, "/admin/banners")
        html = self.cliente().get("/").get_data(as_text=True)
        self.assertNotIn("Segundo", html)
        self.assertIn("Primeiro", html)
        self.post(a, f"/admin/banners/{segundo}/excluir", {}, "/admin/banners")
        self.assertEqual([b.titulo for b in self.banners()], ["Primeiro"])

    def test_produto_desativado_some_do_carrossel(self):
        a = self.admin()
        self.salvar(a, {"product_id": "2"})
        with app.app_context():
            db.session.get(Product, 2).active = False
            db.session.commit()
        self.assertNotIn('class="carrossel"', self.cliente().get("/").get_data(as_text=True))

    def test_so_admin_mexe_em_banner(self):
        c = self.cadastrar()
        self.assertEqual(c.get("/admin/banners").status_code, 302)
        self.assertEqual(c.get("/admin/banners/novo").status_code, 302)


if __name__ == "__main__":
    unittest.main()
