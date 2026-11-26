"""Testes da página Minha conta do painel e do comando trocar-email-admin."""
import unittest

from test_site import ADMIN_SENHA, BaseTeste

from app import app  # noqa: E402
from models import User, db  # noqa: E402

NOVA = "outra-senha-forte-456"


class TesteContaAdmin(BaseTeste):
    def enviar(self, a, dados):
        return self.post(a, "/admin/conta", dados, "/admin/conta")

    def login(self, email, senha):
        c = self.cliente()
        r = self.post(c, "/admin/login", {"email": email, "password": senha}, "/admin/login")
        return r.status_code == 302 and r.headers["Location"].endswith("/admin/")

    def test_pagina_abre_e_aparece_no_menu(self):
        html = self.admin().get("/admin/conta").get_data(as_text=True)
        self.assertIn("Minha conta", html)
        self.assertIn("admin@exemplo.com", html)

    def test_trocar_email_e_entrar_com_ele(self):
        a = self.admin()
        self.enviar(a, {"acao": "dados", "nome": "Ana", "email": "Eu@Exemplo.com", "atual": ADMIN_SENHA})
        self.assertIn("Dados atualizados", a.get("/admin/conta").get_data(as_text=True))
        self.assertTrue(self.login("eu@exemplo.com", ADMIN_SENHA))
        self.assertFalse(self.login("admin@exemplo.com", ADMIN_SENHA))

    def test_trocar_senha(self):
        a = self.admin()
        self.enviar(a, {"acao": "senha", "atual": ADMIN_SENHA, "nova": NOVA, "nova2": NOVA})
        self.assertIn("Senha alterada", a.get("/admin/conta").get_data(as_text=True))
        self.assertTrue(self.login("admin@exemplo.com", NOVA))
        self.assertFalse(self.login("admin@exemplo.com", ADMIN_SENHA))

    def test_exige_senha_atual_e_valida(self):
        a = self.admin()
        casos = [
            ({"acao": "senha", "atual": "errada", "nova": NOVA, "nova2": NOVA}, "senha atual está incorreta"),
            ({"acao": "senha", "atual": ADMIN_SENHA, "nova": "curta", "nova2": "curta"}, "entre 12 e 128"),
            ({"acao": "senha", "atual": ADMIN_SENHA, "nova": NOVA, "nova2": NOVA + "x"}, "não coincidem"),
            ({"acao": "dados", "atual": ADMIN_SENHA, "nome": "X", "email": "sem-arroba"}, "e-mail válidos"),
        ]
        for dados, msg in casos:
            self.enviar(a, dados)
            self.assertIn(msg, a.get("/admin/conta").get_data(as_text=True))
        self.assertTrue(self.login("admin@exemplo.com", ADMIN_SENHA))  # nada mudou

    def test_email_de_cliente_nao_pode_ser_usado(self):
        self.cadastrar("cli@teste.com")
        a = self.admin()
        self.enviar(a, {"acao": "dados", "nome": "Admin", "email": "cli@teste.com", "atual": ADMIN_SENHA})
        self.assertIn("já é usado por outra conta", a.get("/admin/conta").get_data(as_text=True))

    def test_cliente_nao_abre(self):
        c = self.cadastrar()
        self.assertEqual(c.get("/admin/conta").status_code, 302)


class TesteComandoEmail(BaseTeste):
    def rodar(self, *args):
        return app.test_cli_runner().invoke(args=["trocar-email-admin", *args]).output

    def test_troca_o_email(self):
        self.assertIn("trocado", self.rodar("Novo@Exemplo.com"))
        with app.app_context():
            self.assertTrue(User.query.filter_by(email="novo@exemplo.com", is_admin=True).first())

    def test_recusa_email_de_cliente_e_admin_inexistente(self):
        self.cadastrar("cli@teste.com")
        self.assertIn("já é usado", self.rodar("cli@teste.com"))
        self.assertIn("não encontrado", self.rodar("--email", "ninguem@x.com", "a@b.com"))


if __name__ == "__main__":
    unittest.main()
