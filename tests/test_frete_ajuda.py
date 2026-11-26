"""Testes do frete por CEP, da ajuda (perguntas frequentes) e dos limites guardados no banco."""
import io
import unittest
from decimal import Decimal

from openpyxl import load_workbook

from test_b2b import DADOS, BaseB2B

from app import app  # noqa: E402
import ajuda  # noqa: E402
import frete  # noqa: E402
from models import LimiteUso, Order, Pergunta, RegraFrete, db  # noqa: E402


class BaseFrete(BaseB2B):
    def regra(self, **campos):
        with app.app_context():
            db.session.add(RegraFrete(**{"ativo": True, "ordem": 0, **campos}))
            db.session.commit()

    def ultimo(self):
        with app.app_context():
            o = Order.query.order_by(Order.id.desc()).first()
            return o.total, o.frete, o.frete_combinar, o.cep, o.subtotal


class TesteFrete(BaseFrete):
    def test_estado_pelo_cep(self):
        casos = {"01001000": "SP", "13010000": "SP", "20040002": "RJ", "30130000": "MG", "70040010": "DF",
                 "69900000": "AC", "88010000": "SC", "90010000": "RS", "64000000": "PI"}
        for cep, uf in casos.items():
            self.assertEqual(frete.uf_do_cep(cep), uf, cep)
        self.assertIsNone(frete.normalizar_cep("123"))
        self.assertEqual(frete.normalizar_cep("01001-000"), "01001000")

    def test_regra_mais_especifica_vence(self):
        self.regra(nome="Brasil", valor=Decimal("50"))
        self.regra(nome="SP", ufs="SP", valor=Decimal("20"))
        self.regra(nome="Capital", cep_de=1000000, cep_ate=5999999, valor=Decimal("10"))
        with app.app_context():
            self.assertEqual(frete.calcular("01001000", 100, 0)["regra"], "Capital")
            self.assertEqual(frete.calcular("13010000", 100, 0)["valor"], Decimal("20.00"))
            self.assertEqual(frete.calcular("30130000", 100, 0)["valor"], Decimal("50.00"))

    def test_sem_regra_e_a_combinar(self):
        self.regra(nome="Só SP", ufs="SP", valor=Decimal("20"))
        with app.app_context():
            self.assertIsNone(frete.calcular("30130000", 100, 0))

    def test_peso_e_frete_gratis(self):
        self.regra(nome="Brasil", valor=Decimal("10"), valor_kg=Decimal("2"), gratis_acima=Decimal("1000"))
        with app.app_context():
            self.assertEqual(frete.calcular("30130000", 500, Decimal("3.2"))["valor"], Decimal("18.00"))
            r = frete.calcular("30130000", 1200, Decimal("3.2"))
            self.assertTrue(r["gratis"])
            self.assertEqual(r["valor"], 0)

    def test_frete_entra_no_pedido(self):
        self.regra(nome="SP", ufs="SP", valor=Decimal("25"), prazo="2 dias úteis")
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        html = self.post(c, "/carrinho/cep", {"cep": "01001-000"}, "/carrinho") and c.get("/carrinho").get_data(as_text=True)
        self.assertIn("R$ 25,00", html)
        self.assertIn("2 dias úteis", html)
        self.assertEqual(c.get("/carrinho/frete?cep=01001000").get_json()["valor"], "R$ 25,00")
        self.fechar(c, "pix")
        total, valor_frete, combinar, cep, subtotal = self.ultimo()
        self.assertEqual((valor_frete, combinar, cep), (Decimal("25.00"), False, "01001000"))
        self.assertEqual(total, subtotal + Decimal("25"))
        self.assertIn("Frete: R$ 25,00 (2 dias úteis)", self.emails[-2]["texto"] + self.emails[-1]["texto"])

    def test_cep_sem_regra_fica_a_combinar(self):
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.post(c, "/checkout", {**DADOS, "cep": "30130-000", "payment": "pix"})
        total, valor_frete, combinar, _, subtotal = self.ultimo()
        self.assertEqual((valor_frete, combinar, total), (0, True, subtotal))

    def test_checkout_exige_cep(self):
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        r = self.post(c, "/checkout", {**DADOS, "cep": "123", "payment": "pix"})
        self.assertIn("CEP válido", r.get_data(as_text=True))
        with app.app_context():
            self.assertEqual(Order.query.count(), 80, "só os pedidos de exemplo")

    def test_orcamento_com_frete_editado(self):
        c = self.cadastrar()
        self.adicionar(c, 1, 2)
        self.fechar(c, "orcamento")
        with app.app_context():
            o = Order.query.order_by(Order.id.desc()).first()
            oid, item = o.id, o.items[0].id
        self.post(self.admin(), f"/admin/orders/{oid}/orcamento",
                  {"acao": "aprovar", f"preco_{item}": "100,00", "pagamento": "pix", "frete": "35,50",
                   "frete_prazo": "5 dias"}, f"/admin/orders/{oid}/orcamento")
        total, valor_frete, combinar, _, _ = self.ultimo()
        self.assertEqual((total, valor_frete, combinar), (Decimal("235.50"), Decimal("35.50"), False))

    def test_painel_de_regras(self):
        a = self.admin()
        r = self.post(a, "/admin/frete", {"nome": "X", "ufs": "SP, ZZ"}, "/admin/frete")
        self.assertIn("Estado inválido: ZZ", a.get(r.headers["Location"]).get_data(as_text=True))
        r = self.post(a, "/admin/frete", {"nome": "X", "cep_de": "01000-000"}, "/admin/frete")
        self.assertIn("preencha os dois CEPs", a.get(r.headers["Location"]).get_data(as_text=True))
        self.post(a, "/admin/frete", {"nome": "Sudeste", "ufs": "sp rj", "valor": "19,90", "prazo": "3 dias"},
                  "/admin/frete")
        with app.app_context():
            r = RegraFrete.query.one()
            self.assertEqual((r.ufs, r.valor, r.ativo), ("RJ,SP", Decimal("19.90"), True))
        html = self.post(a, "/admin/frete", {"acao": "simular", "sim_cep": "20040-002", "sim_valor": "100",
                                             "sim_peso": "1"}, "/admin/frete").get_data(as_text=True)
        self.assertIn("Sudeste", html)
        self.assertIn("R$ 19,90", html)

    def test_peso_no_produto_e_na_planilha(self):
        a = self.admin()
        with app.app_context():
            from models import Product
            p = db.session.get(Product, 2)
            form = {"name": p.name, "category_id": p.category_id, "price": "149,90", "stock": 50, "active": "on",
                    "qtd_minima": 1, "multiplo": 1, "peso_kg": "1,250"}
        self.post(a, "/admin/products/2/edit", form, "/admin/products/2/edit")
        with app.app_context():
            self.assertEqual(db.session.get(Product, 2).peso_kg, Decimal("1.250"))
        ws = load_workbook(io.BytesIO(a.get("/admin/planilhas/produtos.xlsx").data)).active
        cab = [c.value for c in ws[1]]
        self.assertIn("peso_kg", cab)


