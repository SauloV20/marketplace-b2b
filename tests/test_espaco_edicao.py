"""Testes da compressão de imagens, do espaço no banco, da edição de pedido em aberto e dos extras da conta."""
import io
import os
import unittest
from decimal import Decimal
from unittest import mock

from PIL import Image

from test_b2b import BaseB2B

from app import app  # noqa: E402
import armazenamento  # noqa: E402
import imagens  # noqa: E402
from models import Banner, Order, Product, ProdutoFoto, db  # noqa: E402


def foto(largura=2400, altura=1800, formato="JPEG", modo="RGB", qualidade=97):
    """Imagem com ruído (difícil de comprimir, como uma foto de verdade)."""
    img = Image.frombytes(modo, (largura, altura), os.urandom(largura * altura * len(modo)))
    saida = io.BytesIO()
    img.save(saida, formato, **({"quality": qualidade} if formato == "JPEG" else {}))
    return saida.getvalue()


class TesteImagens(BaseB2B):
    def test_comprimir(self):
        grande = foto()
        novo, tipo = imagens.comprimir(grande, "image/jpeg")
        self.assertEqual(tipo, "image/jpeg")
        self.assertLess(len(novo), len(grande) // 2)
        self.assertEqual(max(Image.open(io.BytesIO(novo)).size), imagens.LADO_FOTO)

        transparente = foto(800, 600, "PNG", "RGBA")
        novo, tipo = imagens.comprimir(transparente, "image/png")
        self.assertEqual(tipo, "image/webp")
        self.assertEqual(Image.open(io.BytesIO(novo)).mode, "RGBA")

        gif = foto(100, 100, "GIF", "RGB")
        self.assertEqual(imagens.comprimir(gif, "image/gif"), (gif, "image/gif"))
        self.assertEqual(imagens.comprimir(b"\x89PNG\r\n\x1a\nquebrado", "image/png"),
                         (b"\x89PNG\r\n\x1a\nquebrado", "image/png"))
        pequena = foto(50, 50, qualidade=30)
        self.assertEqual(imagens.comprimir(pequena, "image/jpeg")[0], pequena, "não troca por uma maior")

    def test_foto_do_produto_e_banner_comprimidos_ao_enviar(self):
        a = self.admin()
        grande = foto()
        a.post("/admin/products/2/fotos", data={"_csrf": self.token(a, "/admin/products/2/edit"),
                                                 "fotos": (io.BytesIO(grande), "celular.jpg")},
               content_type="multipart/form-data")
        with app.app_context():
            f = ProdutoFoto.query.filter_by(product_id=2).one()
            self.assertEqual(f.tipo, "image/jpeg")
            self.assertLess(len(f.dados), len(grande) // 2)
        a.post("/admin/banners/novo", data={"_csrf": self.token(a, "/admin/banners/novo"), "titulo": "Promo",
                                             "ativo": "on", "cor": "azul",
                                             "imagem": (io.BytesIO(foto(3000, 1000)), "banner.jpg")},
               content_type="multipart/form-data")
        with app.app_context():
            b = Banner.query.filter_by(titulo="Promo").one()
            self.assertEqual(max(Image.open(io.BytesIO(b.imagem)).size), imagens.LADO_BANNER)


class TesteEspaco(BaseB2B):
    def test_pagina_mostra_uso_e_comprime_as_antigas(self):
        grande = foto()
        with app.app_context():
            db.session.add(ProdutoFoto(product_id=3, dados=grande, tipo="image/jpeg", ordem=1))
            db.session.commit()
        a = self.admin()
        html = a.get("/admin/backup").get_data(as_text=True)
        self.assertIn("Espaço no banco de dados", html)
        self.assertIn("Fotos de produtos", html)
        self.assertIn("1 foto(s) ou banner(s)", html)
        r = self.post(a, "/admin/backup/comprimir", {"df": "0", "db": "0"}, "/admin/backup")
        self.assertEqual(r.status_code, 302)
        with app.app_context():
            f = ProdutoFoto.query.filter_by(product_id=3).one()
            self.assertLess(len(f.dados), armazenamento.GRANDE_FOTO)
            self.assertEqual(f.versao, 2)
            self.assertEqual(armazenamento.a_comprimir(), 0)
        self.assertIn("1 imagem(ns) comprimida(s)", a.get("/admin/backup").get_data(as_text=True))

    def test_aviso_no_painel_quando_quase_cheio(self):
        a = self.admin()
        with mock.patch("armazenamento.tamanho_do_banco", return_value=450_000_000):
            self.assertIn("com 88% do espaço", a.get("/admin/").get_data(as_text=True))
        with mock.patch("armazenamento.tamanho_do_banco", return_value=100_000_000):
            self.assertNotIn("do espaço do plano", a.get("/admin/").get_data(as_text=True))


class TesteEditarPedido(BaseB2B):
    def novo(self, c=None):
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50, codigo="FRESA-10")
        self.ajustar_produto(2, qtd_minima=1, multiplo=1, stock=50, codigo="SERRA-X")
        c = c or self.cadastrar()
        self.adicionar(c, 1, 5)
        self.fechar(c, "boleto")
        with app.app_context():
            o = Order.query.order_by(Order.id.desc()).first()
            return o.id, o.items[0].id

    def editar(self, a, oid, dados):
        return self.post(a, f"/admin/orders/{oid}/editar", dados, f"/admin/orders/{oid}")

    def test_quantidade_preco_item_novo_e_frete(self):
        oid, item = self.novo()
        a = self.admin()
        self.assertIn("Editar itens, preços ou frete", a.get(f"/admin/orders/{oid}").get_data(as_text=True))
        lista = a.get(f"/admin/orders?q=%23{oid}").get_data(as_text=True)
        self.assertIn(f'href="/admin/orders/{oid}#editar">Editar</a>', lista)
        del self.emails[:]
        self.editar(a, oid, {f"qtd_{item}": "3", f"preco_{item}": "100,00", "novo_produto": "serra-x",
                             "novo_qtd": "2", "frete": "30,00", "avisar": "on"})
        self.assertEqual((self.estoque(1), self.estoque(2)), (47, 48))
        with app.app_context():
            o = db.session.get(Order, oid)
            itens = sorted((i.product_id, i.quantity, i.unit_price) for i in o.items)
            preco2 = db.session.get(Product, 2).price
            self.assertEqual(itens, [(1, 3, Decimal("100.00")), (2, 2, preco2)])
            self.assertEqual(o.total, Decimal("300.00") + preco2 * 2 + Decimal("30.00"))
        self.assertTrue(any("atualizado" in e["assunto"] for e in self.emails))

        self.editar(a, oid, {f"qtd_{item}": "0"})
        self.assertEqual(self.estoque(1), 50, "tirar o item devolve o estoque")

    def test_sem_estoque_nada_muda(self):
        oid, item = self.novo()
        self.editar(self.admin(), oid, {f"qtd_{item}": "999"})
        self.assertEqual(self.estoque(1), 45)
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).items[0].quantity, 5)

    def test_bloqueios(self):
        oid, item = self.novo()
        a = self.admin()
        r = self.editar(a, oid, {f"qtd_{item}": "0"})
        self.assertIn("ficaria sem itens", a.get(f"/admin/orders/{oid}").get_data(as_text=True))
        self.post(a, f"/admin/orders/{oid}/status", {"status": "pago"}, "/admin/orders")
        self.editar(a, oid, {f"qtd_{item}": "1"})
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).items[0].quantity, 5)
        self.assertIn("já foi pago", a.get(f"/admin/orders/{oid}").get_data(as_text=True))
        oid2, item2 = self.novo(self.cadastrar("dois@teste.com"))
        with app.app_context():
            db.session.get(Order, oid2).pix_txid = "abc123"
            db.session.commit()
        self.editar(a, oid2, {f"qtd_{item2}": "1"})
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid2).items[0].quantity, 5)
        self.assertEqual(r.status_code, 302)


