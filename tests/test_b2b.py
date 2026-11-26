"""Testes das vendas para empresas: CPF/CNPJ, aprovação, preço por quantidade, mínimo/múltiplo,
desconto, boleto faturado, pedido mínimo e orçamento."""
import unittest
from decimal import Decimal

from test_site import SENHA, BaseTeste, novo_cnpj, novo_cpf

from app import app  # noqa: E402
from b2b import ajustar_qtd, cnpj_valido, cpf_valido, ler_faixas, ler_prazos  # noqa: E402
from models import Order, Product, User, db  # noqa: E402

DADOS = {"name": "Compras", "email": "compras@ex.com", "phone": "11999", "address": "Rua A, 1", "cep": "01001-000"}


class BaseB2B(BaseTeste):
    def pid(self, nome):
        with app.app_context():
            return Product.query.filter_by(name=nome).one().id

    def ajustar_produto(self, pid, **campos):
        with app.app_context():
            p = db.session.get(Product, pid)
            for k, v in campos.items():
                setattr(p, k, v)
            db.session.commit()

    def empresa(self, email="empresa@teste.com", aprovar=True, desconto="10", prazos="28; 30/60/90"):
        c = self.cadastrar(email, tipo="pj")
        if aprovar:
            with app.app_context():
                uid = User.query.filter_by(email=email).one().id
            a = self.admin()
            self.post(a, f"/admin/clientes/{uid}", {"aprovada": "on", "desconto": desconto, "prazos": prazos,
                                                    "razao_social": "Metalúrgica Teste Ltda"}, f"/admin/clientes/{uid}")
        return c

    def adicionar(self, c, pid, qtd):
        return self.post(c, "/carrinho/add", {"pid": pid, "qty": qtd})

    def fechar(self, c, pagamento, **extra):
        return self.post(c, "/checkout", {**DADOS, "payment": pagamento, **extra})

    def ultimo_pedido(self):
        with app.app_context():
            o = Order.query.order_by(Order.id.desc()).first()
            return o.id, o.status, o.total, o.payment_method, o.prazo, [(i.quantity, i.unit_price) for i in o.items]


class TesteDocumentos(BaseB2B):
    def test_validacao_de_cpf_e_cnpj(self):
        self.assertTrue(cpf_valido("52998224725"))
        self.assertFalse(cpf_valido("52998224724"))
        self.assertFalse(cpf_valido("11111111111"))
        self.assertTrue(cnpj_valido("11222333000181"))
        self.assertFalse(cnpj_valido("11222333000180"))
        self.assertTrue(cpf_valido(novo_cpf()) and cnpj_valido(novo_cnpj()))

    def test_cadastro_recusa_documento_invalido_ou_repetido(self):
        c = self.cliente()
        base = {"nome": "X", "email": "x@ex.com", "senha": SENHA, "senha2": SENHA, "aceite": "1"}
        r = self.post(c, "/cadastro", {**base, "tipo": "pf", "documento": "123.456.789-00"})
        self.assertIn("CPF inválido", r.get_data(as_text=True))
        r = self.post(c, "/cadastro", {**base, "tipo": "pj", "documento": "11.222.333/0001-81", "razao_social": ""})
        self.assertIn("razão social", r.get_data(as_text=True))
        self.post(c, "/cadastro", {**base, "tipo": "pj", "documento": "11.222.333/0001-81", "razao_social": "ACME"})
        r = self.post(self.cliente(), "/cadastro", {**base, "email": "y@ex.com", "tipo": "pj",
                                                    "documento": "11222333000181", "razao_social": "Outra"})
        self.assertIn("Este CNPJ já tem conta", r.get_data(as_text=True))
        with app.app_context():
            u = User.query.filter_by(email="x@ex.com").one()
            self.assertEqual((u.tipo, u.documento, u.empresa_aprovada), ("pj", "11222333000181", False))

    def test_conta_antiga_sem_documento_informa_antes_de_comprar(self):
        c = self.cadastrar()
        with app.app_context():
            User.query.filter_by(email="cli@teste.com").one().documento = None
            db.session.commit()
        self.adicionar(c, 1, 1)
        self.assertIn("/conta/dados", c.get("/checkout").headers["Location"])
        self.post(c, "/conta/dados", {"nome": "Cli", "tipo": "pf", "documento": "529.982.247-25"}, "/conta/dados")
        self.assertEqual(c.get("/checkout").status_code, 200)


