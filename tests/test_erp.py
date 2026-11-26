"""ERP: histórico de estoque e inventário, compras com custo médio, lucro por pedido e financeiro."""
import re
import unittest
from datetime import timedelta
from decimal import Decimal

from test_b2b import BaseB2B

from app import app  # noqa: E402
import credito  # noqa: E402
import lucro  # noqa: E402
from b2b import hoje  # noqa: E402
from models import (Compra, Fornecedor, Lancamento, MovimentoEstoque, Order, OrderItem, Product,  # noqa: E402
                    ProdutoFornecedor, User, db)


class BaseERP(BaseB2B):
    def setUp(self):
        super().setUp()
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=20, estoque_alerta=0)
        with app.app_context():  # sem os pedidos de demonstração do catálogo de exemplo: contas e lucro limpos
            OrderItem.query.delete()
            Order.query.delete()
            db.session.commit()
            self.mov_inicial = db.session.query(db.func.max(MovimentoEstoque.id)).scalar() or 0

    def movimentos(self, pid=1):
        """Movimentos de estoque do produto feitos durante o teste."""
        with app.app_context():
            return [(m.tipo, m.quantidade, m.saldo, m.order_id) for m in
                    MovimentoEstoque.query.filter(MovimentoEstoque.product_id == pid,
                                                  MovimentoEstoque.id > self.mov_inicial)
                    .order_by(MovimentoEstoque.id).all()]

    def estoque(self, pid=1):
        with app.app_context():
            return db.session.get(Product, pid).stock

    def fornecedor_com_preco(self, a, pid=1, preco="10", ipi="5"):
        self.post(a, "/admin/fornecedores", {"nome": "Aços Ltda", "transporte": "transportadora", "frete_pct": "2",
                                             "ativo": "on", "telefone": "(11) 98888-7777", "email": "vendas@acos.com"},
                  "/admin/fornecedores")
        with app.app_context():
            fid = Fornecedor.query.one().id
        self.post(a, f"/admin/fornecedores/{fid}", {"acao": "incluir", "produto": f"#{pid}", "preco_compra": preco,
                                                     "ipi_pct": ipi}, f"/admin/fornecedores/{fid}")
        return fid

    def compra_recebida(self, a, fid, qtd="80", preco="11", condicao="30/60"):
        r = self.post(a, "/admin/compras", {"acao": "nova", "fornecedor_id": str(fid)}, "/admin/compras")
        cid = int(re.search(r"/admin/compras/(\d+)", r.headers["Location"]).group(1))
        self.post(a, f"/admin/compras/{cid}", {"acao": "incluir", "produto": "#1", "qtd": qtd}, f"/admin/compras/{cid}")
        with app.app_context():
            iid = db.session.get(Compra, cid).itens[0].id
        self.post(a, f"/admin/compras/{cid}", {"acao": "receber", f"recebido_{iid}": qtd, f"preco_{iid}": preco,
                                                "frete": "0", "condicao": condicao, "nota": "123",
                                                "atualizar_precos": "on"}, f"/admin/compras/{cid}")
        return cid

    def contas(self, tipo, oid=None):
        with app.app_context():
            q = Lancamento.query.filter_by(tipo=tipo)
            if oid:
                q = q.filter_by(order_id=oid)
            return [(l.parcela, l.vencimento, l.valor, l.pago_em) for l in q.order_by(Lancamento.vencimento).all()]


