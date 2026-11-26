"""Percentuais cadastrados pela loja (impostos e despesas por produto, categoria ou para todos) na formação de preço."""
import unittest
from decimal import Decimal

from test_formacao_preco import BaseFormacao

from app import app  # noqa: E402
import formacao  # noqa: E402
import margem  # noqa: E402
from models import Percentual, Product, User, db  # noqa: E402

PAINEL = "/admin/formacao-de-preco/percentuais"


class BasePercentual(BaseFormacao):
    def cadastrar_pct(self, a, nome, valor, **campos):
        dados = {"nome": nome, "valor": valor, "base": "venda", "escopo": "todos", "ativo": "on", **campos}
        return self.post(a, PAINEL, dados, PAINEL)

    def ids(self, nome):
        with app.app_context():
            return [x.id for x in Percentual.query.filter_by(nome=nome).all()]

    def venda(self, pid, uf=None):
        with app.app_context():
            return formacao.venda_pct(uf, db.session.get(Product, pid))


class TestePercentuais(BasePercentual):
    def test_nada_vem_cadastrado(self):
        with app.app_context():
            self.assertEqual(Percentual.query.count(), 0)
        self.assertIn("Nenhum percentual cadastrado", self.admin().get(PAINEL).get_data(as_text=True))

    def test_percentual_de_todos_muda_o_preco(self):
        a, _, _ = self.montar()
        self.assertEqual(self.preco(1), Decimal("19.32"))
        r = self.cadastrar_pct(a, "PIS", "2")
        self.assertEqual(r.status_code, 302)
        # 10,70 × 1,30 ÷ (1 − 0,30) = 19,87
        self.assertEqual(self.preco(1), Decimal("19.87"))
        self.assertEqual(self.venda(2), Decimal("30"))
        html = a.get(PAINEL).get_data(as_text=True)
        self.assertIn("PIS", html)
        self.assertIn("Todos os produtos", html)

    def test_produto_com_mesmo_nome_passa_na_frente(self):
        a, _, _ = self.montar()
        self.cadastrar_pct(a, "PIS", "2")
        r = self.post(a, "/admin/produtos/1/custo", {"acao": "percentual_incluir", "nome": "pis", "valor": "0",
                                                     "base": "venda"}, "/admin/fornecedores")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.preco(1), Decimal("19.32"))  # PIS zerado só neste produto
        self.assertEqual(self.venda(2), Decimal("30"))  # os outros continuam com o PIS geral
        html = a.get("/admin/produtos/1/custo").get_data(as_text=True)
        self.assertIn("deste produto", html)
        # tirar o do produto volta a valer o geral
        xid = [i for i in self.ids("pis")][0]
        self.post(a, "/admin/produtos/1/custo", {"acao": "percentual_remover", "xid": str(xid)}, "/admin/fornecedores")
        self.assertEqual(self.preco(1), Decimal("19.87"))

    def test_categoria(self):
        a, _, _ = self.montar()
        with app.app_context():
            cat1 = db.session.get(Product, 1).category_id
            outro = Product.query.filter(Product.category_id != cat1).first().id
        self.cadastrar_pct(a, "Comissão", "5", escopo="categoria", category_id=str(cat1))
        self.assertEqual(self.venda(1), Decimal("33"))
        self.assertEqual(self.venda(outro), Decimal("28"))
        # o do produto ganha da categoria
        self.cadastrar_pct(a, "Comissão", "1", escopo="produto", produto="1")
        self.assertEqual(self.venda(1), Decimal("29"))

    def test_percentual_sobre_o_custo(self):
        a, _, _ = self.montar()
        self.cadastrar_pct(a, "Embalagem", "10", base="custo", escopo="produto", produto="1")
        # custo 10,70 + 10% de 10,00 = 11,70; × 1,30 ÷ 0,72 = 21,13
        self.assertEqual(self.preco(1), Decimal("21.13"))
        html = a.get("/admin/produtos/1/custo").get_data(as_text=True)
        self.assertIn("Embalagem (10% da compra)", html)
        self.assertIn("R$ 11,70", html)

    def test_estado_especifico(self):
        a, _, _ = self.montar()
        self.cadastrar_pct(a, "FCP", "2")
        self.cadastrar_pct(a, "FCP", "4", uf="MG")
        self.assertEqual(self.venda(1, "SP"), Decimal("30"))
        self.assertEqual(self.venda(1, "MG"), Decimal("26"))  # MG: 10 federais + 12 ICMS + 4 FCP
        with app.app_context():
            p = db.session.get(Product, 1)
            esperado = (Decimal(100) - Decimal(30)) / (Decimal(100) - Decimal(26))
            self.assertEqual(formacao.fator("MG", p), esperado)

    def test_inativo_nao_conta_e_excluir_recalcula(self):
        a, _, _ = self.montar()
        self.cadastrar_pct(a, "PIS", "2")
        xid = self.ids("PIS")[0]
        self.post(a, f"{PAINEL}/{xid}", {"acao": "ativar", "voltar": "lista"}, PAINEL)
        self.assertEqual(self.preco(1), Decimal("19.32"))
        self.post(a, f"{PAINEL}/{xid}", {"acao": "ativar"}, PAINEL)
        self.assertEqual(self.preco(1), Decimal("19.87"))
        self.post(a, f"{PAINEL}/{xid}", {"acao": "excluir"}, PAINEL)
        self.assertEqual(self.ids("PIS"), [])
        self.assertEqual(self.preco(1), Decimal("19.32"))

    def test_editar(self):
        a, _, _ = self.montar()
        self.cadastrar_pct(a, "PIS", "2")
        xid = self.ids("PIS")[0]
        r = self.post(a, f"{PAINEL}/{xid}", {"nome": "PIS", "valor": "1,65", "base": "venda", "escopo": "todos",
                                              "ativo": "on"}, PAINEL)
        self.assertEqual(r.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(Percentual, xid).valor, Decimal("1.65"))
        self.assertIn('value="1,65"', a.get(f"{PAINEL}/{xid}").get_data(as_text=True))

    def test_validacoes(self):
        a, _, _ = self.montar()
        casos = [({"nome": ""}, "Dê um nome"),
                 ({"valor": "abc"}, "use um número"),
                 ({"valor": "150"}, "vai de 0 a 100"),
                 ({"escopo": "produto", "produto": "não existe"}, "Produto não encontrado"),
                 ({"escopo": "categoria"}, "Escolha a categoria"),
                 ({"base": "custo", "uf": "MG"}, "deixe o estado em branco"),
                 ({"valor": "70"}, "o preço não fecha")]
        for extra, msg in casos:
            dados = {"nome": "X", "valor": "1", "base": "venda", "escopo": "todos", "ativo": "on", **extra}
            html = self.post(a, PAINEL, dados, PAINEL).get_data(as_text=True)
            self.assertIn(msg, html, extra)
        with app.app_context():
            self.assertEqual(Percentual.query.count(), 0)
        self.assertEqual(self.preco(1), Decimal("19.32"))

    def test_margem_e_lucro_usam_o_percentual_do_produto(self):
        a, _, _ = self.montar()
        self.cadastrar_pct(a, "Comissão", "10", escopo="produto", produto="1")
        with app.app_context():
            p = db.session.get(Product, 1)
            r = margem.avaliar(p, Decimal("100"))
            # custo 10,70; impostos 38% de 100
            self.assertEqual(r["lucro"], Decimal("100") - Decimal("10.70") - Decimal("38"))

    def test_excluir_produto_leva_os_percentuais(self):
        a, _, _ = self.montar()
        self.cadastrar_pct(a, "Comissão", "1", escopo="produto", produto="1")
        with app.app_context():
            db.session.delete(db.session.get(Product, 1))
            db.session.commit()
            self.assertEqual(Percentual.query.count(), 0)

    def test_so_quem_tem_custos_ve(self):
        a = self.admin()
        with app.app_context():
            u = User.query.filter_by(email="admin@exemplo.com").one()
            u.permissoes = "vendas"
            db.session.commit()
        r = a.get(PAINEL)
        self.assertEqual(r.status_code, 302)


if __name__ == "__main__":
    unittest.main()
