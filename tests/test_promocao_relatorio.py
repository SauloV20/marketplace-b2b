"""Testes da promoção com data e do relatório de vendas."""
import io
import unittest
from datetime import timedelta
from decimal import Decimal

from test_b2b import BaseB2B

from app import app  # noqa: E402
from b2b import hoje, preco_unitario, promocao_ativa  # noqa: E402
import relatorios  # noqa: E402
from models import Order, Product, User, agora_utc, db  # noqa: E402


class TestePromocao(BaseB2B):
    def promo(self, pid, preco, inicio=None, fim=None):
        self.ajustar_produto(pid, preco_promocional=Decimal(preco), promo_inicio=inicio, promo_fim=fim)

    def test_periodo_e_menor_preco_sem_somar(self):
        d = hoje()
        with app.app_context():
            p = db.session.get(Product, 1)
            normal = p.price
        self.promo(1, normal - 20, d - timedelta(days=1), d + timedelta(days=5))
        with app.app_context():
            p = db.session.get(Product, 1)
            self.assertTrue(promocao_ativa(p))
            self.assertEqual(preco_unitario(p, 1), normal - 20)
            self.assertFalse(promocao_ativa(p, d + timedelta(days=6)), "acaba sozinha depois do último dia")
            self.assertFalse(promocao_ativa(p, d - timedelta(days=2)), "ainda não começou")
            # empresa com 50% de desconto: paga o menor, sem somar a promoção
            u = User(name="X", email="x@x.com", tipo="pj", empresa_aprovada=True, desconto=Decimal("50"),
                     password_hash="x")
            self.assertEqual(preco_unitario(p, 1, u), (normal / 2).quantize(Decimal("0.01")))
        self.promo(1, normal - 20, d + timedelta(days=1))
        with app.app_context():
            self.assertEqual(preco_unitario(db.session.get(Product, 1), 1), normal)

    def test_vitrine_menu_e_carrinho(self):
        d = hoje()
        self.ajustar_produto(2, qtd_minima=1, multiplo=1)
        with app.app_context():
            normal = db.session.get(Product, 2).price
        visitante = self.cliente().get("/").get_data(as_text=True)
        self.assertNotIn("% Ofertas", visitante)
        self.promo(2, "99.90", d, d + timedelta(days=3))
        visitante = self.cliente().get("/").get_data(as_text=True)
        self.assertIn("% Ofertas", visitante)
        self.assertIn('selo oferta', visitante)
        self.assertNotIn("99,90", visitante, "visitante não vê preço nem na oferta")
        c = self.cadastrar()
        ofertas = c.get("/?ofertas=1").get_data(as_text=True)
        self.assertIn("R$ 99,90", ofertas)
        self.assertIn(f"Oferta até {(d + timedelta(days=3)):%d/%m}", ofertas)
        self.assertEqual(ofertas.count('class="card'), 1, "só os produtos em oferta")
        self.adicionar(c, 2, 2)
        self.fechar(c, "pix")
        with app.app_context():
            o = Order.query.order_by(Order.id.desc()).first()
            self.assertEqual(o.items[0].unit_price, Decimal("99.90"))
        self.assertLess(Decimal("99.90"), normal)

    def test_painel_valida(self):
        a = self.admin()
        with app.app_context():
            p = db.session.get(Product, 3)
            form = {"name": p.name, "category_id": p.category_id, "price": "100,00", "stock": "10", "active": "on",
                    "qtd_minima": "1", "multiplo": "1"}
        self.post(a, "/admin/products/3/edit", {**form, "preco_promocional": "120,00"}, "/admin/products/3/edit")
        with app.app_context():
            self.assertIsNone(db.session.get(Product, 3).preco_promocional, "promo maior que o preço é recusada")
        self.post(a, "/admin/products/3/edit", {**form, "preco_promocional": "79,90", "promo_inicio": "2030-01-10",
                                                "promo_fim": "2030-01-01"}, "/admin/products/3/edit")
        with app.app_context():
            self.assertIsNone(db.session.get(Product, 3).preco_promocional, "fim antes do início é recusado")
        self.post(a, "/admin/products/3/edit", {**form, "preco_promocional": "79,90", "promo_fim": "2030-01-31"},
                  "/admin/products/3/edit")
        with app.app_context():
            p = db.session.get(Product, 3)
            self.assertEqual((p.preco_promocional, str(p.promo_fim)), (Decimal("79.90"), "2030-01-31"))
        self.assertIn("Valendo agora", a.get("/admin/products/3/edit").get_data(as_text=True))
        self.post(a, "/admin/products/3/edit", {**form, "preco_promocional": ""}, "/admin/products/3/edit")
        with app.app_context():
            self.assertIsNone(db.session.get(Product, 3).preco_promocional, "apagar o campo tira a promoção")


class TesteRelatorio(BaseB2B):
    def test_resumo_produtos_clientes_e_excel(self):
        from openpyxl import load_workbook
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=100)
        c = self.cadastrar()
        self.adicionar(c, 1, 3)
        self.fechar(c, "pix")
        a = self.admin()
        html = a.get("/admin/relatorios?periodo=mes").get_data(as_text=True)
        self.assertIn("Relatório de vendas", html)
        self.assertIn("Fresa de Topo", html)
        self.assertIn("Cliente Teste", html)
        with app.app_context():
            r = relatorios.montar(hoje().replace(day=1), hoje())
            fresa = next(x for x in r["produtos"] if x["p"].id == 1)
            self.assertGreaterEqual(fresa["qtd"], 3)
            self.assertEqual(r["produtos"][0]["classe"], "A")
            self.assertEqual(r["ticket"], (r["total"] / r["pedidos"]).quantize(Decimal("0.01")))
            so_pagos = relatorios.montar(hoje().replace(day=1), hoje(), "pagos")
            self.assertLessEqual(so_pagos["total"], r["total"])
        x = a.get("/admin/relatorios.xlsx?periodo=mes")
        self.assertIn("attachment", x.headers["Content-Disposition"])
        wb = load_workbook(io.BytesIO(x.data))
        self.assertEqual(wb.sheetnames, ["Resumo", "Produtos", "Clientes", "Pagamento", "Pararam de comprar"])
        self.assertEqual(self.cliente().get("/admin/relatorios").status_code, 302, "só para o administrador")

    def test_periodo_personalizado_e_inativos(self):
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        with app.app_context():
            o = Order.query.order_by(Order.id.desc()).first()
            o.created_at = agora_utc() - timedelta(days=100)
            db.session.commit()
            dia = (o.created_at - timedelta(hours=3)).date()
            r = relatorios.montar(dia, dia)
            self.assertEqual(r["pedidos"], 1)
            nomes = [x["u"].email for x in relatorios.inativos()]
            self.assertIn("cli@teste.com", nomes)
        html = self.admin().get(f"/admin/relatorios?de={dia}&ate={dia}").get_data(as_text=True)
        self.assertIn("Pararam de comprar", html)
        self.assertIn("há 100 dias", html)
        de, ate, atalho = relatorios.periodo({"de": "2030-02-10", "ate": "2030-02-01"})
        self.assertEqual((str(de), str(ate), atalho), ("2030-02-01", "2030-02-10", ""), "datas trocadas se acertam")


if __name__ == "__main__":
    unittest.main()
