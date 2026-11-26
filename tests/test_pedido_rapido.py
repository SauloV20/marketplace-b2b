"""Testes do pedido rápido por código."""
import unittest

from test_b2b import BaseB2B

import pedido_rapido  # noqa: E402


class TestePedidoRapido(BaseB2B):
    def setUp(self):
        super().setUp()
        for pid in (1, 11, 14, 15):
            self.ajustar_produto(pid, stock=500)

    def test_separar_linhas(self):
        casos = {"DISCO-7 50": ("DISCO-7", 50), "DISCO-7\t50": ("DISCO-7", 50), "DISCO-7;50": ("DISCO-7", 50),
                 "Disco de Desbaste 7 pol 20": ("Disco de Desbaste 7 pol", 20), "FRESA-10": ("FRESA-10", 1),
                 "LUVA-RASPA 1.200": ("LUVA-RASPA", 1200), "DISCO-7 50,0": ("DISCO-7", 50)}
        for linha, esperado in casos.items():
            self.assertEqual(pedido_rapido.separar(linha), esperado, linha)

    def test_visitante_precisa_entrar(self):
        self.assertIn("/entrar", self.cliente().get("/pedido-rapido").headers["Location"])

    def test_conferir_mostra_ajustes_e_erros(self):
        c = self.cadastrar()
        lista = "DISCO-7 50\nOCULOS-INC 5\nnao-existe 3\nDisco de Desbaste 7 pol 10\nFRESA-10\t2\nDISCO-FLAP80 7"
        html = self.post(c, "/pedido-rapido", {"lista": lista, "acao": "conferir"}, "/pedido-rapido").get_data(as_text=True)
        self.assertIn("Código não encontrado", html)
        self.assertIn("Ajustado para 12 un (vendido em múltiplos de 12)", html)
        self.assertIn("7 un fora da embalagem (fracionadas)", html)
        self.assertIn("R$ 14,90", html, "60 discos (50 + 10 somados) caem na faixa de 50+")
        with c.session_transaction() as s:
            self.assertFalse(s.get("cart"), "conferir não mexe no carrinho")

    def test_adicionar_tudo(self):
        c = self.cadastrar()
        r = self.post(c, "/pedido-rapido", {"lista": "DISCO-7 50\nOCULOS-INC 5\nFRESA-10 2", "acao": "adicionar"},
                      "/pedido-rapido")
        self.assertTrue(r.headers["Location"].endswith("/carrinho"))
        with c.session_transaction() as s:
            self.assertEqual(s["cart"], {"14": 50, "11": 12, "1": 2})
        self.assertIn("3 produto(s) adicionado(s)", c.get("/carrinho").get_data(as_text=True))

    def test_linha_com_erro_fica_na_tela(self):
        c = self.cadastrar()
        r = self.post(c, "/pedido-rapido", {"lista": "DISCO-7 10\nXYZ 1", "acao": "adicionar"}, "/pedido-rapido")
        html = r.get_data(as_text=True)
        self.assertIn("1 linha(s) ficaram de fora", html)
        with c.session_transaction() as s:
            self.assertEqual(s["cart"], {"14": 10})

    def test_produto_indisponivel_e_limites(self):
        self.ajustar_produto(14, stock=0)
        c = self.cadastrar()
        html = self.post(c, "/pedido-rapido", {"lista": "DISCO-7 10", "acao": "conferir"}, "/pedido-rapido").get_data(as_text=True)
        self.assertIn("Indisponível no momento", html)
        html = self.post(c, "/pedido-rapido", {"lista": "\n".join(["DISCO-7 1"] * 201)}, "/pedido-rapido").get_data(as_text=True)
        self.assertIn("no máximo 200 linhas", html)

    def test_link_no_cabecalho_so_logado(self):
        self.assertNotIn("Pedido rápido", self.cliente().get("/").get_data(as_text=True))
        self.assertIn("Pedido rápido", self.cadastrar().get("/").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
