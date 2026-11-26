"""Testes da importação e exportação de produtos/pedidos por planilha (Excel e CSV)."""
import io
import unittest
from decimal import Decimal

from openpyxl import Workbook, load_workbook

from test_b2b import BaseB2B

from app import app  # noqa: E402
from models import Category, ImportacaoPendente, Product, db  # noqa: E402


def xlsx(linhas):
    wb = Workbook()
    for linha in linhas:
        wb.active.append(linha)
    saida = io.BytesIO()
    wb.save(saida)
    return saida.getvalue()


class TestePlanilha(BaseB2B):
    def enviar(self, a, conteudo, nome="produtos.xlsx", desativar=False):
        dados = {"_csrf": self.token(a, "/admin/planilhas"), "arquivo": (io.BytesIO(conteudo), nome)}
        if desativar:
            dados["desativar_ausentes"] = "on"
        return a.post("/admin/planilhas/importar", data=dados, content_type="multipart/form-data")

    def confirmar(self, a):
        with app.app_context():
            iid = ImportacaoPendente.query.order_by(ImportacaoPendente.id.desc()).first().id
        return self.post(a, f"/admin/planilhas/confirmar/{iid}", {}, "/admin/planilhas")

    def test_importa_produto_novo_e_atualiza_existente(self):
        a = self.admin()
        conteudo = xlsx([
            ["Código", "Nome", "Categoria", "Preço", "Estoque", "Múltiplo", "Fracionável", "Acréscimo", "Faixas"],
            ["LUVA-01", "Luva Nitrílica Industrial", "Luvas", "R$ 7,50", 300, 12, "sim", "10%", "120 = 6,90; 600 = 6,50"],
            [None, "Fresa de Topo Metal Duro 10 mm 4 Cortes", None, 199.9, 15, None, None, None, None],
        ])
        html = self.enviar(a, conteudo).get_data(as_text=True)
        self.assertIn("Prévia da importação", html)
        self.assertIn("Confirmar importação", html)
        with app.app_context():
            self.assertIsNone(Product.query.filter_by(codigo="LUVA-01").first(), "a prévia não grava nada")
        self.confirmar(a)
        with app.app_context():
            luva = Product.query.filter_by(codigo="LUVA-01").one()
            self.assertEqual((luva.price, luva.stock, luva.multiplo, luva.fracionavel, luva.acrescimo_fracionado),
                             (Decimal("7.50"), 300, 12, True, Decimal("10")))
            self.assertEqual([(f.qtd_minima, f.preco) for f in luva.faixas],
                             [(120, Decimal("6.90")), (600, Decimal("6.50"))])
            self.assertEqual(luva.category.name, "Luvas")
            fresa = db.session.get(Product, 1)
            self.assertEqual((fresa.price, fresa.stock, fresa.category.name), (Decimal("199.90"), 15, "Usinagem"))
        self.assertIn("1 produto(s) novo(s), 1 atualizado(s)", a.get("/admin/products").get_data(as_text=True))

    def test_erros_aparecem_por_linha_e_bloqueiam(self):
        a = self.admin()
        conteudo = xlsx([["nome", "categoria", "preco", "estoque", "multiplo"],
                         ["Produto sem preço", "X", None, 10, 1],
                         ["Produto com estoque ruim", "X", 10, "abc", 1],
                         ["Produto com múltiplo zero", "X", 10, 5, 0],
                         ["Produto com múltiplo zero", "X", 10, 5, 1]])
        html = self.enviar(a, conteudo).get_data(as_text=True)
        self.assertIn("produto novo precisa de preco", html)
        self.assertIn("valor inválido em “estoque”", html)
        self.assertIn("vai de 1 a 100.000", html)
        self.assertIn("Produto repetido", html)
        self.assertNotIn("Confirmar importação", html)

    def test_csv_do_excel_brasileiro(self):
        a = self.admin()
        csv = "nome;categoria;preco;estoque\nÓculos Âmbar;EPIs;1.234,50;7\n".encode("cp1252")
        self.enviar(a, csv, "produtos.csv")
        self.confirmar(a)
        with app.app_context():
            p = Product.query.filter_by(name="Óculos Âmbar").one()
            self.assertEqual((p.price, p.stock), (Decimal("1234.50"), 7))

    def test_desativar_os_que_nao_estao_na_planilha(self):
        a = self.admin()
        self.enviar(a, xlsx([["nome", "estoque"], ["Torno de Bancada nº 5", 3]]), desativar=True)
        self.confirmar(a)
        with app.app_context():
            ativos = Product.query.filter_by(active=True).all()
            self.assertEqual([p.name for p in ativos], ["Torno de Bancada nº 5"])
            self.assertEqual(Product.query.count(), 18, "nenhum produto é apagado")

    def test_promocao_pela_planilha(self):
        from datetime import datetime
        a = self.admin()
        with app.app_context():
            p1, p2 = db.session.get(Product, 1), db.session.get(Product, 2)
            n1, n2, preco3 = p1.name, p2.name, db.session.get(Product, 3).price
        conteudo = xlsx([
            ["nome", "Preço promocional", "Início da promoção", "Fim da promoção"],
            [n1, "149,90", "01/12/2026", datetime(2026, 12, 31)],
            [n2, "79.9", None, "31/12/26"],
        ])
        self.enviar(a, conteudo)
        self.confirmar(a)
        with app.app_context():
            p1, p2 = db.session.get(Product, 1), db.session.get(Product, 2)
            self.assertEqual((p1.preco_promocional, str(p1.promo_inicio), str(p1.promo_fim)),
                             (Decimal("149.90"), "2026-12-01", "2026-12-31"))
            self.assertEqual((p2.preco_promocional, p2.promo_inicio, str(p2.promo_fim)),
                             (Decimal("79.90"), None, "2026-12-31"))

        # exporta com a promoção e reimporta sem mudar nada
        r = a.get("/admin/planilhas/produtos.xlsx")
        ws = load_workbook(io.BytesIO(r.data)).active
        cab = [c.value for c in ws[1]]
        i = cab.index("preco_promocional")
        self.assertEqual(cab[i:i + 5], ["preco_promocional", "promo_inicio", "promo_fim", "ncm", "cest"])
        self.assertEqual(cab[-3:], ["altura_cm", "largura_cm", "comprimento_cm"])
        self.enviar(a, r.data)
        self.confirmar(a)
        with app.app_context():
            self.assertEqual(str(db.session.get(Product, 1).promo_fim), "2026-12-31")

        # remover a promoção e erros
        html = self.enviar(a, xlsx([["nome", "preco_promocional", "promo_inicio", "promo_fim"],
                                    [n1, "remover", None, None],
                                    [n2, "0", None, None]])).get_data(as_text=True)
        self.assertIn("Confirmar importação", html)
        self.confirmar(a)
        with app.app_context():
            p1, p2 = db.session.get(Product, 1), db.session.get(Product, 2)
            self.assertEqual((p1.preco_promocional, p1.promo_inicio, p1.promo_fim), (None, None, None))
            self.assertIsNone(p2.preco_promocional)
        with app.app_context():
            nome3 = db.session.get(Product, 3).name
        html = self.enviar(a, xlsx([["nome", "preco_promocional", "promo_inicio", "promo_fim"],
                                    [nome3, str(preco3 + 1), None, None],
                                    [n1, "50", "31/12/2026", "01/12/2026"],
                                    [n2, None, "amanhã", None]])).get_data(as_text=True)
        self.assertIn("menor que o preço normal", html)
        self.assertIn("termina antes de começar", html)
        self.assertIn("data inválida", html)
        self.assertNotIn("Confirmar importação", html)

    def test_medidas_da_embalagem(self):
        a = self.admin()
        conteudo = xlsx([["Código", "Nome", "Altura", "Largura (cm)", "Profundidade"],
                         [None, "Fresa de Topo Metal Duro 10 mm 4 Cortes", "12,5", 20, "30"]])
        html = self.enviar(a, conteudo).get_data(as_text=True)
        self.assertIn("altura", html)
        self.confirmar(a)
        with app.app_context():
            p = db.session.get(Product, 1)
            self.assertEqual((p.altura_cm, p.largura_cm, p.comprimento_cm),
                             (Decimal("12.5"), Decimal("20.0"), Decimal("30.0")))
            nome = p.name
        # a exportação traz as medidas, e "remover" volta para a caixa padrão
        wb = load_workbook(io.BytesIO(a.get("/admin/planilhas/produtos.xlsx").data))
        cab = [c.value for c in wb.active[1]]
        linha = next(r for r in wb.active.iter_rows(min_row=2, values_only=True) if r[1] == nome)
        self.assertEqual([linha[cab.index(c)] for c in ("altura_cm", "largura_cm", "comprimento_cm")], [12.5, 20, 30])
        self.enviar(a, xlsx([["nome", "altura_cm"], [nome, "remover"]]))
        self.confirmar(a)
        with app.app_context():
            self.assertIsNone(db.session.get(Product, 1).altura_cm)
            self.assertEqual(db.session.get(Product, 1).largura_cm, Decimal("20.0"))

    def test_medida_invalida_bloqueia(self):
        a = self.admin()
        html = self.enviar(a, xlsx([["nome", "altura_cm", "largura_cm"],
                                    ["Fresa de Topo Metal Duro 10 mm 4 Cortes", "2000", "abc"]])).get_data(as_text=True)
        self.assertIn("“altura_cm” vai de 0,1 a 1.000 cm", html)
        self.assertIn("valor inválido em “largura_cm”", html)

    def test_exportar_e_reimportar_sem_mudar_nada(self):
        a = self.admin()
        r = a.get("/admin/planilhas/produtos.xlsx")
        self.assertIn("attachment", r.headers["Content-Disposition"])
        ws = load_workbook(io.BytesIO(r.data)).active
        self.assertEqual(ws.cell(1, 2).value, "nome")
        self.assertEqual(ws.max_row, 19)
        with app.app_context():
            antes = {p.id: (p.price, p.stock, p.multiplo, p.fracionavel, len(p.faixas)) for p in Product.query.all()}
        html = self.enviar(a, r.data).get_data(as_text=True)
        self.assertIn("Confirmar importação", html)
        self.confirmar(a)
        with app.app_context():
            depois = {p.id: (p.price, p.stock, p.multiplo, p.fracionavel, len(p.faixas)) for p in Product.query.all()}
            self.assertEqual(antes, depois)
            self.assertEqual(Category.query.count(), 6)

    def test_exportar_pedidos(self):
        c = self.cadastrar()
        self.adicionar(c, 1, 2)
        self.fechar(c, "pix")
        ws = load_workbook(io.BytesIO(self.admin().get("/admin/planilhas/pedidos.xlsx").data)).active
        ultima = [cel.value for cel in ws[2]]
        self.assertEqual(ultima[2], "pendente")
        self.assertIn("2x Fresa de Topo", ultima[-1])

    def test_arquivo_invalido_e_acesso(self):
        a = self.admin()
        r = self.enviar(a, b"qualquer coisa", "foto.png")
        self.assertIn("Envie um arquivo .xlsx", a.get(r.headers["Location"]).get_data(as_text=True))
        self.enviar(a, xlsx([["preco"], [10]]))
        self.assertIn("coluna “nome”", a.get("/admin/planilhas").get_data(as_text=True))
        self.assertEqual(self.cadastrar().get("/admin/planilhas/produtos.xlsx").status_code, 302)

    def test_codigo_unico_no_formulario(self):
        a = self.admin()
        self.ajustar_produto(1, codigo="ABC")
        with app.app_context():
            p = db.session.get(Product, 2)
            form = {"name": p.name, "category_id": p.category_id, "price": "1,00", "stock": 1, "codigo": "ABC"}
        r = self.post(a, "/admin/products/2/edit", form, "/admin/products/2/edit")
        self.assertIn("Já existe outro produto com o código ABC", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