class TesteEstoque(BaseERP):
    def test_venda_e_cancelamento_ficam_no_historico(self):
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 3)
        self.fechar(c, "pix")
        oid = self.ultimo_pedido()[0]
        a = self.admin()
        self.post(a, f"/admin/orders/{oid}/status", {"status": "cancelado"}, "/admin/orders")
        self.assertEqual(self.movimentos(), [("venda", -3, 17, oid), ("cancelamento", 3, 20, oid)])
        html = a.get("/admin/estoque/1").get_data(as_text=True)
        self.assertIn(f"Pedido #{oid}", html)
        self.assertIn("Cancelamento", html)

    def test_mudanca_pelo_cadastro_e_ajuste_registram_quem_fez(self):
        a = self.admin()
        with app.app_context():
            p = db.session.get(Product, 1)
            form = {"name": p.name, "price": "189,90", "stock": "25", "category_id": str(p.category_id),
                    "qtd_minima": "1", "multiplo": "1", "active": "on"}
        self.post(a, "/admin/products/1/edit", form, "/admin/products/1/edit")
        self.post(a, "/admin/estoque/1", {"modo": "saida", "quantidade": "30", "motivo": "Perda ou avaria"},
                  "/admin/estoque/1")
        self.assertEqual(self.estoque(), 25, "saída maior que o estoque é recusada")
        self.post(a, "/admin/estoque/1", {"modo": "saida", "quantidade": "2", "motivo": "Perda ou avaria",
                                          "obs": "caixa molhada"}, "/admin/estoque/1")
        self.assertEqual(self.estoque(), 23)
        with app.app_context():
            ms = MovimentoEstoque.query.filter(MovimentoEstoque.product_id == 1,
                                               MovimentoEstoque.id > self.mov_inicial).order_by(MovimentoEstoque.id).all()
            self.assertEqual([(m.tipo, m.quantidade, m.saldo) for m in ms], [("ajuste", 5, 25), ("ajuste", -2, 23)])
            self.assertEqual(ms[1].autor, "Admin")
            self.assertIn("caixa molhada", ms[1].observacao)

    def test_inventario_grava_so_o_que_mudou(self):
        a = self.admin()
        with app.app_context():
            estoque2 = db.session.get(Product, 2).stock
        self.post(a, "/admin/estoque", {"contado_1": "15", "contado_2": str(estoque2), "obs": "Fim do mês"},
                  "/admin/estoque")
        self.assertEqual(self.estoque(), 15)
        self.assertEqual(self.movimentos(), [("inventario", -5, 15, None)])
        self.assertEqual(self.movimentos(2), [])
        r = self.post(a, "/admin/estoque", {"contado_1": "abc"}, "/admin/estoque")
        self.assertIn("número inteiro", a.get(r.headers["Location"]).get_data(as_text=True))


class TesteCompras(BaseERP):
    def test_sugestao_vira_pedido_de_compra(self):
        a = self.admin()
        fid = self.fornecedor_com_preco(a)
        self.ajustar_produto(1, estoque_alerta=100)
        html = a.get("/admin/compras").get_data(as_text=True)
        self.assertIn('name="qtd_1" inputmode="numeric" value="80"', html)  # 100 de alerta − 20 em estoque
        r = self.post(a, "/admin/compras", {"acao": "sugestao", "fornecedor_id": str(fid), "qtd_1": "80"},
                      "/admin/compras")
        cid = int(re.search(r"/admin/compras/(\d+)", r.headers["Location"]).group(1))
        with app.app_context():
            c = db.session.get(Compra, cid)
            self.assertEqual([(i.quantidade, i.preco, i.ipi_pct) for i in c.itens], [(80, Decimal(10), Decimal(5))])
        html = a.get(f"/admin/compras/{cid}").get_data(as_text=True)
        self.assertIn("https://wa.me/5511988887777?text=", html)
        self.assertIn("mailto:vendas@acos.com", html)
        self.assertNotIn('name="qtd_1"', a.get("/admin/compras").get_data(as_text=True), "já está em compra")
        self.assertEqual(a.get(f"/admin/compras/{cid}/imprimir").status_code, 200)

    def test_recebimento_estoque_custo_medio_e_contas_a_pagar(self):
        a = self.admin()
        fid = self.fornecedor_com_preco(a)
        cid = self.compra_recebida(a, fid)
        self.assertEqual(self.estoque(), 100)
        with app.app_context():
            p = db.session.get(Product, 1)
            self.assertEqual(p.custo_medio, Decimal("11.7700"))  # 11 + 5% IPI + 2% de frete
            self.assertEqual(ProdutoFornecedor.query.one().preco_compra, Decimal(11), "preço do fornecedor atualizado")
            self.assertEqual(db.session.get(Compra, cid).status, "recebido")
        self.assertEqual(self.movimentos()[-1][:3], ("compra", 80, 100))
        self.assertEqual(self.contas("pagar"), [("1/2", hoje() + timedelta(days=30), Decimal("462.00"), None),
                                                ("2/2", hoje() + timedelta(days=60), Decimal("462.00"), None)])
        # segunda compra mais barata: o custo médio fica no meio
        self.compra_recebida(a, fid, qtd="100", preco="9", condicao="")
        with app.app_context():
            self.assertEqual(db.session.get(Product, 1).custo_medio, Decimal("10.7000"))  # (100×11,77 + 100×9,63) ÷ 200
        r = self.post(a, f"/admin/compras/{cid}", {"acao": "receber"}, f"/admin/compras/{cid}")
        self.assertIn("já foi recebida", a.get(r.headers["Location"]).get_data(as_text=True))
        self.assertEqual(self.estoque(), 200)

    def test_fornecedor_com_compras_nao_e_excluido(self):
        a = self.admin()
        fid = self.fornecedor_com_preco(a)
        self.compra_recebida(a, fid)
        self.post(a, f"/admin/fornecedores/{fid}/excluir", {}, f"/admin/fornecedores/{fid}")
        with app.app_context():
            self.assertIsNotNone(db.session.get(Fornecedor, fid))


