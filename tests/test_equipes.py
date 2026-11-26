"""Testes dos vários administradores com permissões e dos usuários da empresa cliente com aprovação."""
import re
import unittest
from decimal import Decimal

from test_b2b import BaseB2B
from test_site import ADMIN_SENHA

from app import app  # noqa: E402
import permissoes  # noqa: E402
from models import Order, PedidoEvento, User, db  # noqa: E402

SENHA_NOVA = "senha-do-painel-123"


class TestePainelEquipe(BaseB2B):
    def convidar(self, a, nome, email, **areas):
        dados = {"acao": "convidar", "nome": nome, "email": email, "acesso": "areas",
                 **{f"area_{k}": "on" for k, v in areas.items() if v}}
        return self.post(a, "/admin/equipe", dados, "/admin/equipe")

    def aceitar(self, email):
        link = next(re.search(r"https?://\S+", e["texto"]).group(0) for e in reversed(self.emails) if e["para"] == email)
        c = self.cliente()
        caminho = link.split("localhost", 1)[-1]
        self.post(c, caminho, {"nova": SENHA_NOVA, "nova2": SENHA_NOVA}, caminho)
        r = self.post(c, "/admin/login", {"email": email, "password": SENHA_NOVA}, "/admin/login")
        self.assertEqual(r.status_code, 302, "entrou com a senha criada pelo convite")
        return c

    def test_todas_as_telas_tem_area_ou_sao_livres(self):
        telas = {r.endpoint for r in app.url_map.iter_rules() if r.endpoint.startswith("admin.")}
        sem_regra = telas - permissoes.LIVRES - set(permissoes.AREA_DA_TELA)
        # as que sobram são só para acesso total (de propósito)
        self.assertEqual(sem_regra, {"admin.backup_automatico", "admin.backup_baixar", "admin.backup_pagina", "admin.backup_restaurar",
                                     "admin.comprimir_fotos", "admin.equipe", "admin.frete_excluir",
                                     "admin.frete_lista", "admin.limpar_exemplos", "admin.orders_excluir",
                                     "admin.regras"})

    def test_vendedor_so_ve_pedidos(self):
        a = self.admin()
        self.convidar(a, "Vendedor", "vendedor@loja-exemplo.com", pedidos=True)
        self.assertIn("Convite enviado", a.get("/admin/equipe").get_data(as_text=True))
        v = self.aceitar("vendedor@loja-exemplo.com")
        r = v.get("/admin/")
        self.assertIn("/admin/orders", r.headers["Location"], "sem Vendas: vai para Pedidos")
        html = v.get("/admin/orders").get_data(as_text=True)
        self.assertIn(">Pedidos</a>", html)
        self.assertNotIn(">Produtos</a>", html)
        self.assertNotIn("Excluir selecionados", html)
        for tela in ("/admin/products", "/admin/backup", "/admin/equipe", "/admin/regras", "/admin/clientes"):
            self.assertEqual(v.get(tela).status_code, 302, tela)
        r = self.post(v, "/admin/orders/excluir", {"oid": "1"}, "/admin/orders")
        self.assertEqual(r.status_code, 403)
        with app.app_context():
            self.assertIsNotNone(db.session.get(Order, 1), "não excluiu")

    def test_avisos_vao_para_quem_cuida(self):
        a = self.admin()
        self.convidar(a, "Estoquista", "estoque@loja-exemplo.com", produtos=True)
        self.aceitar("estoque@loja-exemplo.com")
        del self.emails[:]
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        para = {e["para"] for e in self.emails if "Novo pedido" in e["assunto"]}
        self.assertEqual(para, {"admin@exemplo.com"}, "o estoquista não recebe aviso de pedido")

    def test_dono_muda_acesso_e_remove(self):
        a = self.admin()
        self.convidar(a, "Ana", "ana@loja-exemplo.com", vendas=True)
        with app.app_context():
            uid = User.query.filter_by(email="ana@loja-exemplo.com").one().id
        self.post(a, "/admin/equipe", {"acao": "salvar", "uid": str(uid), "acesso": "areas", "area_clientes": "on"},
                  "/admin/equipe")
        with app.app_context():
            self.assertEqual(permissoes.areas(db.session.get(User, uid)), {"clientes"})
        # o único dono não perde o acesso total
        with app.app_context():
            dono = User.query.filter_by(email="admin@exemplo.com").one().id
        self.post(a, "/admin/equipe", {"acao": "salvar", "uid": str(dono), "acesso": "areas"}, "/admin/equipe")
        with app.app_context():
            self.assertTrue(permissoes.total(db.session.get(User, dono)))
        self.post(a, "/admin/equipe", {"acao": "remover", "uid": str(uid)}, "/admin/equipe")
        with app.app_context():
            self.assertIsNone(db.session.get(User, uid))

    def test_convite_vale_uma_vez(self):
        a = self.admin()
        self.convidar(a, "Bia", "bia@loja-exemplo.com", pedidos=True)
        link = re.search(r"https?://\S+", self.emails[-1]["texto"]).group(0).split("localhost", 1)[-1]
        self.aceitar("bia@loja-exemplo.com")
        r = self.cliente().get(link)
        self.assertIn("/admin/login", r.headers["Location"])