class TesteAjuda(BaseB2B):
    def test_aparece_sem_ia_com_as_perguntas_padrao(self):
        html = self.cliente().get("/").get_data(as_text=True)
        self.assertIn('id="ajuda"', html)
        self.assertIn("Por que não vejo os preços?", html)
        self.assertIn('<a href="/cadastro">/cadastro</a>', html)
        self.assertNotIn('id="ajuda"', self.admin().get("/admin/").get_data(as_text=True))

    def test_some_quando_a_ia_esta_ligada(self):
        app.config["ANTHROPIC_API_KEY"] = "sk-teste"
        self.addCleanup(app.config.update, ANTHROPIC_API_KEY="")
        html = self.cliente().get("/").get_data(as_text=True)
        self.assertNotIn('id="ajuda"', html)
        self.assertIn('id="chat"', html)

    def test_formatar_escapa_html(self):
        with app.app_context():
            html = str(ajuda.formatar('<script>alert(1)</script> veja /conta/pedidos e https://exemplo.com'))
        self.assertNotIn("<script>", html)
        self.assertIn('<a href="/conta/pedidos">', html)
        self.assertIn('target="_blank"', html)

    def test_painel_edita_as_perguntas(self):
        a = self.admin()
        self.post(a, "/admin/ajuda", {"acao": "padrao"}, "/admin/ajuda")
        with app.app_context():
            self.assertEqual(Pergunta.query.count(), len(ajuda.PADRAO))
            pid = Pergunta.query.order_by(Pergunta.ordem).first().id
        self.post(a, "/admin/ajuda", {"pid": pid, "pergunta": "Pergunta nova?", "resposta": "Resposta nova."},
                  "/admin/ajuda")  # sem "ativo": some da loja
        self.post(a, "/admin/ajuda", {"pergunta": "Entregam no Brasil todo?", "resposta": "Sim."}, "/admin/ajuda")
        html = self.cliente().get("/").get_data(as_text=True)
        self.assertNotIn("Pergunta nova?", html)
        self.assertIn("Entregam no Brasil todo?", html)


class TesteLimitesNoBanco(BaseB2B):
    def test_limite_fica_no_banco_sem_guardar_o_email(self):
        from security import dentro_do_limite
        with app.app_context():
            self.assertTrue(dentro_do_limite([("email:ana@ex.com", 60, 1)]))
            self.assertFalse(dentro_do_limite([("email:ana@ex.com", 60, 1)]))
            chaves = [l.chave for l in LimiteUso.query.all()]
        self.assertEqual(len(chaves), 1)
        self.assertNotIn("ana", chaves[0])


if __name__ == "__main__":
    unittest.main()