class TesteLucro(BaseERP):
    def test_custo_guardado_na_venda_e_lucro_do_periodo(self):
        a = self.admin()
        fid = self.fornecedor_com_preco(a)
        self.compra_recebida(a, fid)
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 2)
        self.adicionar(c, 2, 1)  # sem custo cadastrado
        self.fechar(c, "pix")
        oid = self.ultimo_pedido()[0]
        with app.app_context():
            o = db.session.get(Order, oid)
            custos = {i.product_id: i.custo_unitario for i in o.items}
            self.assertEqual(custos, {1: Decimal("11.7700"), 2: None})
            r = lucro.do_pedido(o)
            self.assertEqual(r["custo"], Decimal("23.54"))
            self.assertEqual(r["lucro"], Decimal("189.90") * 2 + Decimal("149.90") - Decimal("23.54"))
            self.assertEqual(len(r["sem_custo"]), 1)
        html = a.get("/admin/lucro").get_data(as_text=True)
        self.assertIn("R$ 506,16", html)
        self.assertIn("1 item(ns) vendido(s) sem custo", html)
        self.assertIn("Lucro estimado", a.get(f"/admin/orders/{oid}").get_data(as_text=True))
        # custo de hoje para o item antigo
        self.post(a, f"/admin/fornecedores/{fid}", {"acao": "incluir", "produto": "#2", "preco_compra": "50"},
                  f"/admin/fornecedores/{fid}")
        self.post(a, "/admin/lucro/preencher", {"periodo": "mes", "situacao": "vendas"}, "/admin/lucro")
        with app.app_context():
            item = [i for i in db.session.get(Order, oid).items if i.product_id == 2][0]
            self.assertEqual(item.custo_unitario, Decimal("51.0000"))  # 50 + 2% de frete
        r = a.get("/admin/lucro.xlsx")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r.headers["Content-Type"])


class TesteFinanceiro(BaseERP):
    def test_faturado_gera_parcelas_e_pago_baixa_todas(self):
        c = self.empresa()
        self.adicionar(c, 1, 3)
        self.fechar(c, "faturado", prazo="30/60/90")
        oid, _, total, *_ = self.ultimo_pedido()
        parcelas = self.contas("receber", oid)
        self.assertEqual([p[0] for p in parcelas], ["1/3", "2/3", "3/3"])
        self.assertEqual(sum(p[2] for p in parcelas), total)
        self.assertEqual(parcelas[0][1], hoje() + timedelta(days=30))
        a = self.admin()
        self.post(a, f"/admin/orders/{oid}/status", {"status": "pago"}, "/admin/orders")
        self.assertTrue(all(p[3] == hoje() for p in self.contas("receber", oid)))

    def test_cancelar_apaga_as_parcelas_em_aberto(self):
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        oid = self.ultimo_pedido()[0]
        self.assertEqual(len(self.contas("receber", oid)), 1)
        self.post(self.admin(), f"/admin/orders/{oid}/status", {"status": "cancelado"}, "/admin/orders")
        self.assertEqual(self.contas("receber", oid), [])

    def test_receber_pelo_financeiro_quita_o_pedido_e_estorno_volta(self):
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        oid = self.ultimo_pedido()[0]
        with app.app_context():
            lid = Lancamento.query.filter_by(order_id=oid).one().id
        a = self.admin()
        self.post(a, f"/admin/financeiro/{lid}", {"acao": "baixar", "pago_em": hoje().isoformat(), "forma": "pix"},
                  "/admin/financeiro")
        self.assertEqual(self.ultimo_pedido()[1], "pago")
        html = a.get("/admin/fluxo-de-caixa").get_data(as_text=True)
        self.assertIn("R$ 189,90", html)
        self.post(a, f"/admin/financeiro/{lid}", {"acao": "estornar"}, "/admin/financeiro")
        self.assertEqual(self.ultimo_pedido()[1], "pendente")
        self.assertIsNone(self.contas("receber", oid)[0][3])
        # parcela de pedido não se exclui pelo financeiro
        self.post(a, f"/admin/financeiro/{lid}", {"acao": "excluir"}, "/admin/financeiro")
        self.assertEqual(len(self.contas("receber", oid)), 1)

    def test_despesa_repetida_e_excluida(self):
        a = self.admin()
        self.post(a, "/admin/financeiro", {"tipo": "pagar", "descricao": "Aluguel do galpão", "categoria": "Aluguel",
                                           "valor": "1.500,00", "vencimento": "2026-01-31", "repetir": "3"},
                  "/admin/financeiro")
        venc = [p[1].isoformat() for p in self.contas("pagar")]
        self.assertEqual(venc, ["2026-01-31", "2026-02-28", "2026-03-31"])
        with app.app_context():
            lid = Lancamento.query.filter_by(tipo="pagar").first().id
        self.post(a, f"/admin/financeiro/{lid}", {"acao": "excluir"}, "/admin/financeiro")
        self.assertEqual(len(self.contas("pagar")), 2)
        html = a.get("/admin/financeiro?tipo=pagar").get_data(as_text=True)
        self.assertIn("Aluguel do galpão", html)

    def test_pedidos_antigos_ganham_parcelas(self):
        with app.app_context():
            o = Order(customer_name="Antigo", total=Decimal("300.00"), status="pago", payment_method="pix")
            db.session.add(o)
            db.session.commit()
            oid = o.id
        self.admin().get("/admin/financeiro")
        parcelas = self.contas("receber", oid)
        self.assertEqual(len(parcelas), 1)
        self.assertIsNotNone(parcelas[0][3], "pedido pago entra como recebido")

    def test_boleto_vencido_bloqueia_o_faturado(self):
        c = self.empresa()
        self.adicionar(c, 1, 1)
        self.fechar(c, "faturado", prazo="28")
        oid = self.ultimo_pedido()[0]
        with app.app_context():
            l = Lancamento.query.filter_by(order_id=oid).one()
            l.vencimento = hoje() - timedelta(days=10)
            db.session.commit()
        a = self.admin()
        self.post(a, "/admin/financeiro", {"acao": "config", "bloqueio_atraso_dias": "5"}, "/admin/financeiro")
        with app.app_context():
            u = User.query.filter_by(email="empresa@teste.com").one()
            self.assertEqual(credito.dias_de_atraso(u), 10)
            self.assertFalse(credito.cabe(u, Decimal(1)))
        self.adicionar(c, 1, 1)
        antes = self.ultimo_pedido()[0]
        r = self.fechar(c, "faturado", prazo="28")
        self.assertIn("boleto vencido", r.get_data(as_text=True))
        self.assertEqual(self.ultimo_pedido()[0], antes)
        self.post(a, "/admin/financeiro", {"acao": "config", "bloqueio_atraso_dias": "0"}, "/admin/financeiro")
        with app.app_context():
            self.assertTrue(credito.cabe(User.query.filter_by(email="empresa@teste.com").one(), Decimal(1)))