class TesteEquipeEmpresa(BaseB2B):
    def setUp(self):
        super().setUp()
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)

    def convidar(self, titular, nome, email, papel="comprador", limite=""):
        return self.post(titular, "/conta/equipe", {"acao": "convidar", "nome": nome, "email": email, "papel": papel,
                                                    "limite": limite}, "/conta/equipe")

    def aceitar(self, email):
        link = next(re.search(r"https?://\S+", e["texto"]).group(0) for e in reversed(self.emails) if e["para"] == email)
        c = self.cliente()
        caminho = link.split("localhost", 1)[-1]
        self.post(c, caminho, {"nova": "senha-membro-1", "nova2": "senha-membro-1"}, caminho)
        r = self.post(c, "/entrar", {"email": email, "senha": "senha-membro-1"}, "/entrar")
        self.assertEqual(r.status_code, 302)
        return c

    def test_comprador_precisa_de_aprovacao(self):
        titular = self.empresa()
        self.convidar(titular, "Carlos Comprador", "carlos@empresa.com")
        self.assertIn("Convite enviado", titular.get("/conta/equipe").get_data(as_text=True))
        carlos = self.aceitar("carlos@empresa.com")
        self.assertIn("Comprando pela conta da", carlos.get("/conta").get_data(as_text=True))
        self.adicionar(carlos, 1, 3)
        del self.emails[:]
        r = self.fechar(carlos, "pix")
        self.assertIn("/conta/pedidos/", r.headers["Location"])
        with app.app_context():
            o = Order.query.order_by(Order.id.desc()).first()
            oid, uid_titular = o.id, User.query.filter_by(email="empresa@teste.com").one().id
            self.assertEqual((o.status, o.comprador_nome, o.user_id), ("aprovacao", "Carlos Comprador", uid_titular))
        self.assertEqual(self.estoque(1), 50, "aguardando aprovação não prende estoque")
        self.assertTrue(any(e["para"] == "empresa@teste.com" and "aguardando sua aprovação" in e["assunto"]
                            for e in self.emails))
        self.assertFalse(any(e["para"] == "admin@exemplo.com" for e in self.emails), "a loja só fica sabendo depois")

        # o comprador não aprova; o titular aprova
        self.post(carlos, f"/conta/pedidos/{oid}/aprovacao", {"acao": "aprovar"}, f"/conta/pedidos/{oid}")
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).status, "aprovacao")
        html = titular.get("/conta").get_data(as_text=True)
        self.assertIn("Aguardando sua aprovação (1)", html)
        self.post(titular, f"/conta/pedidos/{oid}/aprovacao", {"acao": "aprovar"}, f"/conta/pedidos/{oid}")
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).status, "pendente")
            textos = [e.texto for e in PedidoEvento.query.filter_by(order_id=oid)]
        self.assertEqual(self.estoque(1), 47)
        self.assertTrue(any(e["para"] == "admin@exemplo.com" and "Novo pedido" in e["assunto"] for e in self.emails))
        self.assertTrue(any("Aprovado por" in t for t in textos))

    def orcamento_aprovado_pela_loja(self, c, qtd=3, preco="150,00"):
        """O membro pede orçamento; o Marketplace B2B define o preço e aprova. Devolve o id do pedido."""
        self.adicionar(c, 1, qtd)
        self.fechar(c, "orcamento")
        with app.app_context():
            o = Order.query.order_by(Order.id.desc()).first()
            oid, item = o.id, o.items[0].id
            self.assertEqual(o.status, "orcamento", "pedir orçamento não passa pela empresa (ainda sem preço)")
        a = self.admin()
        self.post(a, f"/admin/orders/{oid}/orcamento", {"acao": "aprovar", f"preco_{item}": preco, "pagamento": "pix"},
                  f"/admin/orders/{oid}/orcamento")
        return oid

    def test_orcamento_do_comprador_acima_do_limite_passa_pela_empresa(self):
        titular = self.empresa()
        self.convidar(titular, "Carlos Comprador", "carlos@empresa.com", limite="100,00")
        carlos = self.aceitar("carlos@empresa.com")
        del self.emails[:]
        oid = self.orcamento_aprovado_pela_loja(carlos)  # 3 x 150 = 450, acima do limite de 100
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).status, "aprovacao")
        self.assertEqual(self.estoque(1), 50, "aguardando a empresa não prende estoque")
        self.assertTrue(any(e["para"] == "empresa@teste.com" and "aguardando sua aprovação" in e["assunto"]
                            for e in self.emails))
        aviso = next(e for e in self.emails if "orçamento" in e["assunto"] and "aprovado" in e["assunto"])
        self.assertIn("aprovação da sua empresa", aviso["texto"])
        self.assertNotIn("Veja como pagar", aviso["texto"], "ainda não é para pagar")
        # o comprador não aprova sozinho; o titular aprova e aí vira pedido
        self.post(carlos, f"/conta/pedidos/{oid}/aprovacao", {"acao": "aprovar"}, f"/conta/pedidos/{oid}")
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).status, "aprovacao")
        self.post(titular, f"/conta/pedidos/{oid}/aprovacao", {"acao": "aprovar"}, f"/conta/pedidos/{oid}")
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).status, "pendente")
            textos = [e.texto for e in PedidoEvento.query.filter_by(order_id=oid)]
        self.assertEqual(self.estoque(1), 47)
        self.assertTrue(any("aguardando aprovação da empresa" in t for t in textos))
        self.assertTrue(any("Aprovado por" in t for t in textos))

    def test_orcamento_dentro_do_limite_ou_do_titular_vira_pedido_direto(self):
        titular = self.empresa()
        self.convidar(titular, "Dani", "dani@empresa.com", limite="100000,00")
        dani = self.aceitar("dani@empresa.com")
        oid = self.orcamento_aprovado_pela_loja(dani)
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).status, "pendente", "dentro do limite: direto")
        oid = self.orcamento_aprovado_pela_loja(titular)
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).status, "pendente", "o titular não precisa de aprovação")
        self.convidar(titular, "Edu", "edu@empresa.com", papel="aprovador")
        edu = self.aceitar("edu@empresa.com")
        oid = self.orcamento_aprovado_pela_loja(edu)
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).status, "pendente", "aprovador também não precisa")

    def test_orcamento_de_comprador_que_saiu_da_equipe_passa_pela_empresa(self):
        titular = self.empresa()
        self.convidar(titular, "Gabi", "gabi@empresa.com", limite="100000,00")
        gabi = self.aceitar("gabi@empresa.com")
        self.adicionar(gabi, 1, 3)
        self.fechar(gabi, "orcamento")
        with app.app_context():
            o = Order.query.order_by(Order.id.desc()).first()
            oid, item = o.id, o.items[0].id
            uid = User.query.filter_by(email="gabi@empresa.com").one().id
        self.post(titular, "/conta/equipe", {"acao": "remover", "uid": str(uid)}, "/conta/equipe")
        self.post(self.admin(), f"/admin/orders/{oid}/orcamento",
                  {"acao": "aprovar", f"preco_{item}": "150,00", "pagamento": "pix"}, f"/admin/orders/{oid}/orcamento")
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).status, "aprovacao", "sem o comprador, a empresa decide")

    def test_limite_aprovador_e_recusa(self):
        titular = self.empresa()
        self.convidar(titular, "Dani", "dani@empresa.com", limite="100000,00")
        dani = self.aceitar("dani@empresa.com")
        self.adicionar(dani, 1, 1)
        self.fechar(dani, "pix")
        with app.app_context():
            self.assertEqual(Order.query.order_by(Order.id.desc()).first().status, "pendente", "abaixo do limite: direto")
        self.convidar(titular, "Edu", "edu@empresa.com", papel="aprovador")
        edu = self.aceitar("edu@empresa.com")
        self.convidar(titular, "Fabi", "fabi@empresa.com")
        fabi = self.aceitar("fabi@empresa.com")
        self.adicionar(fabi, 1, 2)
        self.fechar(fabi, "boleto", email="fabi@empresa.com")
        with app.app_context():
            oid = Order.query.order_by(Order.id.desc()).first().id
        self.post(edu, f"/conta/pedidos/{oid}/aprovacao", {"acao": "recusar"}, f"/conta/pedidos/{oid}")
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).status, "cancelado")
        self.assertTrue(any(e["para"] == "fabi@empresa.com" and "não aprovado" in e["assunto"] for e in self.emails))
        # aprovador compra direto
        self.adicionar(edu, 1, 1)
        self.fechar(edu, "pix")
        with app.app_context():
            self.assertEqual(Order.query.order_by(Order.id.desc()).first().status, "pendente")

    def test_membro_nao_mexe_na_empresa_e_remocao_encerra_sessao(self):
        titular = self.empresa()
        self.convidar(titular, "Gil", "gil@empresa.com")
        gil = self.aceitar("gil@empresa.com")
        self.assertIn("/conta", gil.get("/conta/equipe").headers["Location"])
        html = gil.get("/conta/dados").get_data(as_text=True)
        self.assertIn("Os dados da empresa são cuidados por", html)
        self.post(gil, "/conta/dados", {"nome": "Gil Souza", "telefone": "11999"}, "/conta/dados")
        with app.app_context():
            self.assertEqual(User.query.filter_by(email="gil@empresa.com").one().name, "Gil Souza")
            self.assertNotEqual(User.query.filter_by(email="empresa@teste.com").one().name, "Gil Souza")
            uid = User.query.filter_by(email="gil@empresa.com").one().id
        self.post(titular, "/conta/equipe", {"acao": "remover", "uid": str(uid)}, "/conta/equipe")
        self.assertIn("/entrar", gil.get("/conta").headers["Location"])
        with app.app_context():
            self.assertIsNone(db.session.get(User, uid))

    def test_lista_de_clientes_do_painel_e_exclusao(self):
        titular = self.empresa()
        self.convidar(titular, "Hugo", "hugo@empresa.com")
        a = self.admin()
        html = a.get("/admin/clientes").get_data(as_text=True)
        self.assertNotIn("hugo@empresa.com", html, "usuário da equipe não aparece como cliente separado")
        with app.app_context():
            t = User.query.filter_by(email="empresa@teste.com").one()
            hugo = User.query.filter_by(email="hugo@empresa.com").one().id
            tid = t.id
        self.assertIn("hugo@empresa.com", a.get(f"/admin/clientes/{tid}").get_data(as_text=True))
        self.assertIn(f"/admin/clientes/{tid}", a.get(f"/admin/clientes/{hugo}").headers["Location"])
        self.post(a, f"/admin/clientes/{tid}/excluir", {}, f"/admin/clientes/{tid}")
        with app.app_context():
            self.assertIsNone(db.session.get(User, hugo), "excluir a empresa exclui a equipe")

    def test_email_ja_usado_e_pessoa_fisica(self):
        titular = self.empresa()
        self.cadastrar("cliente@outra.com")
        self.convidar(titular, "X", "cliente@outra.com")
        self.assertIn("já tem conta", titular.get("/conta/equipe").get_data(as_text=True))
        pf = self.cadastrar("pf@teste.com")
        self.assertNotIn("Equipe</a>", pf.get("/conta").get_data(as_text=True))
        self.assertIn("/conta", pf.get("/conta/equipe").headers["Location"])

    def test_painel_avisa_que_orcamento_do_comprador_passa_pela_empresa(self):
        titular = self.empresa()
        self.convidar(titular, "Hugo", "hugo@empresa.com", limite="100,00")
        hugo = self.aceitar("hugo@empresa.com")
        self.adicionar(hugo, 1, 1)
        self.fechar(hugo, "orcamento")
        with app.app_context():
            oid = Order.query.order_by(Order.id.desc()).first().id
        html = self.admin().get(f"/admin/orders/{oid}/orcamento").get_data(as_text=True)
        self.assertIn("comprador da equipe da empresa, com limite de R$ 100,00", html)
        self.assertIn("Se o total aprovado passar do limite, o orçamento vai primeiro para a empresa aprovar", html)
        self.assertIn(">Aprovar orçamento<", html)
        oid_titular = None
        self.adicionar(titular, 1, 1)
        self.fechar(titular, "orcamento")
        with app.app_context():
            oid_titular = Order.query.order_by(Order.id.desc()).first().id
        html = self.admin().get(f"/admin/orders/{oid_titular}/orcamento").get_data(as_text=True)
        self.assertNotIn("comprador da equipe", html, "orçamento do titular: sem aviso")
        self.assertIn(">Aprovar e baixar estoque<", html)

    def test_pagina_da_equipe_explica_o_orcamento(self):
        html = self.empresa().get("/conta/equipe").get_data(as_text=True)
        self.assertNotIn("Orçamentos não precisam de aprovação", html)
        self.assertIn("se o total passar do limite, ele volta para você aprovar", html)

    def test_horario_do_pedido_e_o_de_brasilia(self):
        from datetime import datetime
        titular = self.empresa()
        self.adicionar(titular, 1, 1)
        self.fechar(titular, "pix")
        with app.app_context():
            o = Order.query.order_by(Order.id.desc()).first()
            o.created_at = datetime(2026, 11, 18, 12, 19)  # gravado em UTC
            db.session.commit()
            oid = o.id
        self.assertIn("Feito em 18/11/2026 09:19", titular.get(f"/conta/pedidos/{oid}").get_data(as_text=True))
        a = self.admin()
        self.assertIn("18/11/2026 09:19", a.get(f"/admin/orders/{oid}").get_data(as_text=True))
        self.assertIn("18/11/2026 09:19", a.get("/admin/orders").get_data(as_text=True))



if __name__ == "__main__":
    unittest.main()
