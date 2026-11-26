"""Testes da página do produto e das fotos enviadas pelo painel."""
import io
import unittest

from test_b2b import BaseB2B
from test_banners import PNG

from app import app  # noqa: E402
from models import Product, ProdutoFoto, db  # noqa: E402

DISCO = "/produto/14-disco-de-desbaste-7-pol"  # 18,90; 16,90 a partir de 10; 14,90 a partir de 50


class TesteProduto(BaseB2B):
    def enviar_fotos(self, a, pid, arquivos):
        dados = {"_csrf": self.token(a, f"/admin/products/{pid}/edit"),
                 "fotos": [(io.BytesIO(conteudo), nome) for conteudo, nome in arquivos]}
        return a.post(f"/admin/products/{pid}/fotos", data=dados, content_type="multipart/form-data")

    def test_link_amigavel_e_redirecionamento(self):
        c = self.cliente()
        self.assertEqual(c.get(DISCO).status_code, 200)
        for errado in ("/produto/14", "/produto/14-nome-antigo"):
            r = c.get(errado)
            self.assertEqual(r.status_code, 301)
            self.assertTrue(r.headers["Location"].endswith(DISCO))
        self.assertEqual(c.get("/produto/9999").status_code, 404)
        self.ajustar_produto(14, active=False)
        self.assertEqual(c.get(DISCO).status_code, 404)

    def test_visitante_nao_ve_preco(self):
        html = self.cliente().get(DISCO).get_data(as_text=True)
        self.assertIn("Disco de Desbaste 7 pol", html)
        self.assertIn("Entre para ver o preço", html)
        self.assertNotIn("R$", html)
        self.assertNotIn("data-add-form", html)

    def test_tabela_de_precos_e_compra_com_quantidade(self):
        self.ajustar_produto(14, stock=500)
        c = self.cadastrar()
        html = c.get(DISCO).get_data(as_text=True)
        self.assertIn("R$ 18,90", html)
        self.assertIn("1 a 9 un", html)
        self.assertIn("10 a 49 un", html)
        self.assertIn("50 ou mais un", html)
        self.assertIn("21% OFF", html)
        self.assertIn('data-add-form="14"', html)
        self.assertEqual(self.adicionar(c, 14, 50).get_json()["count"], 50)

    def test_regras_de_minimo_e_multiplo_no_campo(self):
        self.ajustar_produto(11, stock=100)  # óculos: mínimo 12, múltiplos de 12
        html = self.cadastrar().get("/produto/11-oculos-de-protecao-incolor").get_data(as_text=True)
        self.assertIn('min="12" step="12" value="12"', html)
        self.assertIn("Pedido mínimo: <b>12 un</b>", html)

    def test_relacionados_e_links_na_vitrine(self):
        html = self.cliente().get(DISCO).get_data(as_text=True)
        self.assertIn("Veja também em Abrasivos", html)
        self.assertIn("/produto/15-disco-flap", html)
        self.assertIn(f'href="{DISCO}"', self.cliente().get("/").get_data(as_text=True))

    def test_fotos_pelo_painel(self):
        a = self.admin()
        self.enviar_fotos(a, 14, [(PNG, "a.png"), (PNG, "b.png")])
        with app.app_context():
            fotos = db.session.get(Product, 14).fotos
            ids = [f.id for f in fotos]
        self.assertEqual(len(ids), 2)
        r = self.cliente().get(f"/produto/foto/{ids[0]}")
        self.assertEqual((r.status_code, r.mimetype), (200, "image/png"))
        html = self.cliente().get(DISCO).get_data(as_text=True)
        self.assertIn(f"/produto/foto/{ids[0]}", html)
        self.assertIn('class="miniaturas"', html)
        self.assertIn(f"/produto/foto/{ids[0]}", self.cliente().get("/").get_data(as_text=True), "foto vai para a vitrine")
        self.post(a, f"/admin/products/14/fotos/{ids[1]}/principal", {}, "/admin/products/14/edit")
        with app.app_context():
            self.assertEqual(db.session.get(Product, 14).fotos[0].id, ids[1])
        self.post(a, f"/admin/products/14/fotos/{ids[0]}/excluir", {}, "/admin/products/14/edit")
        with app.app_context():
            self.assertEqual(ProdutoFoto.query.count(), 1)

    def test_fotos_validadas(self):
        a = self.admin()
        r = self.enviar_fotos(a, 14, [(b"nao sou imagem", "x.png")])
        self.assertIn("não é uma imagem", a.get(r.headers["Location"]).get_data(as_text=True))
        r = self.enviar_fotos(a, 14, [(PNG, f"{i}.png") for i in range(9)])
        self.assertIn("até 8 fotos", a.get(r.headers["Location"]).get_data(as_text=True))
        with app.app_context():
            self.assertEqual(ProdutoFoto.query.count(), 0)
        self.post(a, "/admin/products/14/fotos/999/excluir", {}, "/admin/products/14/edit")
        self.assertEqual(self.cadastrar().post("/admin/products/14/fotos").status_code, 400)