class TestePainelERP(BaseERP):
    def entrar_como(self, permissoes):
        with app.app_context():
            u = User(name="Equipe", email="eq@loja-exemplo.com", is_admin=True, email_verified=True, permissoes=permissoes)
            u.set_password("senha-equipe-123")
            db.session.add(u)
            db.session.commit()
        v = self.cliente()
        self.post(v, "/admin/login", {"email": "eq@loja-exemplo.com", "password": "senha-equipe-123"}, "/admin/login")
        return v

    def test_permissoes_das_areas_novas(self):
        v = self.entrar_como("financeiro")
        self.assertEqual(v.get("/admin/financeiro").status_code, 200)
        self.assertEqual(v.get("/admin/lucro").status_code, 200)
        self.assertEqual(v.get("/admin/compras").status_code, 302)
        self.assertEqual(v.get("/admin/estoque").status_code, 302)
        w = self.cliente()
        with app.app_context():
            u = User.query.filter_by(email="eq@loja-exemplo.com").one()
            u.permissoes = "produtos"
            db.session.commit()
        self.post(w, "/admin/login", {"email": "eq@loja-exemplo.com", "password": "senha-equipe-123"}, "/admin/login")
        html = w.get("/admin/estoque").get_data(as_text=True)
        self.assertNotIn("Valor do estoque", html, "sem a área de custos, não vê custo")
        self.assertNotIn(">Financeiro<", html)
        self.assertEqual(w.get("/admin/financeiro").status_code, 302)

    def test_telas_abrem(self):
        a = self.admin()
        fid = self.fornecedor_com_preco(a)
        cid = self.compra_recebida(a, fid)
        for url in ("/admin/estoque", "/admin/estoque?filtro=baixo", "/admin/estoque/1", "/admin/compras",
                    "/admin/compras?status=todas", f"/admin/compras/{cid}", f"/admin/compras/{cid}/imprimir",
                    "/admin/financeiro", "/admin/financeiro?tipo=pagar&situacao=pagas", "/admin/fluxo-de-caixa",
                    "/admin/lucro", "/admin/lucro?aba=estado", "/admin/manual"):
            self.assertEqual(a.get(url).status_code, 200, url)
        menu = a.get("/admin/financeiro").get_data(as_text=True)
        for grupo in ("Loja", "Gestão", "Configurações"):
            self.assertIn(f'<span class="side-grupo">{grupo}</span>', menu)


if __name__ == "__main__":
    unittest.main()