class TesteContaExtras(BaseB2B):
    def test_planilha_dos_meus_pedidos(self):
        from openpyxl import load_workbook
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        outro = self.cadastrar("outro@teste.com")
        self.adicionar(outro, 2, 1)
        self.fechar(outro, "pix")
        self.assertIn("Baixar pedidos em Excel", c.get("/conta/pedidos").get_data(as_text=True))
        r = c.get("/conta/pedidos.xlsx")
        self.assertIn("attachment", r.headers["Content-Disposition"])
        wb = load_workbook(io.BytesIO(r.data))
        self.assertEqual(wb.sheetnames, ["Pedidos", "Itens"])
        self.assertEqual(wb["Pedidos"].max_row, 2, "só o pedido deste cliente")
        self.assertEqual(wb["Itens"]["E2"].value, "Fresa de Topo Metal Duro 10 mm 4 Cortes")
        self.assertEqual(self.cliente().get("/conta/pedidos.xlsx").status_code, 302)

    def test_condicoes_da_empresa(self):
        c = self.empresa(desconto="7,5", prazos="28; 30/60/90")
        html = c.get("/conta").get_data(as_text=True)
        self.assertIn("Condições da Metalúrgica Teste Ltda", html)
        self.assertIn("7,5%", html)
        self.assertIn("28 ou 30/60/90 dias", html)
        pf = self.cadastrar("pf@teste.com").get("/conta").get_data(as_text=True)
        self.assertIn("Suas condições de compra", pf)
        self.assertIn("Cadastre com o CNPJ", pf)


if __name__ == "__main__":
    unittest.main()
