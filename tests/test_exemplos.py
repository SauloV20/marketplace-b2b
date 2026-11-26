"""Limpeza dos dados de exemplo pelo painel (exemplos.py)."""
import unittest

from test_b2b import BaseB2B
from test_site import ADMIN_SENHA

from app import app  # noqa: E402
from b2b import salvar_config  # noqa: E402
import exemplos  # noqa: E402
from models import Order, Product, agora_utc, db  # noqa: E402


class TesteDadosDeExemplo(BaseB2B):
    def contar(self):
        with app.app_context():
            return exemplos.pedidos().count(), len(exemplos.produtos())

    def backup_feito(self):
        with app.app_context():
            salvar_config("ultimo_backup_em", agora_utc().isoformat(timespec="seconds"))
            db.session.commit()

    def limpar(self, a, **mudar):
        dados = {"senha": ADMIN_SENHA, "confirmacao": "LIMPAR", "produtos": "on", **mudar}
        return self.post(a, "/admin/backup/exemplos", dados, "/admin/backup")

    def test_painel_mostra_o_que_vai_sair(self):
        pedidos, produtos = self.contar()
        self.assertEqual(pedidos, 80)
        self.assertGreater(produtos, 0)
        html = self.admin().get("/admin/backup").get_data(as_text=True)
        self.assertIn("80 pedido(s) de \"Cliente demo\"", html)
        self.assertIn("Primeiro baixe um backup", html, "sem backup recente: o botão não aparece")
        self.assertNotIn("Limpar dados de exemplo</button>", html)

    def test_sem_backup_nao_limpa(self):
        a = self.admin()
        self.limpar(a)
        self.assertEqual(self.contar()[0], 80)

    def test_senha_ou_confirmacao_errada_nao_limpa(self):
        self.backup_feito()
        a = self.admin()
        self.limpar(a, senha="errada-123456")
        self.limpar(a, confirmacao="sim")
        self.assertEqual(self.contar()[0], 80)

    def test_limpa_exemplos_e_mantem_o_que_e_de_verdade(self):
        c = self.empresa(aprovar=False)
        self.adicionar(c, 1, 2)
        self.fechar(c, "pix")
        real, _, _, _, _, _ = self.ultimo_pedido()
        with app.app_context():
            editado = Product.query.filter_by(description=exemplos.DESCRICAO_DEMO).order_by(Product.id.desc()).first()
            editado.description = "Descrição de verdade."
            editado_id = editado.id
            db.session.commit()
            estoque_antes = {p.id: p.stock for p in Product.query.all()}
            amostras = len(exemplos.produtos())
        self.backup_feito()
        a = self.admin()
        self.limpar(a)
        with app.app_context():
            self.assertEqual(Order.query.filter_by(customer_name=exemplos.NOME_DEMO).count(), 0)
            self.assertIsNotNone(db.session.get(Order, real), "pedido de cliente de verdade fica")
            self.assertTrue(db.session.get(Product, editado_id).active, "produto editado continua na loja")
            self.assertTrue(db.session.get(Product, 1).active, "produto com venda de verdade continua")
            self.assertEqual(Product.query.filter_by(active=False).count(), amostras)
            self.assertEqual({p.id: p.stock for p in Product.query.all()}, estoque_antes, "estoque não muda")
        self.assertIn("80 pedido(s) de exemplo apagado(s)", a.get("/admin/backup").get_data(as_text=True))

    def test_pode_manter_os_produtos(self):
        self.backup_feito()
        self.limpar(self.admin(), produtos="")
        with app.app_context():
            self.assertEqual(Product.query.filter_by(active=False).count(), 0)
        self.assertEqual(self.contar()[0], 0)

    def test_depois_de_limpo_a_secao_some(self):
        self.backup_feito()
        a = self.admin()
        self.limpar(a)
        self.assertNotIn('id="exemplos"', a.get("/admin/backup").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
