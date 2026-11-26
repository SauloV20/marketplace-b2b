"""Fornecedores, custo, impostos por estado, markup, transporte e o preço da loja pelo estado de entrega."""
import re
import unittest
from decimal import Decimal

from test_b2b import BaseB2B

from app import app  # noqa: E402
import formacao  # noqa: E402
import frete  # noqa: E402
from models import Endereco, Fornecedor, ImpostoUF, Order, Product, ProdutoFornecedor, User, db  # noqa: E402

CEP_SP, CEP_MG, CEP_BA, CEP_RJ = "01001-000", "30140-071", "40020-000", "20040-002"
CONFIG = {"loja_uf": "SP", "imp_federais": "10", "despesas_venda": "0", "markup_padrao": "30",
          "icms_SP": "18", "icms_MG": "12", "icms_BA": "7", "entrega_RJ": "propria", "custo_entrega_RJ": "5"}


class BaseFormacao(BaseB2B):
    def configurar(self, a, **extra):
        return self.post(a, "/admin/formacao-de-preco", {**CONFIG, **extra}, "/admin/formacao-de-preco")

    def novo_fornecedor(self, a, **campos):
        dados = {"nome": "Aços Paulista Ltda", "transporte": "transportadora", "frete_pct": "2", "credito_pct": "0",
                 "ativo": "on", **campos}
        r = self.post(a, "/admin/fornecedores", dados, "/admin/fornecedores")
        return int(re.search(r"/admin/fornecedores/(\d+)", r.headers["Location"]).group(1))

    def custo_automatico(self, a, pid, fid, preco="10", ipi="5", markup=""):
        """Inclui o preço de compra e liga o preço calculado. Devolve o id do ProdutoFornecedor."""
        self.post(a, f"/admin/produtos/{pid}/custo", {"acao": "incluir", "fornecedor_id": str(fid),
                                                      "preco_compra": preco, "ipi_pct": ipi}, "/admin/fornecedores")
        with app.app_context():
            cid = ProdutoFornecedor.query.filter_by(product_id=pid, fornecedor_id=fid).one().id
        self.post(a, f"/admin/produtos/{pid}/custo", {"acao": "salvar", f"preco_compra_{cid}": preco,
                                                      f"ipi_pct_{cid}": ipi, "principal": str(cid), "markup": markup,
                                                      "preco_auto": "on"}, "/admin/fornecedores")
        return cid

    def preco(self, pid):
        with app.app_context():
            return db.session.get(Product, pid).price

    def montar(self):
        """Loja em SP, produto 1 com preço calculado: compra 10 + IPI 5% + frete 2% = custo 10,70."""
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)
        a = self.admin()
        self.configurar(a)
        fid = self.novo_fornecedor(a)
        cid = self.custo_automatico(a, 1, fid)
        return a, fid, cid


