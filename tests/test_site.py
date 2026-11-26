"""Testes automáticos do marketplace Marketplace B2B.

Rode na pasta do projeto:  python -m unittest discover -s tests -v
Usa um banco SQLite em memória: não mexe no seu MySQL nem no banco do Render, e não envia e-mails de verdade.
"""
import itertools
import os
import re
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(DATABASE_URL="sqlite:///:memory:", APP_ENV="test", SECRET_KEY="chave-so-para-testes", SITE_URL="",
                  ADMIN_PATH="/admin", TWILIO_ACCOUNT_SID="", TWILIO_AUTH_TOKEN="", TWILIO_VERIFY_SID="",
                  EFI_CLIENT_ID="", EFI_CLIENT_SECRET="", EFI_CERTIFICADO="", PIX_KEY="", CONSULTA_CNPJ="0",
                  ANTHROPIC_API_KEY="")
os.environ.pop("RENDER", None)

from sqlalchemy import inspect, text  # noqa: E402

from app import app, atualizar_esquema, carregar_catalogo_exemplo  # noqa: E402
from models import Category, Order, Product, User, db  # noqa: E402
from security import limpar_limites  # noqa: E402

SENHA = "senha-boa-123"
ADMIN_SENHA = "admin-forte-123"
_seq = itertools.count(100000000)


def _dv(numeros, pesos):
    resto = sum(int(n) * p for n, p in zip(numeros, pesos)) % 11
    return "0" if resto < 2 else str(11 - resto)


def novo_cpf():
    base = str(next(_seq))
    d1 = _dv(base, range(10, 1, -1))
    return base + d1 + _dv(base + d1, range(11, 1, -1))


def novo_cnpj():
    base = str(next(_seq))[1:] + "0001"
    pesos = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    d1 = _dv(base, pesos)
    return base + d1 + _dv(base + d1, [6] + pesos)


class BaseTeste(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, EMAILS_ENVIADOS=[])
        limpar_limites()
        with app.app_context():
            db.drop_all()
            db.create_all()
            admin = User(name="Admin", email="admin@exemplo.com", is_admin=True, email_verified=True)
            admin.set_password(ADMIN_SENHA)
            db.session.add(admin)
            db.session.commit()
            carregar_catalogo_exemplo()

    # ---------- ajudantes ----------
    @property
    def emails(self):
        return app.config["EMAILS_ENVIADOS"]

    def cliente(self):
        return app.test_client()

    def token(self, c, caminho="/"):
        html = c.get(caminho).get_data(as_text=True)
        return re.search(r'name="csrf" content="([^"]+)"', html).group(1)

    def post(self, c, caminho, dados, pagina=None):
        return c.post(caminho, data={"_csrf": self.token(c, pagina or "/"), **dados})

    def link_do_ultimo_email(self):
        return re.search(r"https?://\S+", self.emails[-1]["texto"]).group(0).split("localhost", 1)[-1]

    def cadastrar(self, email="cli@teste.com", confirmar=True, tipo="pf", **extra):
        c = self.cliente()
        doc = novo_cnpj() if tipo == "pj" else novo_cpf()
        dados = {"nome": "Cliente Teste", "email": email, "senha": SENHA, "senha2": SENHA, "tipo": tipo, "aceite": "1",
                 "documento": doc, "razao_social": "Metalúrgica Teste Ltda" if tipo == "pj" else "", **extra}
        r = self.post(c, "/cadastro", dados)
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[-600:])
        if confirmar:
            self.assertEqual(c.get(self.link_do_ultimo_email()).status_code, 302)
        return c

    def entrar(self, email="cli@teste.com", senha=SENHA):
        c = self.cliente()
        r = self.post(c, "/entrar", {"email": email, "senha": senha})
        return c, r

    def admin(self):
        c = self.cliente()
        r = self.post(c, "/admin/login", {"email": "admin@exemplo.com", "password": ADMIN_SENHA})
        self.assertEqual(r.status_code, 302)
        return c

    def estoque(self, pid):
        with app.app_context():
            return db.session.get(Product, pid).stock


