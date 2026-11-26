"""Testes da busca/páginas da lista de pedidos e da verificação em duas etapas do painel."""
import re
import time
import unittest
from datetime import timedelta

from test_b2b import BaseB2B
from test_site import ADMIN_SENHA

from app import app  # noqa: E402
import dois_fatores  # noqa: E402
from models import Order, User, agora_utc, db  # noqa: E402


class TesteBuscaPedidos(BaseB2B):
    def novo_pedido(self, c, pagamento="pix"):
        self.adicionar(c, 1, 1)
        self.fechar(c, pagamento)
        with app.app_context():
            return Order.query.order_by(Order.id.desc()).first().id

    def ids(self, html):
        return {int(x) for x in re.findall(r'name="oid" value="(\d+)"', html)}

    def test_busca_por_numero_nome_documento_e_email(self):
        c = self.empresa()
        oid = self.novo_pedido(c)
        with app.app_context():
            o = db.session.get(Order, oid)
            doc = o.documento
        a = self.admin()
        self.assertEqual(self.ids(a.get(f"/admin/orders?q=%23{oid}").get_data(as_text=True)), {oid})
        self.assertIn(oid, self.ids(a.get("/admin/orders?q=metal%C3%BArgica").get_data(as_text=True)))
        cnpj = f"{doc[:2]}.{doc[2:5]}.{doc[5:8]}/{doc[8:12]}-{doc[12:]}"
        self.assertEqual(self.ids(a.get("/admin/orders", query_string={"q": cnpj}).get_data(as_text=True)), {oid})
        self.assertEqual(self.ids(a.get("/admin/orders?q=compras@ex.com").get_data(as_text=True)) & {oid}, {oid})
        html = a.get("/admin/orders?q=ninguem-assim").get_data(as_text=True)
        self.assertIn("Nenhum pedido encontrado com essa busca", html)
        self.assertEqual(a.get("/admin/orders?q=99999999999999999999").status_code, 200, "número enorme não quebra")

    def test_periodo_status_e_paginas(self):
        c = self.cadastrar()
        novo = self.novo_pedido(c)
        a = self.admin()
        with app.app_context():
            total = Order.query.count()
            db.session.get(Order, novo).created_at = agora_utc() - timedelta(days=400)
            db.session.commit()
            antigo = db.session.get(Order, novo).created_at
        dia = (antigo - timedelta(hours=3)).date().isoformat()
        html = a.get(f"/admin/orders?de={dia}&ate={dia}").get_data(as_text=True)
        self.assertEqual(self.ids(html), {novo})
        html = a.get("/admin/orders").get_data(as_text=True)
        self.assertIn(f"{total} pedido(s)", html)
        self.assertEqual(len(self.ids(html)), 50)
        self.assertIn("página 1 de", html)
        pag2 = a.get("/admin/orders?pagina=2").get_data(as_text=True)
        self.assertTrue(self.ids(pag2).isdisjoint(self.ids(html)))
        self.assertEqual(a.get("/admin/orders?pagina=999").status_code, 200)
        pendentes = a.get("/admin/orders?status=pendente&q=%23" + str(novo)).get_data(as_text=True)
        self.assertEqual(self.ids(pendentes), {novo})
        self.assertIn("status=pendente", pendentes)

    def test_excluir_volta_para_a_mesma_busca(self):
        c = self.cadastrar()
        oid = self.novo_pedido(c)
        a = self.admin()
        r = self.post(a, "/admin/orders/excluir", {"oid": str(oid), "voltar": "/admin/orders?q=abc&pagina=2"},
                      "/admin/orders")
        self.assertTrue(r.headers["Location"].endswith("/admin/orders?q=abc&pagina=2"))
        oid = self.novo_pedido(c)
        r = self.post(a, "/admin/orders/excluir", {"oid": str(oid), "voltar": "https://malicioso.exemplo/"}, "/admin/orders")
        self.assertTrue(r.headers["Location"].endswith("/admin/orders"))