if __name__ == "__main__":
    unittest.main()


class TesteCadastroCompleto(BaseB2B):
    def cadastrar_produto(self, a, dados, fotos=()):
        campos = {"_csrf": self.token(a, "/admin/products"), "category_id": "5", "price": "10,00", "stock": "100",
                  "qtd_minima": "1", "multiplo": "1", **dados}
        if fotos:
            campos["fotos"] = [(io.BytesIO(c), n) for c, n in fotos]
        return a.post("/admin/products", data=campos, content_type="multipart/form-data")

    def test_cadastra_com_fotos_faixas_e_fracionamento(self):
        a = self.admin()
        r = self.cadastrar_produto(a, {"name": "Disco Novo", "description": "Linha 1\nLinha 2", "multiplo": "10",
                                       "fracionavel": "on", "acrescimo_fracionado": "15", "faixas": "50 = 9,00"},
                                   fotos=[(PNG, "a.png"), (PNG, "b.png")])
        self.assertEqual(r.status_code, 302)
        self.assertIn("cadastrado com 2 foto(s)", a.get(r.headers["Location"]).get_data(as_text=True))
        with app.app_context():
            p = Product.query.filter_by(name="Disco Novo").one()
            self.assertEqual((len(p.fotos), p.fracionavel, p.multiplo, [(f.qtd_minima, str(f.preco)) for f in p.faixas]),
                             (2, True, 10, [(50, "9.00")]))
            pid = p.id
        html = self.cliente().get(f"/produto/{pid}-disco-novo").get_data(as_text=True)
        self.assertIn("/produto/foto/", html)
        self.assertIn("<p>Linha 2</p>", html)

    def test_sem_foto_tambem_cadastra(self):
        a = self.admin()
        self.cadastrar_produto(a, {"name": "Sem Foto"})
        with app.app_context():
            self.assertEqual(len(Product.query.filter_by(name="Sem Foto").one().fotos), 0)

    def test_erro_nao_grava_nada_e_mantem_o_que_foi_digitado(self):
        a = self.admin()
        r = self.cadastrar_produto(a, {"name": "Com Foto Ruim", "description": "não apagar"},
                                   fotos=[(b"texto", "x.png")])
        html = r.get_data(as_text=True)
        self.assertIn("não é uma imagem", html)
        self.assertIn('value="Com Foto Ruim"', html)
        self.assertIn("não apagar", html)
        self.assertIn("<details class=\"box novo-produto\" open", html)
        r = self.cadastrar_produto(a, {"name": "Faixa Ruim", "faixas": "dez = 1"})
        self.assertIn("Faixa inválida", r.get_data(as_text=True))
        with app.app_context():
            self.assertEqual(Product.query.filter(Product.name.in_(["Com Foto Ruim", "Faixa Ruim"])).count(), 0)