class TesteQuantidadesEPrecos(BaseB2B):
    def test_ajuste_ao_minimo_e_multiplo(self):
        p = Product(price=Decimal(1), stock=35, qtd_minima=12, multiplo=12)
        self.assertEqual([ajustar_qtd(p, q) for q in (1, 12, 13, 30, 99)], [12, 12, 24, 24, 24])
        p.stock = 5
        self.assertEqual(ajustar_qtd(p, 1), 0)

    def test_carrinho_respeita_multiplo_e_faixa_de_preco(self):
        flap = self.pid("Disco Flap 4.1/2 pol Grão 80")  # múltiplos de 10; 50+ sai a 12,90
        self.ajustar_produto(flap, stock=500, fracionavel=False)
        c = self.cadastrar()
        self.assertEqual(self.adicionar(c, flap, 1).get_json()["count"], 10)
        self.post(c, "/carrinho/update", {"pid": flap, "qty": 47}, "/carrinho")
        self.fechar(c, "pix")
        _, _, total, *_ , itens = self.ultimo_pedido()
        self.assertEqual(itens, [(50, Decimal("12.90"))])
        self.assertEqual(total, Decimal("645.00"))

    def test_faixas_e_prazos_do_painel(self):
        self.assertEqual(ler_faixas("10 = 16,50\n\n50=14,90")[0], [(10, Decimal("16.50")), (50, Decimal("14.90"))])
        self.assertIsNone(ler_faixas("dez = 1")[0])
        self.assertIsNone(ler_faixas("1 = 5")[0])
        self.assertEqual(ler_prazos("28; 30 / 60 / 90 ;28")[0], ["28", "30/60/90"])
        self.assertIsNone(ler_prazos("30/abc")[0])

    def test_admin_edita_regras_do_produto(self):
        a = self.admin()
        with app.app_context():
            p = db.session.get(Product, 2)
            form = {"name": p.name, "category_id": p.category_id, "price": "149,90", "stock": 50, "active": "on",
                    "qtd_minima": 2, "multiplo": 2, "faixas": "10 = 139,90"}
        self.post(a, "/admin/products/2/edit", form, "/admin/products/2/edit")
        with app.app_context():
            p = db.session.get(Product, 2)
            self.assertEqual((p.qtd_minima, p.multiplo, [(f.qtd_minima, f.preco) for f in p.faixas]),
                             (2, 2, [(10, Decimal("139.90"))]))
        r = self.post(a, "/admin/products/2/edit", {**form, "faixas": "10 = abc"}, "/admin/products/2/edit")
        self.assertIn("Faixa inválida", r.get_data(as_text=True))


class TesteEmpresa(BaseB2B):
    def test_empresa_em_analise_nao_tem_desconto_nem_faturado(self):
        c = self.empresa(aprovar=False)
        self.assertIn("em análise", c.get("/conta").get_data(as_text=True))
        self.adicionar(c, 1, 1)
        html = c.get("/checkout").get_data(as_text=True)
        self.assertNotIn('value="faturado"', html)
        self.fechar(c, "faturado", prazo="28")
        with app.app_context():
            self.assertEqual(Order.query.filter_by(payment_method="faturado").count(), 0)

    def test_empresa_aprovada_tem_desconto_e_boleto_faturado(self):
        c = self.empresa(desconto="10", prazos="28; 30/60/90")
        self.adicionar(c, 1, 1)  # tabela 189,90 -> 170,91 com 10%
        self.assertIn("170,91", c.get("/carrinho").get_data(as_text=True))
        self.fechar(c, "faturado", prazo="45")  # prazo não liberado
        with app.app_context():
            self.assertEqual(Order.query.filter_by(payment_method="faturado").count(), 0)
        r = self.fechar(c, "faturado", prazo="30/60/90")
        self.assertIn("/pedido/", r.headers["Location"])
        _, status, total, metodo, prazo, _ = self.ultimo_pedido()
        self.assertEqual((status, total, metodo, prazo), ("pendente", Decimal("170.91"), "faturado", "30/60/90"))
        with app.app_context():
            o = Order.query.order_by(Order.id.desc()).first()
            self.assertEqual(o.razao_social, "Metalúrgica Teste Ltda")
            self.assertEqual(len(o.documento), 14)

    def test_pessoa_fisica_nao_recebe_condicoes_de_empresa(self):
        self.cadastrar()
        with app.app_context():
            uid = User.query.filter_by(email="cli@teste.com").one().id
        a = self.admin()
        self.post(a, f"/admin/clientes/{uid}", {"aprovada": "on", "desconto": "50", "prazos": "28"},
                  f"/admin/clientes/{uid}")
        with app.app_context():
            u = db.session.get(User, uid)
            self.assertFalse(u.empresa_aprovada)
            self.assertEqual(u.desconto, 0)

    def test_pagina_de_clientes_e_painel(self):
        self.empresa(aprovar=False)
        a = self.admin()
        html = a.get("/admin/clientes?f=analise").get_data(as_text=True)
        self.assertIn("Metalúrgica Teste Ltda", html)
        self.assertIn("Empresas para aprovar", a.get("/admin/").get_data(as_text=True))
        self.assertEqual(self.cadastrar("x@x.com").get("/admin/clientes").status_code, 302)


