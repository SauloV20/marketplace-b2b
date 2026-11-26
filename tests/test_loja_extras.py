"""Testes da vitrine (filtros e ordenação), do avise-me quando chegar e das listas de compra."""
import re
import unittest
from decimal import Decimal

from test_b2b import BaseB2B

from app import app  # noqa: E402
import lgpd  # noqa: E402
from models import AvisoEstoque, ListaCompra, Product, User, db  # noqa: E402


def ids_da_vitrine(html):
    return [int(x) for x in re.findall(r'href="/produto/(\d+)-', html.split('class="grid"', 1)[-1])][::2]


class TesteFiltros(BaseB2B):
    def test_ordem_por_nome_e_so_disponiveis(self):
        self.ajustar_produto(1, stock=0)
        html = self.cliente().get("/?ordem=nome").get_data(as_text=True)
        with app.app_context():
            nomes = [db.session.get(Product, i).name for i in ids_da_vitrine(html)]
        self.assertEqual(nomes, sorted(nomes, key=str.casefold))
        self.assertIn(1, ids_da_vitrine(html))
        html = self.cliente().get("/?disp=1").get_data(as_text=True)
        self.assertNotIn(1, ids_da_vitrine(html))
        self.assertIn('name="robots" content="noindex', html, "lista filtrada não vai para o Google")

    def test_preco_so_para_quem_entrou(self):
        visitante = self.cliente().get("/?ordem=menor&min=100").get_data(as_text=True)
        self.assertNotIn("Menor preço", visitante)
        self.assertNotIn('name="min"', visitante)
        with app.app_context():
            todos = Product.query.filter_by(active=True).count()
        self.assertEqual(len(ids_da_vitrine(visitante)), todos, "visitante: filtro de preço é ignorado")

        c = self.cadastrar()
        html = c.get("/?ordem=menor").get_data(as_text=True)
        with app.app_context():
            precos = [db.session.get(Product, i).price for i in ids_da_vitrine(html)]
        self.assertEqual(precos, sorted(precos))
        html = c.get("/?min=50&max=100,00").get_data(as_text=True)
        with app.app_context():
            precos = [db.session.get(Product, i).price for i in ids_da_vitrine(html)]
        self.assertTrue(precos and all(Decimal(50) <= p <= Decimal(100) for p in precos))
        self.assertIn("Limpar filtros", html)

    def test_busca_pelo_codigo(self):
        self.ajustar_produto(2, codigo="ZXQ-77")
        self.assertEqual(ids_da_vitrine(self.cliente().get("/?q=zxq-77").get_data(as_text=True)), [2])


class TesteAviseMe(BaseB2B):
    def test_pedir_receber_uma_vez_e_cancelar(self):
        self.ajustar_produto(1, stock=0)
        c = self.cadastrar()
        html = c.get(self.link(1)).get_data(as_text=True)
        self.assertIn("Avise-me quando chegar", html)
        self.post(c, "/produto/1/avise-me", {}, self.link(1))
        self.assertIn("Você será avisado", c.get(self.link(1)).get_data(as_text=True))
        self.assertIn("Avise-me quando chegar", c.get("/conta").get_data(as_text=True))
        a = self.admin()
        self.assertIn("1 cliente(s) pediram", a.get("/admin/products/1/edit").get_data(as_text=True))

        del self.emails[:]
        with app.app_context():
            p = db.session.get(Product, 1)
            form = {"name": p.name, "category_id": p.category_id, "price": "189,90", "stock": "40",
                    "qtd_minima": p.qtd_minima, "multiplo": p.multiplo, "active": "on"}
        self.post(a, "/admin/products/1/edit", form, "/admin/products/1/edit")
        avisos = [e for e in self.emails if e["para"] == "cli@teste.com"]
        self.assertEqual(len(avisos), 1)
        self.assertIn("Marketplace B2B: chegou", avisos[0]["assunto"])
        self.assertIn("/produto/1-", avisos[0]["texto"])
        self.post(a, "/admin/products/1/edit", form, "/admin/products/1/edit")
        self.assertEqual(len([e for e in self.emails if e["para"] == "cli@teste.com"]), 1, "avisa uma vez só")

        # cancelar antes de chegar
        self.ajustar_produto(2, stock=0)
        self.post(c, "/produto/2/avise-me", {}, self.link(2))
        self.post(c, "/produto/2/avise-me", {"voltar": "/conta"}, "/conta")
        with app.app_context():
            self.assertEqual(AvisoEstoque.query.filter_by(product_id=2).count(), 0)

    def test_cancelamento_de_pedido_devolve_e_avisa(self):
        self.ajustar_produto(1, stock=1, qtd_minima=1, multiplo=1)
        comprador = self.cadastrar("comprador@teste.com")
        self.adicionar(comprador, 1, 1)
        self.fechar(comprador, "pix")
        espera = self.cadastrar()
        self.post(espera, "/produto/1/avise-me", {}, self.link(1))
        with app.app_context():
            from models import Order
            oid = Order.query.order_by(Order.id.desc()).first().id
        del self.emails[:]
        self.post(self.admin(), f"/admin/orders/{oid}/status", {"status": "cancelado"}, "/admin/orders")
        self.assertTrue(any(e["para"] == "cli@teste.com" and "chegou" in e["assunto"] for e in self.emails))

    def link(self, pid):
        with app.app_context():
            from produtos import link
            with app.test_request_context():
                return link(db.session.get(Product, pid))


