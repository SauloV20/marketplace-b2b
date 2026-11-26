"""Testes das correções de sessão, estoque atômico e bloqueio de login.

Rode na pasta do projeto:  python -m unittest discover -s tests -v
"""
import time
import unittest
from unittest import mock

from test_site import ADMIN_SENHA, BaseTeste, novo_cpf  # prepara o ambiente de teste (SQLite em memória)

import blueprints.shop as shop  # noqa: E402
from app import app  # noqa: E402
from models import LoginFalha, Order, Product, db, mudar_status  # noqa: E402

DADOS = {"name": "Ana", "email": "ana@ex.com", "phone": "11999", "address": "Rua A, 1", "cep": "01001-000", "payment": "pix"}


class BaseCorrecoes(BaseTeste):
    def comprador(self):
        return self.cadastrar(f"comprador{novo_cpf()}@teste.com")

    def comprar(self, c, pid, qtd=1):
        self.post(c, "/carrinho/add", {"pid": pid, "qty": qtd})
        return self.post(c, "/checkout", DADOS, "/checkout")

    def oid(self, r):
        return int(r.headers["Location"].rsplit("/", 1)[1])

    def definir_estoque(self, pid, qtd):
        with app.app_context():
            db.session.get(Product, pid).stock = qtd
            db.session.commit()


class TesteSessao(BaseCorrecoes):
    def test_abrir_o_painel_nao_desloga_o_cliente_nem_esvazia_o_carrinho(self):
        c = self.cadastrar()
        self.post(c, "/carrinho/add", {"pid": 1, "qty": 1})
        self.assertEqual(c.get("/admin/").status_code, 302)
        html = c.get("/").get_data(as_text=True)
        self.assertIn("Minha conta", html)
        self.assertIn("Carrinho, 1 itens", html)

    def test_sair_do_painel_so_por_post(self):
        a = self.admin()
        self.assertEqual(a.get("/admin/logout").status_code, 405)  # um link externo não desloga o admin
        self.assertEqual(self.post(a, "/admin/logout", {}, "/admin/").status_code, 302)
        self.assertEqual(a.get("/admin/").status_code, 302)

    def test_sair_da_conta_esquece_os_pedidos_recentes(self):
        c = self.cadastrar()
        r = self.comprar(c, 1)
        self.post(c, "/sair", {})
        self.assertEqual(c.get(r.headers["Location"]).status_code, 404)

    def test_pedido_sem_forma_de_pagamento_abre(self):
        c = self.comprador()
        r = self.comprar(c, 1)
        with app.app_context():
            db.session.get(Order, self.oid(r)).payment_method = ""
            db.session.commit()
        self.assertEqual(c.get(r.headers["Location"]).status_code, 200)


class TesteEstoqueAtomico(BaseCorrecoes):
    def test_ultima_unidade_vai_para_um_comprador_so(self):
        self.definir_estoque(2, 1)
        a, b = self.comprador(), self.comprador()
        self.post(a, "/carrinho/add", {"pid": 2, "qty": 1})
        self.post(b, "/carrinho/add", {"pid": 2, "qty": 1})
        with app.app_context():
            antes = Order.query.count()
        self.assertIn("/pedido/", self.post(a, "/checkout", DADOS, "/checkout").headers["Location"])
        self.assertNotIn("/pedido/", self.post(b, "/checkout", DADOS).headers.get("Location", ""))
        with app.app_context():
            self.assertEqual(Order.query.count(), antes + 1)
        self.assertEqual(self.estoque(2), 0)

    def test_checkout_desiste_se_o_estoque_acabar_no_meio(self):
        c = self.comprador()
        self.post(c, "/carrinho/add", {"pid": 3, "qty": 1})
        antes = self.estoque(3)
        with app.app_context():
            pedidos = Order.query.count()
        with mock.patch.object(shop, "baixar_estoque", return_value=False):  # outro comprador levou antes
            r = self.post(c, "/checkout", DADOS, "/checkout")
        self.assertTrue(r.headers["Location"].endswith("/carrinho"))
        self.assertIn("O estoque mudou", c.get("/carrinho").get_data(as_text=True))
        with app.app_context():
            self.assertEqual(Order.query.count(), pedidos)
        self.assertEqual(self.estoque(3), antes)

    def test_status_desatualizado_nao_devolve_estoque_de_novo(self):
        oid = self.oid(self.comprar(self.comprador(), 4, 2))
        with app.app_context():
            pedido = db.session.get(Order, oid)
            self.assertEqual(pedido.status, "pendente")
            # outra pessoa cancela no mesmo instante (o objeto em memória ainda diz "pendente")
            db.session.execute(db.update(Order).where(Order.id == oid).values(status="cancelado")
                               .execution_options(synchronize_session=False))
            antes = db.session.get(Product, 4).stock
            ok, erro = mudar_status(pedido, "cancelado")
            self.assertFalse(ok)
            self.assertIn("alterado", erro)
            db.session.expire_all()
            self.assertEqual(db.session.get(Product, 4).stock, antes)

    def test_reativar_pedido_sem_estoque_e_recusado(self):
        a = self.admin()
        oid = self.oid(self.comprar(self.comprador(), 5, 1))
        self.post(a, f"/admin/orders/{oid}/status", {"status": "cancelado"}, "/admin/orders")
        self.definir_estoque(5, 0)
        self.post(a, f"/admin/orders/{oid}/status", {"status": "pago"}, "/admin/orders")
        self.assertIn("Sem estoque para este pedido", a.get("/admin/orders").get_data(as_text=True))
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).status, "cancelado")
        self.assertEqual(self.estoque(5), 0)


class TesteBloqueioNoBanco(BaseCorrecoes):
    def errar(self, c, vezes):
        for _ in range(vezes):
            self.post(c, "/admin/login", {"email": "admin@exemplo.com", "password": "errada"}, "/admin/login")

    def test_bloqueio_fica_no_banco_e_vale_para_outro_processo(self):
        self.errar(self.cliente(), 5)
        with app.app_context():
            self.assertEqual(LoginFalha.query.count(), 1)
        # outro navegador do mesmo IP (como se caísse em outro worker): continua bloqueado
        r = self.post(self.cliente(), "/admin/login", {"email": "admin@exemplo.com", "password": ADMIN_SENHA},
                      "/admin/login")
        self.assertIn("Muitas tentativas", r.get_data(as_text=True))

    def test_login_certo_zera_as_falhas(self):
        self.errar(self.cliente(), 3)
        self.admin()
        with app.app_context():
            self.assertEqual(LoginFalha.query.count(), 0)

    def test_erros_espalhados_no_tempo_nao_bloqueiam(self):
        c = self.cliente()
        self.errar(c, 4)
        with app.app_context():  # os 4 erros foram há mais de 15 minutos
            db.session.execute(db.update(LoginFalha).values(ultima=int(time.time()) - 3600))
            db.session.commit()
        self.errar(c, 1)
        self.admin()  # não ficou bloqueado


if __name__ == "__main__":
    unittest.main()
