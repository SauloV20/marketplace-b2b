"""Testes dos avisos por e-mail (os e-mails ficam numa lista, nada é enviado de verdade)."""
import unittest
from unittest import mock

from test_b2b import BaseB2B

from app import app  # noqa: E402
from models import Order, User, db  # noqa: E402


class TesteAvisos(BaseB2B):
    def para(self, email):
        return [e for e in self.emails if e["para"] == email]

    def admin_recebeu(self, trecho):
        return any(trecho in e["assunto"] for e in self.para("admin@exemplo.com"))

    def test_pedido_avisa_cliente_e_loja(self):
        c = self.cadastrar()
        self.adicionar(c, 1, 2)
        del self.emails[:]
        self.fechar(c, "pix")
        oid = self.ultimo_pedido()[0]
        cliente = self.para("compras@ex.com")
        self.assertEqual(len(cliente), 1)
        self.assertIn(f"pedido #{oid} recebido", cliente[0]["assunto"])
        self.assertIn("2x Fresa de Topo", cliente[0]["texto"])
        self.assertIn(f"/conta/pedidos/{oid}", cliente[0]["texto"])
        self.assertTrue(self.admin_recebeu(f"Novo pedido #{oid}"))

    def test_orcamento_avisa_e_resposta_chega_ao_cliente(self):
        c = self.empresa()
        self.adicionar(c, 1, 1)
        del self.emails[:]
        self.fechar(c, "orcamento")
        oid = self.ultimo_pedido()[0]
        self.assertTrue(self.admin_recebeu(f"Novo orçamento #{oid}"))
        self.assertIn(f"/admin/orders/{oid}/orcamento", self.para("admin@exemplo.com")[-1]["texto"])
        with app.app_context():
            item = db.session.get(Order, oid).items[0].id
        self.post(self.admin(), f"/admin/orders/{oid}/orcamento",
                  {"acao": "aprovar", f"preco_{item}": "150,00", "pagamento": "pix"}, f"/admin/orders/{oid}/orcamento")
        resposta = self.para("compras@ex.com")[-1]
        self.assertIn("aprovado", resposta["assunto"])
        self.assertIn("R$ 150,00", resposta["texto"])

    def test_orcamento_recusado(self):
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "orcamento")
        oid = self.ultimo_pedido()[0]
        self.post(self.admin(), f"/admin/orders/{oid}/orcamento", {"acao": "recusar"}, f"/admin/orders/{oid}/orcamento")
        self.assertIn("não aprovado", self.para("compras@ex.com")[-1]["assunto"])

    def test_empresa_nova_e_aprovacao_uma_vez_so(self):
        self.empresa(aprovar=False)
        self.assertTrue(self.admin_recebeu("Nova empresa para aprovar: Metalúrgica Teste Ltda"))
        with app.app_context():
            uid = User.query.filter_by(email="empresa@teste.com").one().id
        a = self.admin()
        dados = {"aprovada": "on", "desconto": "10", "prazos": "30/60", "razao_social": "Metalúrgica Teste Ltda"}
        self.post(a, f"/admin/clientes/{uid}", dados, f"/admin/clientes/{uid}")
        self.post(a, f"/admin/clientes/{uid}", dados, f"/admin/clientes/{uid}")
        aprovacoes = [e for e in self.para("empresa@teste.com") if "aprovado" in e["assunto"]]
        self.assertEqual(len(aprovacoes), 1, "salvar de novo não repete o aviso")
        self.assertIn("10% de desconto", aprovacoes[0]["texto"])
        self.assertIn("30/60 dias", aprovacoes[0]["texto"])

    def test_pagamento_e_cancelamentos(self):
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        oid = self.ultimo_pedido()[0]
        a = self.admin()
        self.post(a, f"/admin/orders/{oid}/status", {"status": "pago"}, "/admin/orders")
        self.assertIn("pagamento do pedido", self.para("compras@ex.com")[-1]["assunto"])
        self.post(a, f"/admin/orders/{oid}/status", {"status": "cancelado"}, "/admin/orders")
        self.assertIn("cancelado", self.para("compras@ex.com")[-1]["assunto"])
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        oid2 = self.ultimo_pedido()[0]
        self.post(c, f"/conta/pedidos/{oid2}/cancelar", {})
        self.assertTrue(self.admin_recebeu(f"Cliente cancelou o pedido #{oid2}"))

    def test_pix_pago_por_outra_pessoa_alerta_a_loja(self):
        app.config.update(EFI_CLIENT_ID="id", EFI_CLIENT_SECRET="s", EFI_CERTIFICADO="/x.pem", EFI_CHAVE_PIX="k")
        self.addCleanup(app.config.update, EFI_CLIENT_ID="", EFI_CLIENT_SECRET="", EFI_CERTIFICADO="", EFI_CHAVE_PIX="")
        with mock.patch("efi.criar_cobranca", return_value=("Marketplace B2B" + "1" * 25, "000201X")), \
                mock.patch("efi.consultar_cobranca", return_value=(True, {"nome": "FULANO", "documento": "***.000.000-**"})):
            c = self.cadastrar()
            self.adicionar(c, 1, 1)
            self.fechar(c, "pix")
            oid = self.ultimo_pedido()[0]
            c.get(f"/pedido/{oid}")
            self.post(self.admin(), f"/admin/orders/{oid}/pix", {}, "/admin/orders")
        self.assertTrue(self.admin_recebeu(f"Atenção: pedido #{oid} pago por outra pessoa"))

    def test_falha_no_envio_nao_atrapalha_a_compra(self):
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        with mock.patch("avisos.enviar_email", return_value=False):
            r = self.fechar(c, "pix")
        self.assertIn("/pedido/", r.headers["Location"])


if __name__ == "__main__":
    unittest.main()