class TesteListas(BaseB2B):
    def lista(self, nome):
        with app.app_context():
            lista = ListaCompra.query.filter_by(nome=nome).one()
            return lista.id, {i.product_id: i.qtd for i in lista.itens}

    def test_salvar_do_produto_e_mandar_ao_carrinho(self):
        self.ajustar_produto(1, qtd_minima=1, multiplo=1)
        self.ajustar_produto(2, qtd_minima=1, multiplo=1)
        c = self.cadastrar()
        self.post(c, "/conta/listas/adicionar", {"pid": "1", "qtd": "3", "lista": "nova", "nome_lista": "Mensal"}, "/conta")
        lid, itens = self.lista("Mensal")
        self.assertEqual(itens, {1: 3})
        self.post(c, "/conta/listas/adicionar", {"pid": "2", "qtd": "1", "lista": str(lid)}, "/conta")
        self.post(c, "/conta/listas/adicionar", {"pid": "1", "qtd": "2", "lista": str(lid)}, "/conta")
        self.assertEqual(self.lista("Mensal")[1], {1: 5, 2: 1})
        html = c.get(f"/conta/listas/{lid}").get_data(as_text=True)
        self.assertIn("Mandar tudo ao carrinho", html)

        self.ajustar_produto(2, stock=0)
        r = self.post(c, f"/conta/listas/{lid}", {"acao": "carrinho", "qtd_1": "4"}, f"/conta/listas/{lid}")
        self.assertIn("/carrinho", r.headers["Location"])
        with c.session_transaction() as s:
            self.assertEqual(s["cart"], {"1": 4})
        self.assertEqual(self.lista("Mensal")[1], {1: 4, 2: 1}, "a quantidade digitada fica salva")

    def test_salvar_carrinho_renomear_e_apagar(self):
        self.ajustar_produto(1, qtd_minima=1, multiplo=1)
        c = self.cadastrar()
        self.adicionar(c, 1, 2)
        r = self.post(c, "/conta/listas/do-carrinho", {"lista": "nova", "nome_lista": "Obra"}, "/carrinho")
        lid, itens = self.lista("Obra")
        self.assertEqual(itens, {1: 2})
        self.assertIn(f"/conta/listas/{lid}", r.headers["Location"])
        self.post(c, f"/conta/listas/{lid}", {"acao": "remover:999"}, f"/conta/listas/{lid}")
        self.post(c, f"/conta/listas/{lid}", {"acao": "renomear", "nome": "Obra Centro"}, f"/conta/listas/{lid}")
        self.assertEqual(self.lista("Obra Centro")[0], lid)
        r = self.post(c, "/conta/listas", {"nome": "Obra Centro"}, "/conta/listas")
        self.assertIn("já tem uma lista", c.get("/conta/listas").get_data(as_text=True))
        self.post(c, f"/conta/listas/{lid}", {"acao": "excluir"}, f"/conta/listas/{lid}")
        with app.app_context():
            self.assertEqual(ListaCompra.query.count(), 0)

    def test_lista_de_outro_cliente_nao_abre(self):
        c = self.cadastrar()
        self.post(c, "/conta/listas", {"nome": "Minha"}, "/conta/listas")
        lid = self.lista("Minha")[0]
        outro = self.cadastrar("outro@teste.com")
        self.assertEqual(outro.get(f"/conta/listas/{lid}").status_code, 404)
        self.assertEqual(self.post(outro, f"/conta/listas/{lid}", {"acao": "excluir"}, "/conta").status_code, 404)
        r = self.post(outro, "/conta/listas/adicionar", {"pid": "1", "lista": str(lid)}, "/conta")
        self.assertEqual(self.lista("Minha")[1], {})
        self.assertEqual(r.status_code, 302)

    def test_atualizar_todas_as_quantidades_de_uma_vez(self):
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)
        self.ajustar_produto(2, qtd_minima=1, multiplo=1, stock=50)
        self.ajustar_produto(3, qtd_minima=1, multiplo=1, stock=50)
        c = self.cadastrar()
        for pid in (1, 2, 3):
            self.adicionar(c, pid, 1)
        html = c.get("/carrinho").get_data(as_text=True)
        self.assertEqual(html.count("Atualizar carrinho"), 1, "um botão só")
        self.assertNotIn('<button class="btn ghost">Atualizar</button>', html)
        self.assertIn('name="qty_1" form="carrinho-qtd"', html)
        self.assertIn("shop.js", html)
        r = self.post(c, "/carrinho/atualizar", {"qty_1": "4", "qty_2": "0", "qty_3": "999"}, "/carrinho")
        self.assertIn("/carrinho", r.headers["Location"])
        with c.session_transaction() as s:
            self.assertEqual(s["cart"], {"1": 4, "3": 50})
        html = c.get("/carrinho").get_data(as_text=True)
        self.assertIn("quantidade ajustada para 50 un", html)
        self.post(c, "/carrinho/atualizar", {"qty_1": "6", "qty_99": "5"}, "/carrinho")
        with c.session_transaction() as s:
            self.assertEqual(s["cart"], {"1": 6, "3": 50}, "item que não está no carrinho é ignorado")
        self.assertIn("Carrinho atualizado", c.get("/carrinho").get_data(as_text=True))

    def test_esvaziar_carrinho(self):
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.adicionar(c, 2, 1)
        with c.session_transaction() as s:
            s["cupom"] = "QUALQUER"
        self.assertIn("Esvaziar carrinho", c.get("/carrinho").get_data(as_text=True))
        r = self.post(c, "/carrinho/esvaziar", {}, "/carrinho")
        self.assertIn("/carrinho", r.headers["Location"])
        with c.session_transaction() as s:
            self.assertEqual(s["cart"], {})
            self.assertNotIn("cupom", s)
        html = c.get("/carrinho").get_data(as_text=True)
        self.assertIn("Carrinho esvaziado", html)
        self.assertIn("Seu carrinho está vazio", html)

    def test_lista_soma_com_o_que_ja_esta_no_carrinho(self):
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)
        c = self.cadastrar()
        self.post(c, "/conta/listas/adicionar", {"pid": "1", "qtd": "3", "lista": "nova", "nome_lista": "L"}, "/conta")
        lid = self.lista("L")[0]
        self.adicionar(c, 1, 2)
        self.post(c, f"/conta/listas/{lid}", {"acao": "carrinho"}, f"/conta/listas/{lid}")
        with c.session_transaction() as s:
            self.assertEqual(s["cart"], {"1": 5})

    def test_lgpd(self):
        c = self.cadastrar()
        self.post(c, "/conta/listas/adicionar", {"pid": "1", "qtd": "1", "lista": "nova", "nome_lista": "X"}, "/conta")
        self.ajustar_produto(2, stock=0)
        self.post(c, "/produto/2/avise-me", {}, "/conta")
        with app.app_context():
            u = User.query.filter_by(email="cli@teste.com").one()
            dados = lgpd.exportar(u)
            self.assertEqual(dados["listas_de_compra"][0]["nome"], "X")
            self.assertEqual(len(dados["avise_me"]), 1)
            self.assertEqual(lgpd.excluir_conta(u), 0)
            self.assertEqual((ListaCompra.query.count(), AvisoEstoque.query.count()), (0, 0))


if __name__ == "__main__":
    unittest.main()