class TesteLoja(BaseTeste):
    def test_pagina_inicial_mostra_produtos(self):
        r = self.cliente().get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Fresa de Topo", r.get_data(as_text=True))

    def test_cabecalhos_de_seguranca(self):
        h = self.cliente().get("/").headers
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertIn("Content-Security-Policy", h)

    def test_formulario_sem_token_csrf_e_recusado(self):
        self.assertEqual(self.cliente().post("/cadastro", data={"nome": "x"}).status_code, 400)
        c = self.cliente()
        c.get("/")
        self.assertEqual(c.post("/carrinho/add", data={"pid": 1, "qty": 1}).status_code, 400)

    def test_visitante_nao_ve_preco_nem_compra(self):
        c = self.cliente()
        html = c.get("/").get_data(as_text=True)
        self.assertIn("Entre para ver o preço", html)
        self.assertNotIn("R$", html, "o preço não pode ir nem no código da página")
        r = self.post(c, "/carrinho/add", {"pid": 1, "qty": 1})
        self.assertEqual(r.status_code, 401)
        for url in ("/carrinho", "/checkout"):
            self.assertIn("/entrar", c.get(url).headers["Location"])

    def test_compra_logado_baixa_o_estoque(self):
        c = self.cadastrar()
        self.assertIn("R$", c.get("/").get_data(as_text=True))
        antes = self.estoque(1)
        r = self.post(c, "/carrinho/add", {"pid": 1, "qty": 2})
        self.assertTrue(r.get_json()["ok"])
        r = self.post(c, "/checkout", {"name": "A", "email": "a@b.com", "phone": "11", "address": "Rua 1", "cep": "01001000",
                                       "payment": "pix"}, "/checkout")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c.get(r.headers["Location"]).status_code, 200)
        self.assertEqual(self.estoque(1), antes - 2)


class TesteCadastroEConfirmacao(BaseTeste):
    def test_cadastro_envia_email_e_so_entra_depois_de_confirmar(self):
        self.cadastrar(confirmar=False)
        self.assertEqual(len(self.emails), 1)
        self.assertEqual(self.emails[0]["para"], "cli@teste.com")
        c, r = self.entrar()
        self.assertIn("/confirme-seu-email", r.headers["Location"])
        self.assertEqual(c.get("/conta").status_code, 302, "sem confirmar não pode ver a conta")
        c = self.cliente()
        self.assertEqual(c.get(self.link_do_ultimo_email()).status_code, 302)
        self.assertEqual(c.get("/conta").status_code, 200, "o link de confirmação já faz login")
        _, r = self.entrar()
        self.assertIn("/conta", r.headers["Location"])

    def test_link_de_confirmacao_adulterado_ou_vencido(self):
        self.cadastrar(confirmar=False)
        link = self.link_do_ultimo_email()
        self.cliente().get(link[:-3] + "abc")
        with app.app_context():
            self.assertFalse(User.query.filter_by(email="cli@teste.com").one().email_verified)
        with mock.patch("time.time", return_value=time.time() + 49 * 3600):
            self.cliente().get(link)
        with app.app_context():
            self.assertFalse(User.query.filter_by(email="cli@teste.com").one().email_verified)

    def test_email_duplicado(self):
        self.cadastrar()
        c = self.cliente()
        r = self.post(c, "/cadastro", {"nome": "Outro", "email": "CLI@teste.com", "senha": SENHA, "senha2": SENHA, "aceite": "1",
                                       "tipo": "pf", "documento": novo_cpf()})
        self.assertIn("já está cadastrado", r.get_data(as_text=True))

    def test_reenvio_de_confirmacao_tem_limite(self):
        self.cadastrar(confirmar=False)
        limpar_limites()
        c = self.cliente()
        self.post(c, "/reenviar-confirmacao", {"email": "cli@teste.com"})
        self.post(c, "/reenviar-confirmacao", {"email": "cli@teste.com"})
        self.assertEqual(len(self.emails), 2, "o segundo pedido em menos de 1 minuto é ignorado")

    def test_contas_antigas_ficam_confirmadas_na_migracao(self):
        with app.app_context():
            db.drop_all()
            db.session.execute(text('CREATE TABLE "user"(id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL, '
                                    'email VARCHAR(120) NOT NULL, password_hash VARCHAR(255) NOT NULL, '
                                    'is_admin BOOLEAN NOT NULL DEFAULT 0)'))
            db.session.execute(text('CREATE TABLE "order"(id INTEGER PRIMARY KEY, customer_name VARCHAR(120))'))
            db.session.execute(text('CREATE TABLE product(id INTEGER PRIMARY KEY, name VARCHAR(160))'))
            db.session.execute(text('CREATE TABLE order_item(id INTEGER PRIMARY KEY, quantity INTEGER)'))
            db.session.execute(text("""INSERT INTO "user" VALUES (1, 'Velho', 'v@x.com', 'h', 0)"""))
            db.session.commit()
            atualizar_esquema()
            atualizar_esquema()  # rodar de novo não pode dar erro
            colunas = {c["name"] for c in inspect(db.engine).get_columns("user")}
            self.assertTrue({"phone", "address", "email_verified", "tipo", "documento", "desconto", "prazos"} <= colunas)
            self.assertTrue({"qtd_minima", "multiplo"} <= {c["name"] for c in inspect(db.engine).get_columns("product")})
            self.assertTrue({"documento", "prazo"} <= {c["name"] for c in inspect(db.engine).get_columns("order")})
            self.assertEqual(db.session.execute(text('SELECT email_verified FROM "user"')).scalar(), 1)
            db.session.execute(text('DROP TABLE "order"'))
            db.session.execute(text('DROP TABLE product'))
            db.session.execute(text('DROP TABLE order_item'))
            db.session.execute(text('DROP TABLE "user"'))
            db.session.commit()


