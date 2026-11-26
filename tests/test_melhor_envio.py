"""Frete pelo Melhor Envio (Correios e transportadoras), com a API simulada: nada sai para a internet."""
import unittest
from decimal import Decimal

from test_b2b import DADOS
from test_frete_ajuda import BaseFrete

from app import app  # noqa: E402
import frete  # noqa: E402
import melhor_envio  # noqa: E402
from models import Order, db  # noqa: E402

RESPOSTA = [
    {"id": 1, "name": "PAC", "price": "22.50", "custom_price": "21.30", "delivery_time": 6, "custom_delivery_time": 6,
     "company": {"id": 1, "name": "Correios"}},
    {"id": 2, "name": "SEDEX", "price": "41.90", "custom_price": "39.80", "delivery_time": 2,
     "custom_delivery_time": 2, "company": {"id": 1, "name": "Correios"}},
    {"id": 17, "name": "Mini Envios", "error": "Peso acima do limite", "company": {"id": 1, "name": "Correios"}},
]
AJUSTES = {"me_ativo": "on", "me_cep_origem": "01310-100", "me_servicos": ["1", "2"], "me_prazo_extra": "1",
           "me_acrescimo": "0", "me_altura": "10", "me_largura": "15", "me_comprimento": "20", "me_peso": "0,3"}


class BaseMelhorEnvio(BaseFrete):
    def setUp(self):
        super().setUp()
        self.chamadas, self.resposta, self.falhar = [], RESPOSTA, False
        self._http_original = melhor_envio._http
        melhor_envio._http = self.http_falso
        melhor_envio.limpar_cache()
        app.config.update(MELHOR_ENVIO_TOKEN="token-teste", MELHOR_ENVIO_CONTATO="ti@loja.test")

    def tearDown(self):
        melhor_envio._http = self._http_original
        melhor_envio.limpar_cache()
        app.config.update(MELHOR_ENVIO_TOKEN="", MELHOR_ENVIO_CONTATO="")
        super().tearDown()

    def http_falso(self, url, corpo, token, contato, metodo="POST"):
        self.chamadas.append({"url": url, "corpo": corpo, "token": token, "contato": contato})
        if self.falhar:
            raise melhor_envio.ErroMelhorEnvio("fora do ar")
        return self.resposta

    def ligar(self, **extra):
        a = self.admin()
        r = self.post(a, "/admin/frete", {"acao": "melhor_envio", **AJUSTES, **extra}, "/admin/frete")
        self.assertEqual(r.status_code, 302)
        return a

    def pedido(self):
        with app.app_context():
            o = Order.query.order_by(Order.id.desc()).first()
            return o.frete, o.frete_servico, o.frete_codigo, o.frete_prazo, o.frete_combinar