class TesteCalculo(BaseFormacao):
    def test_preco_sai_do_custo_markup_e_impostos(self):
        a, fid, _ = self.montar()
        # 10,70 × 1,30 ÷ (1 − 0,28) = 19,32
        self.assertEqual(self.preco(1), Decimal("19.32"))
        html = a.get("/admin/produtos/1/custo").get_data(as_text=True)
        self.assertIn("R$ 10,70", html)  # custo
        self.assertIn("R$ 19,32", html)
        self.assertIn("R$ 17,83", html, "MG: 10,70 × 1,30 ÷ 0,78")

    def test_estados_sem_aliquota_usam_a_da_loja(self):
        self.montar()
        with app.app_context():
            self.assertEqual(formacao.venda_pct("PR"), Decimal(28))
            self.assertEqual(formacao.fator("PR"), Decimal(1))
            self.assertEqual(formacao.venda_pct("RJ"), Decimal(33), "frota própria soma o custo da entrega")

    def test_veiculo_proprio_divide_a_viagem_pela_compra(self):
        a = self.admin()
        self.configurar(a)
        fid = self.novo_fornecedor(a, nome="Busca Própria", transporte="proprio", frete_pct="",
                                   custo_viagem="300", compra_viagem="10.000,00", credito_pct="12")
        self.custo_automatico(a, 1, fid, preco="100", ipi="0")
        with app.app_context():
            p = db.session.get(Product, 1)
            r = formacao.calcular(p)
            self.assertEqual(r["frete"], Decimal(3))  # 300 ÷ 10.000 = 3%
            self.assertEqual(r["total"], Decimal(91))  # 100 + 3 − 12 de crédito
        self.assertEqual(self.preco(1), Decimal("164.31"))  # 91 × 1,30 ÷ 0,72

    def test_mudar_o_preco_de_compra_recalcula_e_as_faixas_acompanham(self):
        a = self.admin()
        self.configurar(a)
        fid = self.novo_fornecedor(a)
        pid = self.pid("Disco de Desbaste 7 pol")  # 18,90 com faixa de 10 un a 16,90
        cid = self.custo_automatico(a, pid, fid)
        self.assertEqual(self.preco(pid), Decimal("19.32"))
        with app.app_context():
            faixa = db.session.get(Product, pid).faixas[0].preco
        self.post(a, f"/admin/fornecedores/{fid}", {"acao": "precos", f"preco_compra_{cid}": "20",
                                                     f"ipi_pct_{cid}": "5", f"st_pct_{cid}": "", f"codigo_{cid}": ""},
                  f"/admin/fornecedores/{fid}")
        self.assertEqual(self.preco(pid), Decimal("38.64"))  # 21,40 × 1,30 ÷ 0,72
        with app.app_context():
            nova = db.session.get(Product, pid).faixas[0].preco
        self.assertEqual(nova, (faixa * Decimal("38.64") / Decimal("19.32")).quantize(Decimal("0.01")))

    def test_markup_da_categoria_e_do_produto(self):
        a, _, cid = self.montar()
        with app.app_context():
            cat = db.session.get(Product, 1).category_id
        self.configurar(a, **{f"markup_cat_{cat}": "50"})
        self.assertEqual(self.preco(1), Decimal("22.29"))  # 10,70 × 1,50 ÷ 0,72
        self.post(a, "/admin/produtos/1/custo", {"acao": "salvar", f"preco_compra_{cid}": "10", f"ipi_pct_{cid}": "5",
                                                 "principal": str(cid), "markup": "0", "preco_auto": "on"},
                  "/admin/fornecedores")
        self.assertEqual(self.preco(1), Decimal("14.86"))  # só custo e impostos
        self.assertIn("markup está em 0%", a.get("/admin/produtos/1/custo").get_data(as_text=True))

    def test_impostos_absurdos_sao_recusados(self):
        a = self.admin()
        r = self.configurar(a, imp_federais="60", icms_SP="35")
        self.assertEqual(r.status_code, 200)
        self.assertIn("não fecha", r.get_data(as_text=True))
        with app.app_context():
            self.assertEqual(ImpostoUF.query.count(), 0)

    def test_sem_fornecedor_nao_liga_o_preco_calculado(self):
        a = self.admin()
        self.post(a, "/admin/produtos/1/custo", {"acao": "salvar", "preco_auto": "on"}, "/admin/fornecedores")
        with app.app_context():
            self.assertFalse(db.session.get(Product, 1).preco_auto)
        self.assertEqual(self.preco(1), Decimal("189.90"))

    def test_icms_interestadual_preenchido(self):
        a = self.admin()
        self.configurar(a, icms_MG="", icms_BA="")
        self.post(a, "/admin/formacao-de-preco", {"acao": "icms_interestadual"}, "/admin/formacao-de-preco")
        with app.app_context():
            aliq = {r.uf: r.icms for r in ImpostoUF.query.all()}
        self.assertEqual(aliq["MG"], Decimal(12))
        self.assertEqual(aliq["BA"], Decimal(7))
        self.assertEqual(aliq["ES"], Decimal(7))
        self.assertEqual(aliq["SP"], Decimal(18), "o estado da loja fica com a alíquota interna")

    def test_excluir_fornecedor_mantem_o_preco_e_avisa(self):
        a, fid, _ = self.montar()
        self.post(a, f"/admin/fornecedores/{fid}/excluir", {}, f"/admin/fornecedores/{fid}")
        with app.app_context():
            self.assertEqual(ProdutoFornecedor.query.count(), 0)
            self.assertIsNone(db.session.get(Fornecedor, fid))
        self.assertEqual(self.preco(1), Decimal("19.32"))
        self.assertIn("sem preço de compra", a.get("/admin/formacao-de-preco").get_data(as_text=True))