class TesteRecuperacaoDeSenha(BaseTeste):
    def test_email_inexistente_recebe_a_mesma_resposta_e_nada_e_enviado(self):
        c = self.cliente()
        r = self.post(c, "/esqueci-senha", {"email": "ninguem@teste.com"})
        self.assertIn("Se houver uma conta", c.get(r.headers["Location"]).get_data(as_text=True))
        self.assertEqual(self.emails, [])

    def test_fluxo_completo_e_link_de_uso_unico(self):
        self.cadastrar()
        limpar_limites()
        c = self.cliente()
        self.post(c, "/esqueci-senha", {"email": "cli@teste.com"})
        link = self.link_do_ultimo_email()
        self.assertEqual(c.get(link).status_code, 200)
        r = self.post(c, link, {"nova": "nova-senha-456", "nova2": "nova-senha-456"})
        self.assertIn("/entrar", r.headers["Location"])
        _, r = self.entrar(senha=SENHA)
        self.assertNotIn("/conta", r.headers.get("Location", ""), "a senha antiga não vale mais")
        _, r = self.entrar(senha="nova-senha-456")
        self.assertIn("/conta", r.headers["Location"])
        self.assertEqual(self.cliente().get(link).status_code, 302, "o mesmo link não pode ser usado de novo")

    def test_link_de_senha_vence_em_1_hora(self):
        self.cadastrar()
        limpar_limites()
        self.post(self.cliente(), "/esqueci-senha", {"email": "cli@teste.com"})
        link = self.link_do_ultimo_email()
        with mock.patch("time.time", return_value=time.time() + 2 * 3600):
            self.assertEqual(self.cliente().get(link).status_code, 302)

    def test_admin_nao_recupera_senha_por_email(self):
        self.post(self.cliente(), "/esqueci-senha", {"email": "admin@exemplo.com"})
        self.assertEqual(self.emails, [])

    def test_bloqueio_apos_5_senhas_erradas(self):
        self.cadastrar()
        for _ in range(5):
            self.entrar(senha="errada")
        _, r = self.entrar()
        self.assertIn("Muitas tentativas", r.get_data(as_text=True))