class TestePedidoMinimo(BaseB2B):
    def test_pedido_minimo(self):
        a = self.admin()
        self.post(a, "/admin/regras", {"pedido_minimo": "500,00"}, "/admin/regras")
        c = self.cadastrar()
        self.adicionar(c, 1, 1)  # 189,90
        self.assertIn("Pedido mínimo", c.get("/carrinho").get_data(as_text=True))
        self.assertTrue(c.get("/checkout").headers["Location"].endswith("/carrinho"))
        self.fechar(c, "pix")
        with app.app_context():
            self.assertEqual(Order.query.filter_by(customer_email="compras@ex.com").count(), 0)
        self.adicionar(c, 1, 2)
        self.assertIn("/pedido/", self.fechar(c, "pix").headers["Location"])


class TesteOrcamento(BaseB2B):
    def test_orcamento_nao_baixa_estoque_ate_aprovar(self):
        c = self.empresa()
        antes = self.estoque(1)
        self.adicionar(c, 1, 3)
        self.fechar(c, "orcamento")
        oid, status, *_ = self.ultimo_pedido()
        self.assertEqual(status, "orcamento")
        self.assertEqual(self.estoque(1), antes)
        self.assertIn("Orçamento em análise", c.get(f"/conta/pedidos/{oid}").get_data(as_text=True))
        a = self.admin()
        self.assertIn("Revisar orçamento", a.get("/admin/orders").get_data(as_text=True))
        # o seletor genérico de status não pula a revisão
        self.post(a, f"/admin/orders/{oid}/status", {"status": "pago"}, "/admin/orders")
        self.assertEqual(self.ultimo_pedido()[1], "orcamento")
        with app.app_context():
            item = Order.query.get(oid).items[0].id
        self.post(a, f"/admin/orders/{oid}/orcamento", {"acao": "aprovar", f"preco_{item}": "150,00",
                                                        "pagamento": "faturado", "prazo": "30/60"},
                  f"/admin/orders/{oid}/orcamento")
        _, status, total, metodo, prazo, itens = self.ultimo_pedido()
        self.assertEqual((status, total, metodo, prazo), ("pendente", Decimal("450.00"), "faturado", "30/60"))
        self.assertEqual(self.estoque(1), antes - 3)

    def test_aprovar_sem_estoque_nao_grava_nada(self):
        c = self.cadastrar()
        self.adicionar(c, 5, 2)
        self.fechar(c, "orcamento")
        oid, *_ = self.ultimo_pedido()
        self.ajustar_produto(5, stock=1)
        a = self.admin()
        with app.app_context():
            item = Order.query.get(oid).items[0].id
        self.post(a, f"/admin/orders/{oid}/orcamento", {"acao": "aprovar", f"preco_{item}": "1,00",
                                                        "pagamento": "pix"}, f"/admin/orders/{oid}/orcamento")
        _, status, total, metodo, _, itens = self.ultimo_pedido()
        self.assertEqual((status, metodo), ("orcamento", "orcamento"))
        self.assertNotEqual(itens[0][1], Decimal("1.00"))
        self.assertEqual(self.estoque(5), 1)

    def test_recusar_e_cliente_cancelar(self):
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "orcamento")
        oid, *_ = self.ultimo_pedido()
        antes = self.estoque(1)
        self.post(c, f"/conta/pedidos/{oid}/cancelar", {})
        self.assertEqual(self.ultimo_pedido()[1], "cancelado")
        self.assertEqual(self.estoque(1), antes, "orçamento cancelado não devolve o que nunca saiu")
        self.adicionar(c, 1, 1)
        self.fechar(c, "orcamento")
        oid, *_ = self.ultimo_pedido()
        self.post(self.admin(), f"/admin/orders/{oid}/orcamento", {"acao": "recusar"}, f"/admin/orders/{oid}/orcamento")
        self.assertEqual(self.ultimo_pedido()[1], "cancelado")
        self.assertEqual(self.estoque(1), antes)


if __name__ == "__main__":
    unittest.main()