class TesteMelhorEnvio(BaseMelhorEnvio):
    def test_desligado_nao_chama_a_api(self):
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        self.assertEqual(self.chamadas, [])
        self.assertTrue(self.pedido()[4])  # sem tabela e sem Melhor Envio: a combinar

    def test_cotacao_e_pedido_com_sedex(self):
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50, peso_kg=Decimal("1.5"),
                             altura_cm=Decimal("8"), largura_cm=None, comprimento_cm=None)
        self.ligar()
        c = self.cadastrar()
        self.adicionar(c, 1, 2)
        self.post(c, "/carrinho/cep", {"cep": "01001-000"}, "/carrinho")
        html = c.get("/checkout").get_data(as_text=True)
        self.assertIn("Correios PAC", html)
        self.assertIn("Correios SEDEX", html)
        self.assertNotIn("Mini Envios", html)  # serviço com erro fica de fora
        self.assertIn("7 dias úteis", html)  # 6 + 1 dia de separação
        corpo = self.chamadas[-1]["corpo"]
        self.assertEqual(corpo["from"]["postal_code"], "01310100")
        self.assertEqual(corpo["services"], "1,2")
        p = corpo["products"][0]
        self.assertEqual((p["height"], p["width"], p["length"], p["weight"], p["quantity"]), (8, 15, 20, 1.5, 2))
        self.assertEqual(p["insurance_value"], 0)
        self.assertEqual(self.chamadas[-1]["token"], "token-teste")
        self.fechar(c, "pix", frete_opcao="me:2")
        valor, servico, codigo, prazo, combinar = self.pedido()
        self.assertEqual((valor, servico, codigo, prazo, combinar),
                         (Decimal("39.80"), "Correios SEDEX", "me:2", "3 dias úteis", False))
        self.assertIn("Correios SEDEX", self.emails[-2]["texto"] + self.emails[-1]["texto"])
        self.assertEqual(len(self.chamadas), 1)  # a mesma cotação vale até a confirmação

    def test_sem_escolha_vale_a_mais_barata(self):
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)
        self.ligar()
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        self.assertEqual(self.pedido()[:3], (Decimal("21.30"), "Correios PAC", "me:1"))

    def test_tabela_aparece_junto(self):
        self.regra(nome="SP interno", ufs="SP", valor=Decimal("15"), prazo="1 dia útil")
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)
        self.ligar()
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        r = c.get("/carrinho/frete?cep=01001000").get_json()
        self.assertEqual(r["codigo"], "tabela")  # a mais barata
        self.assertIn("Entrega padrão", r["opcoes"])
        self.assertIn("Correios SEDEX", r["opcoes"])
        self.assertNotIn("SP interno", r["opcoes"])  # o nome interno da regra não aparece para o cliente
        r = c.get("/carrinho/frete?cep=01001000&opcao=me:2").get_json()
        self.assertEqual((r["codigo"], r["valor"]), ("me:2", "R$ 39,80"))
        self.fechar(c, "pix", frete_opcao="tabela")
        self.assertEqual(self.pedido()[:3], (Decimal("15.00"), "", "tabela"))

    def test_api_fora_do_ar_segue_com_a_tabela(self):
        self.regra(nome="Brasil", valor=Decimal("30"))
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)
        self.ligar()
        self.falhar = True
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        self.assertEqual(self.pedido()[:3], (Decimal("30.00"), "", "tabela"))
        chamadas = len(self.chamadas)
        with app.app_context():
            from models import Product
            self.assertEqual(melhor_envio.cotar("30130000", [melhor_envio.item(db.session.get(Product, 1), 1, 10)]),
                             [])
        self.assertEqual(len(self.chamadas), chamadas)  # pausa depois do erro: não insiste

    def test_opcao_que_sumiu_pede_nova_escolha(self):
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)
        self.ligar()
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        with app.app_context():
            antes = Order.query.count()
        r = self.fechar(c, "pix", frete_opcao="me:3")
        self.assertIn("Escolha a entrega de novo", r.get_data(as_text=True))
        with app.app_context():
            self.assertEqual(Order.query.count(), antes)

    def test_acrescimo_e_seguro(self):
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)
        self.ligar(me_acrescimo="10", me_seguro="on")
        with app.app_context():
            from models import Product
            p = db.session.get(Product, 1)
            ops = melhor_envio.cotar("30130000", [melhor_envio.item(p, 1, Decimal("100"))])
        self.assertEqual(ops[0]["valor"], Decimal("23.43"))  # 21,30 + 10%
        self.assertEqual(self.chamadas[-1]["corpo"]["products"][0]["insurance_value"], 100.0)

    def test_entrega_propria_nao_cota(self):
        with app.app_context():
            from models import ImpostoUF
            db.session.add(ImpostoUF(uf="SP", entrega="propria", custo_entrega=0))
            db.session.commit()
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)
        self.ligar()
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        self.assertEqual(self.chamadas, [])
        self.assertEqual(self.pedido()[:3], (Decimal("0"), "", "propria"))

    def test_validacoes_do_painel(self):
        a = self.admin()
        casos = [({"me_cep_origem": "123"}, "CEP de onde sai"),
                 ({"me_servicos": []}, "pelo menos um serviço"),
                 ({"me_acrescimo": "abc"}, "use um número"),
                 ({"me_prazo_extra": "1,5"}, "número inteiro"),
                 ({"me_altura": "0"}, "vai de")]
        for extra, msg in casos:
            r = self.post(a, "/admin/frete", {"acao": "melhor_envio", **AJUSTES, **extra}, "/admin/frete")
            self.assertIn(msg, a.get("/admin/frete").get_data(as_text=True), extra)
        with app.app_context():
            self.assertFalse(melhor_envio.ligado())

    def test_sem_token_avisa_e_nao_liga(self):
        app.config.update(MELHOR_ENVIO_TOKEN="")
        a = self.admin()
        self.post(a, "/admin/frete", {"acao": "melhor_envio", **AJUSTES}, "/admin/frete")
        self.assertIn("MELHOR_ENVIO_TOKEN", a.get("/admin/frete").get_data(as_text=True))
        with app.app_context():
            self.assertFalse(melhor_envio.ligado())

    def test_simular_no_painel(self):
        a = self.ligar()
        r = self.post(a, "/admin/frete", {"acao": "simular", "sim_cep": "30130-000", "sim_valor": "200",
                                          "sim_peso": "2"}, "/admin/frete")
        html = r.get_data(as_text=True)
        self.assertIn("Correios SEDEX", html)
        self.assertEqual(self.chamadas[-1]["corpo"]["products"][0]["weight"], 2.0)

    def test_medidas_do_produto_no_painel(self):
        a = self.admin()
        html = a.get("/admin/products/1/edit").get_data(as_text=True)
        self.assertIn('name="altura_cm"', html)
        with app.app_context():
            from models import Product
            p = db.session.get(Product, 1)
            dados = {"name": p.name, "category_id": str(p.category_id), "price": "10,00", "stock": "50",
                     "qtd_minima": "1", "multiplo": "1", "peso_kg": "1,2", "altura_cm": "12,5", "largura_cm": "",
                     "comprimento_cm": "30", "estoque_alerta": "0", "codigo": p.codigo or ""}
        r = self.post(a, "/admin/products/1/edit", dados, "/admin/products/1/edit")
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[-400:])
        with app.app_context():
            from models import Product
            p = db.session.get(Product, 1)
            self.assertEqual((p.altura_cm, p.largura_cm, p.comprimento_cm), (Decimal("12.5"), None, Decimal("30.0")))
        r = self.post(a, "/admin/products/1/edit", {**dados, "altura_cm": "-1"}, "/admin/products/1/edit")
        self.assertIn("da embalagem vai de", r.get_data(as_text=True))

    def test_cliente_altera_pedido_e_mantem_o_servico(self):
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)
        self.ligar()
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix", frete_opcao="me:2")
        with app.app_context():
            oid = Order.query.order_by(Order.id.desc()).first().id
        self.resposta = [dict(RESPOSTA[1], custom_price="45.00")]
        melhor_envio.limpar_cache()
        r = self.post(c, f"/conta/pedidos/{oid}/alterar", {"qtd_1": "2"}, f"/conta/pedidos/{oid}")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.pedido()[:3], (Decimal("45.00"), "Correios SEDEX", "me:2"))
        # Melhor Envio fora do ar na próxima mudança: o frete fica como estava (não vira "a combinar")
        self.falhar = True
        melhor_envio.limpar_cache()
        self.post(c, f"/conta/pedidos/{oid}/alterar", {"qtd_1": "3"}, f"/conta/pedidos/{oid}")
        valor, servico, codigo, _, combinar = self.pedido()
        self.assertEqual((valor, servico, codigo, combinar), (Decimal("45.00"), "Correios SEDEX", "me:2", False))


class TesteRespostaDaApi(unittest.TestCase):
    def test_ler_resposta(self):
        a = {"servicos": ["1", "2"], "acrescimo": Decimal(0), "prazo_extra": 0}
        ops = melhor_envio._ler_resposta(RESPOSTA + [{"id": 2, "price": "0"}, "lixo"], a)
        self.assertEqual([o["codigo"] for o in ops], ["me:1", "me:2"])
        self.assertEqual(ops[0]["prazo"], "6 dias úteis")
        self.assertEqual(melhor_envio._ler_resposta({"id": 1, "price": "10", "delivery_time": 1,
                                                     "company": {"name": "Correios"}, "name": "PAC"}, a)[0]["prazo"],
                         "1 dia útil")

    def test_escolher(self):
        ops = [{"codigo": "tabela", "valor": 5}, {"codigo": "me:2", "valor": 9}]
        self.assertEqual(frete.escolher(ops, "me:2")["codigo"], "me:2")
        self.assertEqual(frete.escolher(ops, "me:9")["codigo"], "tabela")
        self.assertIsNone(frete.escolher([], "me:2"))


if __name__ == "__main__":
    unittest.main()
