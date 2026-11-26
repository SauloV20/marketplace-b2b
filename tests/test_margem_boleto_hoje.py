"""Proteção de margem, boleto registrado pela Efí (com baixa automática) e o painel do dia."""
import re
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from test_b2b import BaseB2B

from app import app  # noqa: E402
import boleto_efi  # noqa: E402
from b2b import hoje  # noqa: E402
from models import Cupom, Lancamento, Order, Product, User, db  # noqa: E402


class BaseMargem(BaseB2B):
    def setUp(self):
        super().setUp()
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50, custo_medio=Decimal("150"))  # vende a 189,90
        self.a = self.admin()

    def margem(self, modo="avisar", minima="20"):
        self.post(self.a, "/admin/formacao-de-preco", {"acao": "margem", "margem_modo": modo, "margem_minima": minima},
                  "/admin/formacao-de-preco")


class TesteMargem(BaseMargem):
    def test_avisa_no_produto_e_no_pedido(self):
        self.margem("avisar", "30")  # 189,90 − 150 = 21% de margem
        with app.app_context():
            p = db.session.get(Product, 1)
            form = {"name": p.name, "price": "189,90", "stock": "50", "category_id": str(p.category_id),
                    "qtd_minima": "1", "multiplo": "1", "active": "on"}
        r = self.post(self.a, "/admin/products/1/edit", form, "/admin/products/1/edit")
        self.assertIn("abaixo do mínimo de 30", self.a.get(r.headers["Location"]).get_data(as_text=True))
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        oid = self.ultimo_pedido()[0]
        self.assertIn("Margem:", self.a.get(f"/admin/orders/{oid}").get_data(as_text=True))
        self.assertIn("margem abaixo do mínimo", self.a.get("/admin/hoje").get_data(as_text=True).lower())

    def test_bloqueia_preco_editado_e_cupom(self):
        self.margem("bloquear", "0")  # 0 = só abaixo do custo
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 1)
        with app.app_context():
            db.session.add(Cupom(codigo="METADE", tipo="percentual", valor=Decimal(50), um_por_cliente=False))
            db.session.add(Cupom(codigo="DEZ", tipo="percentual", valor=Decimal(10), um_por_cliente=False))
            db.session.commit()
        r = self.post(c, "/carrinho/cupom", {"codigo": "METADE"}, "/carrinho")
        self.assertIn("não vale para estes produtos", c.get(r.headers["Location"]).get_data(as_text=True))
        self.post(c, "/carrinho/cupom", {"codigo": "DEZ"}, "/carrinho")
        self.assertIn("Cupom DEZ aplicado", c.get("/carrinho").get_data(as_text=True))
        self.fechar(c, "pix")
        oid = self.ultimo_pedido()[0]
        with app.app_context():
            item = db.session.get(Order, oid).items[0].id
        r = self.post(self.a, f"/admin/orders/{oid}/editar", {f"qtd_{item}": "1", f"preco_{item}": "100,00"},
                      f"/admin/orders/{oid}")
        self.assertIn("Margem abaixo do mínimo", self.a.get(r.headers["Location"]).get_data(as_text=True))
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).items[0].unit_price, Decimal("189.90"))

    def test_desligada_nao_avalia(self):
        self.margem("desligado", "50")
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        self.assertNotIn("Margem:", self.a.get(f"/admin/orders/{self.ultimo_pedido()[0]}").get_data(as_text=True))


class EfiFalsa:
    def __init__(self):
        self.cobrancas, self.canceladas, self.pagas = {}, [], set()

    def __call__(self, metodo, caminho, corpo=None):
        if caminho == "/v1/charge/one-step":
            cid = 9000 + len(self.cobrancas)
            self.cobrancas[cid] = corpo
            return {"code": 200, "data": {"charge_id": cid, "barcode": f"34191.0000 {cid}", "status": "waiting",
                                          "billet_link": f"https://boleto.efi/{cid}", "expire_at":
                                              corpo["payment"]["banking_billet"]["expire_at"]}}
        if caminho.startswith("/v1/notification/"):
            return {"data": [{"status": {"current": "paid", "previous": "waiting"}, "identifiers": {"charge_id": cid},
                              "value": self.cobrancas[cid]["items"][0]["value"], "received_by_bank_at": "2026-11-13"}
                             for cid in sorted(self.pagas)]}
        m = re.match(r"/v1/charge/(\d+)(/cancel)?$", caminho)
        if m and m.group(2):
            self.canceladas.append(int(m.group(1)))
            return {"code": 200}
        if m:
            cid = int(m.group(1))
            return {"data": {"status": "paid" if cid in self.pagas else "waiting",
                             "total": self.cobrancas[cid]["items"][0]["value"]}}
        raise AssertionError(caminho)