class TesteLojaPorEstado(BaseFormacao):
    def carrinho(self, c):
        return c.get("/carrinho").get_data(as_text=True)

    def test_preco_muda_com_o_estado_de_entrega(self):
        self.montar()
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 1)
        self.assertIn("R$ 19,32", self.carrinho(c))
        for cep, valor in ((CEP_MG, "R$ 17,83"), (CEP_BA, "R$ 16,76"), (CEP_RJ, "R$ 20,76")):
            self.post(c, "/carrinho/cep", {"cep": cep}, "/carrinho")
            html = self.carrinho(c)
            self.assertIn(valor, html, cep)
            self.assertIn("Preços para entrega em", html)
            self.assertNotIn("R$ 19,32", html, "o preço de SP não aparece riscado como se fosse desconto")
            self.assertNotIn("R$ 19,32", c.get("/").get_data(as_text=True))
        # produto com preço manual: o mesmo em todo o Brasil
        self.adicionar(c, 2, 1)
        self.assertIn("R$ 149,90", self.carrinho(c))

    def test_entrega_propria_frete_gratis(self):
        self.montar()
        with app.app_context():
            r = frete.calcular(CEP_RJ, Decimal(100), Decimal(5))
            self.assertTrue(r["gratis"])
            self.assertEqual(r["regra"], "Entrega própria")
            self.assertIsNone(frete.calcular(CEP_MG, Decimal(100), Decimal(5)), "MG segue as regras de frete")

    def test_finalizacao_usa_o_estado_do_cep_e_avisa_antes_de_gravar(self):
        self.montar()
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 2)
        self.post(c, "/carrinho/cep", {"cep": CEP_MG}, "/carrinho")
        with app.app_context():
            antes = Order.query.count()
        # o cliente viu os preços de MG, mas confirmou com um CEP de SP: mostra os preços novos antes de gravar
        r = self.fechar(c, "pix", cep=CEP_SP)
        self.assertEqual(r.status_code, 200)
        self.assertIn("preços foram atualizados para entrega em SP", r.get_data(as_text=True))
        with app.app_context():
            self.assertEqual(Order.query.count(), antes)
        self.fechar(c, "pix", cep=CEP_SP)
        self.assertEqual(self.ultimo_pedido()[5], [(2, Decimal("19.32"))])
        # mesmo estado do carrinho: grava direto com o preço de MG
        self.adicionar(c, 1, 1)
        self.post(c, "/carrinho/cep", {"cep": CEP_MG}, "/carrinho")
        self.fechar(c, "pix", cep=CEP_MG)
        self.assertEqual(self.ultimo_pedido()[5], [(1, Decimal("17.83"))])

    def test_cep_digitado_na_finalizacao_refaz_o_resumo(self):
        self.montar()
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 1)
        j = c.get(f"/carrinho/frete?cep={CEP_BA}").get_json()
        self.assertIn("R$ 16,76", j["resumo"])
        self.assertIn("BA", j["resumo"])
        self.assertEqual(c.get(f"/carrinho/frete?cep={CEP_BA}").get_json()["resumo"], "", "nada mudou: sem resumo")

    def test_endereco_principal_define_o_estado(self):
        self.montar()
        c = self.cadastrar("cli@teste.com")
        self.post(c, "/conta/enderecos", {"acao": "novo", "apelido": "Filial", "cep": CEP_BA,
                                          "endereco": "Av. Sete, 100, Salvador"}, "/conta/enderecos")
        with app.app_context():
            uid = User.query.filter_by(email="cli@teste.com").one().id
            self.assertEqual(Endereco.query.filter_by(user_id=uid).count(), 1)
        self.adicionar(c, 1, 1)
        self.assertIn("R$ 16,76", self.carrinho(c))

    def test_pedido_editado_pelo_painel_usa_o_estado_do_pedido(self):
        a, _, _ = self.montar()
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 2, 1)
        self.post(c, "/carrinho/cep", {"cep": CEP_MG}, "/carrinho")
        self.fechar(c, "pix", cep=CEP_MG)
        oid, *_ = self.ultimo_pedido()
        with app.app_context():
            item = db.session.get(Order, oid).items[0].id
        self.post(a, f"/admin/orders/{oid}/editar", {f"qtd_{item}": "1", f"preco_{item}": "149,90",
                                                      "novo_produto": "#1", "novo_qtd": "1"}, f"/admin/orders/{oid}")
        with app.app_context():
            precos = sorted(i.unit_price for i in db.session.get(Order, oid).items)
        self.assertEqual(precos, [Decimal("17.83"), Decimal("149.90")])


