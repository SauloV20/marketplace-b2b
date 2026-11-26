"""Testes das correções da revisão de segurança."""
import re
import unittest

from test_b2b import BaseB2B

from app import app  # noqa: E402
from models import Banner, Category, Order, User, db  # noqa: E402


class TesteRevisaoSeguranca(BaseB2B):
    def test_nome_do_cliente_nao_vira_codigo_no_painel(self):
        self.cadastrar("x@teste.com", tipo="pj", razao_social="ACME');alert(1);('")
        with app.app_context():
            uid = User.query.filter_by(email="x@teste.com").one().id
            db.session.add(Category(name="Cat');alert(1);('"))
            db.session.commit()
        a = self.admin()
        html = a.get(f"/admin/clientes/{uid}").get_data(as_text=True)
        # as perguntas de confirmar ficam em data-confirmar (texto, não código) e o nome vai escapado
        self.assertTrue(re.findall(r'data-confirmar="([^"]*)"', html))
        self.assertNotIn("');alert(1)", html)
        self.assertNotRegex(html, r"\son(click|submit)=")
        html = a.get("/admin/categories").get_data(as_text=True)
        self.assertNotIn("');alert(1)", html)
        self.assertNotRegex(html, r"\son(click|submit)=")

    def test_usuario_removido_da_equipe_nao_compra_mais(self):
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)
        titular = self.empresa()
        self.post(titular, "/conta/equipe", {"acao": "convidar", "nome": "Ivo", "email": "ivo@empresa.com",
                                             "papel": "comprador", "limite": ""}, "/conta/equipe")
        link = next(re.search(r"https?://\S+", e["texto"]).group(0) for e in reversed(self.emails)
                    if e["para"] == "ivo@empresa.com").split("localhost", 1)[-1]
        ivo = self.cliente()
        self.post(ivo, link, {"nova": "senha-ivo-123", "nova2": "senha-ivo-123"}, link)
        self.post(ivo, "/entrar", {"email": "ivo@empresa.com", "senha": "senha-ivo-123"}, "/entrar")
        self.adicionar(ivo, 1, 1)
        token = self.token(ivo, "/carrinho")
        with app.app_context():
            uid = User.query.filter_by(email="ivo@empresa.com").one().id
            antes = Order.query.count()
        self.post(titular, "/conta/equipe", {"acao": "remover", "uid": str(uid)}, "/conta/equipe")
        r = ivo.post("/checkout", data={"_csrf": token, "name": "Ivo", "email": "ivo@empresa.com", "phone": "11",
                                        "address": "Rua A, 1", "cep": "01001-000", "payment": "pix"})
        self.assertIn("/entrar", r.headers["Location"])
        with app.app_context():
            self.assertEqual(Order.query.count(), antes, "nenhum pedido sem aprovação")
        self.assertIn("/entrar", ivo.get("/carrinho").headers["Location"])

    def test_membro_nao_mexe_no_celular_do_titular(self):
        titular = self.empresa()
        self.post(titular, "/conta/equipe", {"acao": "convidar", "nome": "Ju", "email": "ju@empresa.com",
                                             "papel": "aprovador"}, "/conta/equipe")
        link = re.search(r"https?://\S+", self.emails[-1]["texto"]).group(0).split("localhost", 1)[-1]
        ju = self.cliente()
        self.post(ju, link, {"nova": "senha-ju-1234", "nova2": "senha-ju-1234"}, link)
        self.post(ju, "/entrar", {"email": "ju@empresa.com", "senha": "senha-ju-1234"}, "/entrar")
        self.assertIn("/conta", ju.get("/conta/celular").headers["Location"])

    def test_convites_tem_limite(self):
        titular = self.empresa()
        dados = {"acao": "convidar", "nome": "K", "email": "k@empresa.com", "papel": "comprador"}
        self.post(titular, "/conta/equipe", dados, "/conta/equipe")
        with app.app_context():
            uid = User.query.filter_by(email="k@empresa.com").one().id
        r = self.post(titular, "/conta/equipe", {"acao": "reenviar", "uid": str(uid)}, "/conta/equipe")
        self.assertIn("Aguarde um minuto", titular.get("/conta/equipe").get_data(as_text=True))
        self.assertEqual(sum(1 for e in self.emails if e["para"] == "k@empresa.com"), 1)
        self.assertEqual(r.status_code, 302)

    def test_manual_do_painel(self):
        a = self.admin()
        html = a.get("/admin/manual").get_data(as_text=True)
        self.assertIn("Manual do painel", html)
        self.assertIn('href="/admin/equipe"', html, "acesso total: links para todas as áreas")
        # quem tem só Pedidos também abre o manual, mas sem links para áreas fechadas
        with app.app_context():
            u = User(name="Vend", email="vend@loja-exemplo.com", is_admin=True, email_verified=True, permissoes="pedidos")
            u.set_password("senha-vendedor-123")
            db.session.add(u)
            db.session.commit()
        v = self.cliente()
        self.post(v, "/admin/login", {"email": "vend@loja-exemplo.com", "password": "senha-vendedor-123"}, "/admin/login")
        html = v.get("/admin/manual").get_data(as_text=True)
        self.assertIn('href="/admin/orders"', html)
        self.assertNotIn('href="/admin/equipe"', html)
        self.assertNotIn('href="/admin/products"', html)

    def test_link_do_banner_nao_sai_da_loja_disfarcado(self):
        from banners import ler_formulario
        with app.test_request_context():
            for link in ("//site-malicioso.com", "/\\site-malicioso.com", "javascript:alert(1)"):
                self.assertIsNotNone(ler_formulario({"titulo": "X", "link": link}, None)[2], link)
            self.assertIsNone(ler_formulario({"titulo": "X", "link": "/?cat=1"}, None)[2])
        with app.app_context():
            self.assertEqual(Banner.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