class TesteContaDoCliente(BaseTeste):
    def comprar(self, c):
        self.post(c, "/carrinho/add", {"pid": 1, "qty": 2})
        r = self.post(c, "/checkout", {"name": "Cli", "email": "cli@teste.com", "phone": "11", "address": "Rua 1", "cep": "01001000",
                                       "payment": "pix"}, "/checkout")
        self.assertEqual(r.status_code, 302)
        with app.app_context():
            return Order.query.order_by(Order.id.desc()).first().id

    def test_pedido_aparece_so_para_o_dono(self):
        c = self.cadastrar()
        oid = self.comprar(c)
        for url in ("/conta", "/conta/pedidos", f"/conta/pedidos/{oid}", "/conta/dados"):
            self.assertEqual(c.get(url).status_code, 200, url)
        outro = self.cadastrar("outro@teste.com")
        self.assertEqual(outro.get(f"/conta/pedidos/{oid}").status_code, 404)
        self.assertEqual(outro.get(f"/pedido/{oid}").status_code, 404)

    def test_cancelar_devolve_estoque_uma_vez_so(self):
        c = self.cadastrar()
        antes = self.estoque(1)
        oid = self.comprar(c)
        self.post(c, f"/conta/pedidos/{oid}/cancelar", {})
        self.post(c, f"/conta/pedidos/{oid}/cancelar", {})
        self.assertEqual(self.estoque(1), antes)

    def test_comprar_novamente_enche_o_carrinho(self):
        c = self.cadastrar()
        oid = self.comprar(c)
        self.post(c, f"/conta/pedidos/{oid}/repetir", {})
        with c.session_transaction() as s:
            self.assertEqual(s.get("cart"), {"1": 2})

    def test_trocar_senha_logado(self):
        c = self.cadastrar()
        self.post(c, "/conta/senha", {"atual": "errada", "nova": "outra-senha-1", "nova2": "outra-senha-1"}, "/conta/dados")
        self.post(c, "/conta/senha", {"atual": SENHA, "nova": "outra-senha-1", "nova2": "outra-senha-1"}, "/conta/dados")
        _, r = self.entrar(senha="outra-senha-1")
        self.assertIn("/conta", r.headers["Location"])


class TestePainel(BaseTeste):
    def test_cliente_e_visitante_nao_entram_no_painel(self):
        self.assertEqual(self.cliente().get("/admin/products").status_code, 302)
        c = self.cadastrar()
        self.assertEqual(c.get("/admin/products").status_code, 302)

    def test_editar_produto_valida_os_dados(self):
        a = self.admin()
        base = {"name": "Fresa Nova", "category_id": 1, "price": "210,50", "stock": 7, "description": "", "active": "on"}
        r = self.post(a, "/admin/products/1/edit", {**base, "image_url": "javascript:alert(1)"}, "/admin/products/1/edit")
        self.assertEqual(r.status_code, 200)
        r = self.post(a, "/admin/products/1/edit", {**base, "price": "-5", "image_url": ""}, "/admin/products/1/edit")
        self.assertEqual(r.status_code, 200)
        r = self.post(a, "/admin/products/1/edit", {**base, "image_url": ""}, "/admin/products/1/edit")
        self.assertEqual(r.status_code, 302)
        with app.app_context():
            p = db.session.get(Product, 1)
            self.assertEqual((p.name, float(p.price), p.stock), ("Fresa Nova", 210.5, 7))

    def test_categorias_e_filtro(self):
        a = self.admin()
        self.post(a, "/admin/categories", {"name": "Lubrificantes"}, "/admin/categories")
        r = self.post(a, "/admin/categories", {"name": " lubrificantes "}, "/admin/categories")
        self.assertIn("existe", a.get(r.headers["Location"]).get_data(as_text=True).lower())
        with app.app_context():
            nova = Category.query.filter_by(name="Lubrificantes").one().id
        self.post(a, "/admin/categories/1/delete", {}, "/admin/categories")
        self.post(a, f"/admin/categories/{nova}/delete", {}, "/admin/categories")
        with app.app_context():
            self.assertIsNotNone(db.session.get(Category, 1), "categoria com produtos não pode ser excluída")
            self.assertIsNone(db.session.get(Category, nova))
        html = a.get("/admin/products?cat=1").get_data(as_text=True)
        self.assertIn("Fresa de Topo", html)
        self.assertNotIn("Serra Fita", html)
        html2 = a.get("/admin/products?q=Paqu").get_data(as_text=True)
        self.assertIn("Paquímetro", html2, "busca por nome")
        self.assertNotIn("Fresa de Topo", html2)

    def test_renomear_categoria(self):
        a = self.admin()
        self.post(a, "/admin/categories/1/rename", {"name": "Usinagem CNC"}, "/admin/categories")
        with app.app_context():
            self.assertEqual(db.session.get(Category, 1).name, "Usinagem CNC")

    def test_painel_em_endereco_secreto(self):
        from config import _admin_path
        with mock.patch.dict(os.environ, {"ADMIN_PATH": "painel-xyz/"}):
            self.assertEqual(_admin_path(), "/painel-xyz")
        self.assertNotIn("/admin", self.cliente().get("/").get_data(as_text=True), "a loja não mostra o link do painel")


if __name__ == "__main__":
    unittest.main()