class TestePainel(BaseFormacao):
    def test_quem_so_cuida_de_produtos_nao_ve_custos(self):
        with app.app_context():
            u = User(name="Cad", email="cad@loja-exemplo.com", is_admin=True, email_verified=True, permissoes="produtos")
            u.set_password("senha-cadastro-123")
            db.session.add(u)
            db.session.commit()
        v = self.cliente()
        self.post(v, "/admin/login", {"email": "cad@loja-exemplo.com", "password": "senha-cadastro-123"}, "/admin/login")
        self.assertNotIn("/admin/fornecedores", v.get("/admin/fornecedores").headers.get("Location", ""))
        self.assertEqual(v.get("/admin/fornecedores").status_code, 302)
        self.assertEqual(v.get("/admin/produtos/1/custo").status_code, 302)
        html = v.get("/admin/products/1/edit").get_data(as_text=True)
        self.assertNotIn("/custo", html)
        self.assertNotIn("Fornecedores", v.get("/admin/products").get_data(as_text=True))

    def test_preco_calculado_nao_muda_pelo_formulario_nem_pela_planilha(self):
        a, _, _ = self.montar()
        html = a.get("/admin/products/1/edit").get_data(as_text=True)
        self.assertIn("calculado pelo custo", html)
        with app.app_context():
            p = db.session.get(Product, 1)
            form = {"name": p.name, "price": "1,00", "stock": "50", "category_id": str(p.category_id),
                    "qtd_minima": "1", "multiplo": "1", "active": "on"}
        self.post(a, "/admin/products/1/edit", form, "/admin/products/1/edit")
        self.assertEqual(self.preco(1), Decimal("19.32"))
        import planilha
        with app.app_context():
            planilha.aplicar([{"linha": 2, "acao": "atualizar", "pid": 1, "dados": {"price": Decimal("5")},
                               "nome": "x"}])
            db.session.commit()
        self.assertEqual(self.preco(1), Decimal("19.32"))

    def test_telas_abrem(self):
        a, fid, _ = self.montar()
        for url in ("/admin/fornecedores", f"/admin/fornecedores/{fid}", "/admin/formacao-de-preco",
                    "/admin/produtos/1/custo", "/admin/produtos/2/custo", "/admin/products"):
            r = a.get(url)
            self.assertEqual(r.status_code, 200, url)
        html = a.get(f"/admin/fornecedores/{fid}").get_data(as_text=True)
        self.assertIn("Fresa de Topo", html)
        self.assertIn("usado", html)

    def test_fornecedor_com_cnpj_invalido_e_recusado(self):
        a = self.admin()
        r = self.post(a, "/admin/fornecedores", {"nome": "X", "documento": "11.111.111/1111-11",
                                                  "transporte": "fornecedor", "ativo": "on"}, "/admin/fornecedores")
        self.assertEqual(r.status_code, 200)
        self.assertIn("CNPJ", r.get_data(as_text=True))
        self.assertIn('value="X"', r.get_data(as_text=True), "o que foi digitado continua no formulário")
        with app.app_context():
            self.assertEqual(Fornecedor.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