class TesteFracionado(BaseB2B):
    """Caixa com 25, fracionável com 10% de acréscimo: o que não fecha caixa sai fracionado."""

    def setUp(self):
        super().setUp()
        self.ajustar_produto(13, multiplo=25, stock=100, estoque_avulso=0, fracionavel=True,
                             acrescimo_fracionado=Decimal("10"))  # Disco de Corte Inox, R$ 99,90

    def avulso(self):
        with app.app_context():
            p = db.session.get(Product, 13)
            return p.stock, p.estoque_avulso

    def test_novo_avulso(self):
        from models import novo_avulso
        self.assertEqual(novo_avulso(100, 0, 25, 0, 7), 18)    # abre uma caixa: sobram 18 avulsas
        self.assertEqual(novo_avulso(93, 18, 25, 0, 7), 11)    # usa as avulsas primeiro
        self.assertEqual(novo_avulso(100, 0, 25, 50, 7), 18)   # 2 caixas fechadas + 7 de uma aberta
        self.assertEqual(novo_avulso(93, 3, 25, 0, 7), 21)     # 3 avulsas não bastam: abre outra caixa
        self.assertEqual(novo_avulso(10, 10, 25, 0, 7), 3)     # sem caixa fechada: unidade é unidade

    def test_compra_fracionada_abre_caixa_e_cobra_acrescimo(self):
        c = self.cadastrar()
        self.assertEqual(self.adicionar(c, 13, 7).get_json()["count"], 7, "não arredonda para a caixa")
        html = c.get("/carrinho").get_data(as_text=True)
        self.assertIn("7 un fracionada(s) a R$ 109,89", html)
        self.fechar(c, "pix")
        _, _, total, *_, itens = self.ultimo_pedido()
        self.assertEqual(itens, [(7, Decimal("109.89"))])
        self.assertEqual(total, Decimal("769.23"))
        self.assertEqual(self.avulso(), (93, 18))
        # o próximo pedido quebrado usa as avulsas: nenhuma caixa nova é aberta
        c2 = self.cadastrar("dois@teste.com")
        self.adicionar(c2, 13, 5)
        self.fechar(c2, "pix")
        self.assertEqual(self.avulso(), (88, 13))

    def test_caixas_fechadas_mais_fracionadas_e_cancelamento(self):
        c = self.cadastrar()
        self.adicionar(c, 13, 57)
        self.fechar(c, "pix")
        oid, _, total, *_, itens = self.ultimo_pedido()
        self.assertEqual(sorted(itens), [(7, Decimal("109.89")), (50, Decimal("99.90"))])
        self.assertEqual(total, Decimal("5764.23"))
        self.assertEqual(self.avulso(), (43, 18))
        self.post(c, f"/conta/pedidos/{oid}/cancelar", {})
        self.assertEqual(self.avulso(), (100, 25), "as 7 fracionadas voltam como avulsas")

    def test_produto_que_nao_fraciona_continua_em_multiplos(self):
        self.ajustar_produto(13, fracionavel=False)
        c = self.cadastrar()
        self.assertEqual(self.adicionar(c, 13, 7).get_json()["count"], 25)

    def test_orcamento_fracionado_baixa_avulso_ao_aprovar(self):
        c = self.cadastrar()
        self.adicionar(c, 13, 7)
        self.fechar(c, "orcamento")
        oid, *_ = self.ultimo_pedido()
        self.assertEqual(self.avulso(), (100, 0))
        with app.app_context():
            item = db.session.get(Order, oid).items[0].id
        self.post(self.admin(), f"/admin/orders/{oid}/orcamento",
                  {"acao": "aprovar", f"preco_{item}": "105,00", "pagamento": "pix"}, f"/admin/orders/{oid}/orcamento")
        self.assertEqual(self.avulso(), (93, 18))

    def test_painel_configura_fracionamento(self):
        a = self.admin()
        with app.app_context():
            p = db.session.get(Product, 13)
            form = {"name": p.name, "category_id": p.category_id, "price": "99,90", "stock": 100, "active": "on",
                    "qtd_minima": 1, "multiplo": 25, "fracionavel": "on", "acrescimo_fracionado": "12,5",
                    "estoque_avulso": 20}
        self.post(a, "/admin/products/13/edit", form, "/admin/products/13/edit")
        with app.app_context():
            p = db.session.get(Product, 13)
            self.assertEqual((p.fracionavel, p.acrescimo_fracionado, p.estoque_avulso), (True, Decimal("12.50"), 20))
        r = self.post(a, "/admin/products/13/edit", {**form, "estoque_avulso": 101}, "/admin/products/13/edit")
        self.assertIn("unidades avulsas fazem parte do estoque", r.get_data(as_text=True))
        self.assertIn("80 un em embalagens fechadas (3 com 25) + 20 un avulsa(s)", a.get("/admin/products/13/edit").get_data(as_text=True)
                      .replace("\n", " ").replace("  ", " "))
