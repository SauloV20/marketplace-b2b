"""Testes da LGPD: páginas legais, aceite no cadastro, exportação e exclusão da conta pelo titular."""
import json
import unittest

from test_b2b import BaseB2B
from test_site import SENHA, novo_cpf

from app import app  # noqa: E402
import lgpd  # noqa: E402
from models import Order, User, db  # noqa: E402


class TesteLgpd(BaseB2B):
    def usuario(self, email="cli@teste.com"):
        with app.app_context():
            return User.query.filter_by(email=email).first()

    def test_paginas_legais_e_rodape(self):
        c = self.cliente()
        for url, titulo in (("/privacidade", "Política de Privacidade"), ("/termos", "Termos de Uso")):
            html = c.get(url).get_data(as_text=True)
            self.assertIn(titulo, html)
        html = c.get("/").get_data(as_text=True)
        self.assertIn('href="/privacidade"', html)
        self.assertNotIn('href="/privacidade"', self.admin().get("/admin/").get_data(as_text=True))

    def test_dados_da_empresa_aparecem_na_politica(self):
        a = self.admin()
        self.post(a, "/admin/regras", {"pedido_minimo": "0", "empresa_razao_social": "Marketplace B2B Comércio Ltda",
                                       "empresa_cnpj": "11.222.333/0001-81", "empresa_endereco": "Rua X, 1",
                                       "empresa_email_privacidade": "privacidade@loja-exemplo.com"}, "/admin/regras")
        html = self.cliente().get("/privacidade").get_data(as_text=True)
        self.assertIn("Marketplace B2B Comércio Ltda, CNPJ 11.222.333/0001-81", html)
        self.assertIn("privacidade@loja-exemplo.com", html)
        r = self.post(a, "/admin/regras", {"pedido_minimo": "0", "empresa_cnpj": "123"}, "/admin/regras")
        self.assertIn("CNPJ da empresa é inválido", a.get("/admin/regras").get_data(as_text=True))

    def test_cadastro_exige_aceite_e_registra_a_versao(self):
        c = self.cliente()
        r = self.post(c, "/cadastro", {"nome": "Ana", "email": "ana@ex.com", "senha": SENHA, "senha2": SENHA,
                                       "tipo": "pf", "documento": novo_cpf()})
        self.assertIn("aceitar a Política de Privacidade", r.get_data(as_text=True))
        self.assertIsNone(self.usuario("ana@ex.com"))
        self.cadastrar()
        u = self.usuario()
        self.assertEqual(u.termos_versao, lgpd.VERSAO_TERMOS)
        self.assertIsNotNone(u.termos_aceitos_em)

    def test_baixar_meus_dados(self):
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        r = c.get("/conta/meus-dados.json")
        self.assertIn("attachment", r.headers["Content-Disposition"])
        d = json.loads(r.data)
        self.assertEqual(d["conta"]["email"], "cli@teste.com")
        self.assertEqual(len(d["pedidos"]), 1)
        self.assertIn("Fresa de Topo", d["pedidos"][0]["itens"][0]["produto"])
        self.assertEqual(self.cliente().get("/conta/meus-dados.json").status_code, 302)

    def test_cliente_exclui_a_propria_conta(self):
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "orcamento")
        oid = self.ultimo_pedido()[0]
        self.post(c, "/conta/excluir", {"senha": SENHA}, "/conta/privacidade")
        self.assertIsNotNone(self.usuario(), "com orçamento em aberto não exclui")
        self.post(c, f"/conta/pedidos/{oid}/cancelar", {})
        self.post(c, "/conta/excluir", {"senha": "errada"}, "/conta/privacidade")
        self.assertIsNotNone(self.usuario(), "senha errada não exclui")
        r = self.post(c, "/conta/excluir", {"senha": SENHA}, "/conta/privacidade")
        self.assertTrue(r.headers["Location"].endswith("/"))
        self.assertIsNone(self.usuario())
        with app.app_context():
            self.assertIsNone(db.session.get(Order, oid).user_id)
        self.assertEqual(c.get("/conta").status_code, 302)


if __name__ == "__main__":
    unittest.main()
