"""Avaliações de produto: só quem comprou, moderação no painel, média na página e dados para o Google."""
import json
import re
import unittest

from test_b2b import BaseB2B

from app import app  # noqa: E402
import produtos  # noqa: E402
from models import Avaliacao, Product, User, db  # noqa: E402


class TesteAvaliacoes(BaseB2B):
    def setUp(self):
        super().setUp()
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)
        self.a = self.admin()
        with app.test_request_context():
            self.pagina = produtos.link(db.session.get(Product, 1))

    def comprador(self, email="cli@teste.com", pagar=True):
        c = self.cadastrar(email)
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        if pagar:
            self.post(self.a, f"/admin/orders/{self.ultimo_pedido()[0]}/status", {"status": "pago"}, "/admin/orders")
        return c

    def avaliar(self, c, nota="5", comentario="Ótima fresa, durou bastante."):
        return self.post(c, "/produto/1/avaliar", {"nota": nota, "comentario": comentario}, self.pagina)

    def moderar(self, acao, resposta=""):
        with app.app_context():
            aid = Avaliacao.query.order_by(Avaliacao.id.desc()).first().id
        return self.post(self.a, "/admin/avaliacoes", {"aid": aid, "acao": acao, "resposta": resposta},
                         "/admin/avaliacoes")

    def contagem(self):
        with app.app_context():
            return Avaliacao.query.count()

    def test_so_quem_comprou_e_pagou_avalia(self):
        c = self.cadastrar("curioso@teste.com")
        self.assertNotIn("Avaliar este produto", c.get(self.pagina).get_data(as_text=True))
        self.avaliar(c)
        self.assertEqual(self.contagem(), 0)
        c2 = self.comprador("naopagou@teste.com", pagar=False)
        self.avaliar(c2)
        self.assertEqual(self.contagem(), 0, "pedido aguardando pagamento ainda não vale")

    def test_moderacao_publica_media_e_google(self):
        c = self.comprador()
        self.assertIn("Avaliar este produto", c.get(self.pagina).get_data(as_text=True))
        self.emails.clear()
        self.avaliar(c, "4")
        self.assertTrue(any("nova avaliação" in e["assunto"] for e in self.emails))
        self.assertNotIn("Ótima fresa", self.cliente().get(self.pagina).get_data(as_text=True), "antes de publicar")
        self.moderar("aprovar", "Obrigado pela confiança!")
        html = self.cliente().get(self.pagina).get_data(as_text=True)
        self.assertIn("Ótima fresa", html)
        self.assertIn("Obrigado pela confiança!", html)
        self.assertIn("Cliente T.", html, "primeiro nome e inicial")
        self.assertNotIn("cli@teste.com", html)
        dados = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html).group(1))
        self.assertEqual(dados["aggregateRating"]["ratingValue"], 4.0)
        self.assertEqual(dados["aggregateRating"]["reviewCount"], 1)

    def test_editar_volta_para_moderacao_e_uma_por_pessoa(self):
        c = self.comprador()
        self.avaliar(c, "5")
        self.moderar("aprovar")
        self.avaliar(c, "2", "Mudei de ideia")
        self.assertEqual(self.contagem(), 1)
        with app.app_context():
            a = Avaliacao.query.one()
            self.assertEqual((a.nota, a.status), (2, "pendente"))
        self.assertNotIn("Mudei de ideia", self.cliente().get(self.pagina).get_data(as_text=True))

    def test_nota_invalida_e_comentario_com_html(self):
        c = self.comprador()
        self.avaliar(c, "9")
        self.assertEqual(self.contagem(), 0)
        self.avaliar(c, "3", "<script>alert(1)</script>")
        self.moderar("aprovar")
        html = self.cliente().get(self.pagina).get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_recusar_e_excluir(self):
        c = self.comprador()
        self.avaliar(c)
        self.moderar("recusar")
        self.assertNotIn("Ótima fresa", self.cliente().get(self.pagina).get_data(as_text=True))
        self.assertIn("Ótima fresa", self.a.get("/admin/avaliacoes?status=recusada").get_data(as_text=True))
        self.moderar("excluir")
        self.assertEqual(self.contagem(), 0)

    def test_conta_excluida_leva_as_avaliacoes(self):
        c = self.comprador()
        self.avaliar(c)
        self.assertEqual(c.get("/conta/meus-dados.json").get_json()["avaliacoes"][0]["nota"], 5)
        with app.app_context():
            db.session.delete(User.query.filter_by(email="cli@teste.com").first())
            db.session.commit()
        self.assertEqual(self.contagem(), 0)


    def test_editar_varias_vezes_avisa_a_loja_uma_vez(self):
        c = self.comprador()
        self.emails.clear()
        for i in range(5):
            self.avaliar(c, "5", f"edição {i}")
        self.assertEqual(len([e for e in self.emails if "nova avaliação" in e["assunto"]]), 1)
        self.moderar("aprovar")
        self.avaliar(c, "3", "mudei depois de publicada")
        self.assertEqual(len([e for e in self.emails if "nova avaliação" in e["assunto"]]), 2,
                         "editar depois da moderação avisa de novo")


if __name__ == "__main__":
    unittest.main()