class TesteBoleto(BaseMargem):
    def setUp(self):
        super().setUp()
        app.config.update(EFI_CLIENT_ID="id", EFI_CLIENT_SECRET="segredo")
        self.efi = EfiFalsa()
        trava = mock.patch.object(boleto_efi, "_chamar", side_effect=self.efi)
        trava.start()
        self.addCleanup(trava.stop)

    def tearDown(self):
        app.config.update(EFI_CLIENT_ID="", EFI_CLIENT_SECRET="")
        super().tearDown()

    def faturado(self):
        c = self.empresa()
        self.adicionar(c, 1, 2)
        self.fechar(c, "faturado", prazo="30/60/90")
        return self.ultimo_pedido()[0], c

    def test_gera_boletos_e_baixa_pela_notificacao(self):
        oid, c = self.faturado()
        self.emails.clear()
        self.post(self.a, f"/admin/orders/{oid}/boletos", {}, f"/admin/orders/{oid}")
        with app.app_context():
            ls = Lancamento.query.filter_by(order_id=oid).order_by(Lancamento.vencimento).all()
            self.assertTrue(all(l.boleto_id for l in ls))
            self.assertEqual(len(self.efi.cobrancas), 3)
            primeiro = self.efi.cobrancas[int(ls[0].boleto_id)]
            self.assertEqual(primeiro["items"][0]["value"], int(ls[0].valor * 100))
            self.assertIn("juridical_person", primeiro["payment"]["banking_billet"]["customer"])
            self.assertEqual(primeiro["payment"]["banking_billet"]["configurations"], {"fine": 200, "interest": 3})
        self.assertTrue(any("https://boleto.efi/" in e["texto"] for e in self.emails))
        self.assertIn("Abrir boleto", c.get(f"/conta/pedidos/{oid}").get_data(as_text=True))
        self.efi.pagas = set(self.efi.cobrancas)
        r = self.cliente().post("/efi/boleto", data={"notification": "token-123"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.ultimo_pedido()[1], "pago", "a última parcela paga quita o pedido")
        with app.app_context():
            self.assertTrue(all(l.pago_em and l.forma == "boleto" for l in Lancamento.query.filter_by(order_id=oid)))

    def test_consultar_e_cancelar(self):
        oid, _ = self.faturado()
        self.post(self.a, f"/admin/orders/{oid}/boletos", {}, f"/admin/orders/{oid}")
        with app.app_context():
            primeira = Lancamento.query.filter_by(order_id=oid).order_by(Lancamento.vencimento).first()
            lid, cid = primeira.id, int(primeira.boleto_id)
        self.efi.pagas = {cid}
        self.post(self.a, "/admin/financeiro", {"acao": "boletos_atualizar"}, "/admin/financeiro")
        with app.app_context():
            self.assertIsNotNone(db.session.get(Lancamento, lid).pago_em)
        self.assertEqual(self.ultimo_pedido()[1], "pendente", "ainda faltam duas parcelas")
        self.post(self.a, f"/admin/orders/{oid}/status", {"status": "cancelado"}, "/admin/orders")
        self.assertEqual(len(self.efi.canceladas), 2, "os boletos em aberto são cancelados na Efí")

    def test_boleto_automatico_ao_fechar(self):
        self.post(self.a, "/admin/financeiro", {"acao": "boletos_config", "boleto_multa": "2", "boleto_juros": "1",
                                                "boleto_auto": "on"}, "/admin/financeiro")
        self.faturado()
        self.assertEqual(len(self.efi.cobrancas), 3)

    def test_sem_credenciais_fica_desligado(self):
        app.config.update(EFI_CLIENT_ID="")
        oid, _ = self.faturado()
        self.post(self.a, f"/admin/orders/{oid}/boletos", {}, f"/admin/orders/{oid}")
        self.assertEqual(self.efi.cobrancas, {})
        self.assertIn("Desligado", self.a.get("/admin/financeiro?tipo=receber").get_data(as_text=True))


class TesteHoje(BaseMargem):
    def test_quadros_por_area(self):
        self.ajustar_produto(2, stock=0)
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 1)
        self.fechar(c, "orcamento")
        with app.app_context():
            db.session.add(Lancamento(tipo="pagar", descricao="Aluguel", valor=Decimal("1000"),
                                      vencimento=hoje() + timedelta(days=2)))
            db.session.commit()
        html = self.a.get("/admin/hoje").get_data(as_text=True)
        for titulo in ("Orçamentos esperando resposta", "Estoque zerado", "A pagar até daqui a 7 dias"):
            self.assertIn(titulo, html)
        with app.app_context():
            u = User(name="Vend", email="vend@loja-exemplo.com", is_admin=True, email_verified=True, permissoes="pedidos")
            u.set_password("senha-vendedor-123")
            db.session.add(u)
            db.session.commit()
        v = self.cliente()
        self.post(v, "/admin/login", {"email": "vend@loja-exemplo.com", "password": "senha-vendedor-123"}, "/admin/login")
        html = v.get("/admin/hoje").get_data(as_text=True)
        self.assertIn("Orçamentos esperando resposta", html)
        self.assertNotIn("Estoque zerado", html)
        self.assertNotIn("A pagar", html)


if __name__ == "__main__":
    unittest.main()