class TesteDuasEtapas(BaseB2B):
    def setUp(self):
        super().setUp()
        self.addCleanup(app.config.update, ADMIN_2FA_DESLIGADO=False)

    def codigo_agora(self, segredo, deslocamento=0):
        return dois_fatores.codigo(segredo, int(time.time() // 30) + deslocamento)

    def ativar(self, a):
        self.post(a, "/admin/conta/duas-etapas", {"acao": "iniciar", "atual": ADMIN_SENHA}, "/admin/conta")
        with a.session_transaction() as s:
            segredo = s["2fa_novo"]
        r = self.post(a, "/admin/conta/duas-etapas", {"acao": "confirmar", "codigo": self.codigo_agora(segredo)},
                      "/admin/conta")
        codigos = re.findall(r"<code>([0-9a-f]{6}-[0-9a-f]{6})</code>", r.get_data(as_text=True))
        return segredo, codigos

    def entrar_so_senha(self):
        c = self.cliente()
        r = self.post(c, "/admin/login", {"email": "admin@exemplo.com", "password": ADMIN_SENHA}, "/admin/login")
        return c, r

    def test_rfc6238(self):
        segredo = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890" do RFC 6238
        self.assertEqual(dois_fatores.codigo(segredo, 59 // 30), "287082")
        self.assertEqual(dois_fatores.codigo(segredo, 1111111109 // 30), "081804")
        self.assertEqual(dois_fatores.conferir(segredo, "287 082", 0, agora=59), 1)
        self.assertIsNone(dois_fatores.conferir(segredo, "287082", 1, agora=59), "não vale duas vezes")

    def test_ativar_e_entrar_com_codigo(self):
        a = self.admin()
        html = a.get("/admin/conta").get_data(as_text=True)
        self.assertIn("Ativar verificação em duas etapas", html)
        segredo, codigos = self.ativar(a)
        self.assertEqual(len(codigos), 8)
        with app.app_context():
            u = User.query.filter_by(email="admin@exemplo.com").one()
            self.assertEqual(u.totp_segredo, segredo)
            self.assertNotIn(codigos[0], u.codigos_recuperacao, "guarda só os hashes")

        c, r = self.entrar_so_senha()
        self.assertIn("/admin/login/codigo", r.headers["Location"])
        self.assertIn("/admin/login", c.get("/admin/").headers["Location"], "só a senha não entra")
        r = self.post(c, "/admin/login/codigo", {"codigo": "000000"}, "/admin/login/codigo")
        self.assertIn("Código incorreto", r.get_data(as_text=True))
        r = self.post(c, "/admin/login/codigo", {"codigo": self.codigo_agora(segredo, 1)}, "/admin/login/codigo")
        self.assertIn("/admin/", r.headers["Location"])
        self.assertEqual(c.get("/admin/").status_code, 200)

        # o mesmo código não serve num segundo login (nem um anterior a ele)
        c2, _ = self.entrar_so_senha()
        r = self.post(c2, "/admin/login/codigo", {"codigo": self.codigo_agora(segredo, 1)}, "/admin/login/codigo")
        self.assertEqual(r.status_code, 200)

    def test_codigo_de_recuperacao_vale_uma_vez(self):
        _, codigos = self.ativar(self.admin())
        c, _ = self.entrar_so_senha()
        r = self.post(c, "/admin/login/codigo", {"codigo": codigos[0].upper()}, "/admin/login/codigo")
        self.assertEqual(r.status_code, 302)
        self.assertIn("restam 7", c.get("/admin/").get_data(as_text=True))
        c2, _ = self.entrar_so_senha()
        r = self.post(c2, "/admin/login/codigo", {"codigo": codigos[0]}, "/admin/login/codigo")
        self.assertEqual(r.status_code, 200)

    def test_segunda_etapa_expira_e_bloqueia(self):
        segredo, _ = self.ativar(self.admin())
        c, _ = self.entrar_so_senha()
        with c.session_transaction() as s:
            s["admin_2fa"] = {**s["admin_2fa"], "ate": int(time.time()) - 1}
        r = c.get("/admin/login/codigo")
        self.assertIn("/admin/login", r.headers["Location"])
        c, _ = self.entrar_so_senha()
        for _ in range(5):
            self.post(c, "/admin/login/codigo", {"codigo": "123456"}, "/admin/login/codigo")
        r = self.post(c, "/admin/login/codigo", {"codigo": self.codigo_agora(segredo, 1)}, "/admin/login/codigo")
        self.assertIn("/admin/login", r.headers["Location"], "bloqueado: nem o código certo entra")
        self.assertNotIn("/codigo", r.headers["Location"])

    def test_variavel_de_emergencia_e_desativar(self):
        a = self.admin()
        self.ativar(a)
        app.config["ADMIN_2FA_DESLIGADO"] = True
        c, r = self.entrar_so_senha()
        self.assertTrue(r.headers["Location"].endswith("/admin/"))
        self.assertIn("ADMIN_2FA_DESLIGADO", c.get("/admin/").get_data(as_text=True))
        self.post(c, "/admin/conta/duas-etapas", {"acao": "desativar", "atual": ADMIN_SENHA}, "/admin/conta")
        with app.app_context():
            self.assertEqual(User.query.filter_by(email="admin@exemplo.com").one().totp_segredo, "")

    def test_desativar_exige_senha_e_codigo(self):
        a = self.admin()
        segredo, _ = self.ativar(a)
        self.post(a, "/admin/conta/duas-etapas", {"acao": "desativar", "atual": "errada", "codigo": ""}, "/admin/conta")
        self.post(a, "/admin/conta/duas-etapas", {"acao": "desativar", "atual": ADMIN_SENHA, "codigo": "000000"},
                  "/admin/conta")
        with app.app_context():
            self.assertEqual(User.query.filter_by(email="admin@exemplo.com").one().totp_segredo, segredo)
        self.post(a, "/admin/conta/duas-etapas", {"acao": "desativar", "atual": ADMIN_SENHA,
                                                  "codigo": self.codigo_agora(segredo, 1)}, "/admin/conta")
        with app.app_context():
            self.assertEqual(User.query.filter_by(email="admin@exemplo.com").one().totp_segredo, "")

    def test_confirmacao_errada_nao_ativa(self):
        a = self.admin()
        self.post(a, "/admin/conta/duas-etapas", {"acao": "iniciar", "atual": ADMIN_SENHA}, "/admin/conta")
        html = a.get("/admin/conta").get_data(as_text=True)
        self.assertIn("<svg", html)
        self.post(a, "/admin/conta/duas-etapas", {"acao": "confirmar", "codigo": "000000"}, "/admin/conta")
        with app.app_context():
            self.assertEqual(User.query.filter_by(email="admin@exemplo.com").one().totp_segredo, "")


if __name__ == "__main__":
    unittest.main()
