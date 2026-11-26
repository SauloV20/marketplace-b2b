"""Etiqueta e postagem pelo Melhor Envio, com a API simulada: nada sai para a internet nem gasta saldo."""
import unittest
from decimal import Decimal

from test_melhor_envio import AJUSTES, BaseMelhorEnvio

from app import app  # noqa: E402
import etiquetas  # noqa: E402
import melhor_envio  # noqa: E402
from b2b import salvar_config  # noqa: E402
from models import EtiquetaEnvio, Order, db  # noqa: E402

PACOTE = {"price": "39.80", "format": "box", "dimensions": {"height": 10, "width": 15, "length": 20},
          "weight": "1.20", "insurance_value": "0.00", "products": [{"id": "1", "quantity": 1}]}
COTACAO = [{"id": 1, "name": "PAC", "custom_price": "21.30", "custom_delivery_time": 6,
            "company": {"name": "Correios"}, "packages": [dict(PACOTE, price="21.30")]},
           {"id": 2, "name": "SEDEX", "custom_price": "39.80", "custom_delivery_time": 2,
            "company": {"name": "Correios"}, "packages": [PACOTE]}]
EMPRESA = {"nfe_razao_social": "Marketplace B2B Ferramentas Ltda", "nfe_cnpj": "11222333000181", "nfe_ie": "123456789",
           "nfe_logradouro": "Rua da Loja", "nfe_numero": "100", "nfe_bairro": "Centro", "nfe_municipio": "São Paulo",
           "nfe_uf": "SP", "nfe_cep": "01310100", "nfe_telefone": "11999990000",
           "empresa_email_privacidade": "contato@loja.test"}


class BaseEtiqueta(BaseMelhorEnvio):
    def setUp(self):
        super().setUp()
        self.resposta = COTACAO
        self.recusar = {}  # caminho -> mensagem de erro simulada
        self.rastreio = {"status": "posted", "tracking": "ME123456BR"}
        self.n = 0

    def http_falso(self, url, corpo, token, contato, metodo="POST"):
        caminho = url.split(".com.br", 1)[1]
        self.chamadas.append({"url": url, "caminho": caminho, "corpo": corpo, "metodo": metodo, "token": token,
                              "contato": contato})
        if caminho in self.recusar:
            raise melhor_envio.ErroMelhorEnvio(self.recusar[caminho])
        if caminho.endswith("/calculate"):
            return self.resposta
        if caminho == "/api/v2/me/cart":
            self.n += 1
            return {"id": f"{self.n:08d}-0000-0000-0000-000000000000", "protocol": f"ORD-{self.n}", "price": 39.8}
        ids = (corpo or {}).get("orders") or []
        if caminho.endswith("/generate"):
            return {i: {"status": True, "message": "Envio gerado com sucesso"} for i in ids}
        if caminho.endswith("/print"):
            return {"url": "https://sandbox.melhorenvio.com.br/imprimir/abc"}
        if caminho.endswith("/tracking"):
            return {i: dict(self.rastreio, id=i) for i in ids}
        return {}

    def pedido_com_sedex(self, **empresa):
        with app.app_context():
            for k, v in {**EMPRESA, **empresa}.items():
                salvar_config(k, v)
            db.session.commit()
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)
        self.a = self.ligar()
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix", frete_opcao="me:2", address="Rua das Flores, 250, Centro, São Paulo")
        with app.app_context():
            return Order.query.order_by(Order.id.desc()).first().id

    def comprar(self, oid, **extra):
        html = self.a.get(f"/admin/orders/{oid}").get_data(as_text=True)
        self.assertIn("Comprar etiqueta", html)
        dados = {"acao": "comprar", "nome": "Cliente Teste", "documento": self.documento(oid), "ie": "",
                 "telefone": "11988887777", "email": "cli@teste.com", "cep": "01001000", "logradouro": "Praça da Sé",
                 "numero": "250", "complemento": "", "bairro": "Sé", "municipio": "São Paulo", "uf": "SP",
                 "servico": "2", "tipo": "declaracao", "chave": "", **extra}
        return self.post(self.a, f"/admin/orders/{oid}/etiqueta", dados, f"/admin/orders/{oid}")

    def documento(self, oid):
        with app.app_context():
            return db.session.get(Order, oid).documento

    def etiquetas_do(self, oid):
        with app.app_context():
            return [(e.situacao, e.valor, e.link_impressao, e.rastreio, e.erro)
                    for e in EtiquetaEnvio.query.filter_by(order_id=oid).order_by(EtiquetaEnvio.id)]

    def caminhos(self):
        return [c["caminho"] for c in self.chamadas if not c["caminho"].endswith("/calculate")]


class TesteEtiqueta(BaseEtiqueta):
    def test_compra_completa(self):
        oid = self.pedido_com_sedex()
        self.chamadas.clear()
        r = self.comprar(oid)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.caminhos(), ["/api/v2/me/cart", "/api/v2/me/shipment/checkout",
                                           "/api/v2/me/shipment/generate", "/api/v2/me/shipment/print"])
        carrinho = next(c["corpo"] for c in self.chamadas if c["caminho"] == "/api/v2/me/cart")
        self.assertEqual(carrinho["service"], 2)
        self.assertEqual(carrinho["from"]["company_document"], "11222333000181")
        self.assertEqual(carrinho["from"]["state_register"], "")  # declaração de conteúdo: sem IE
        self.assertEqual(carrinho["to"]["document"], self.documento(oid))
        self.assertEqual((carrinho["to"]["number"], carrinho["to"]["postal_code"]), ("250", "01001000"))
        self.assertEqual(carrinho["volumes"], [{"height": 10, "width": 15, "length": 20, "weight": 1.2}])
        self.assertTrue(carrinho["options"]["non_commercial"])
        self.assertNotIn("invoice", carrinho["options"])
        self.assertEqual(carrinho["products"][0]["quantity"], "1")
        self.assertEqual(self.etiquetas_do(oid),
                         [("gerada", Decimal("39.80"), "https://sandbox.melhorenvio.com.br/imprimir/abc", "", "")])
        html = self.a.get(f"/admin/orders/{oid}").get_data(as_text=True)
        self.assertIn("Pronta para imprimir", html)
        self.assertIn("imprimir/abc", html)
        self.assertNotIn("Comprar etiqueta", html)
        self.assertIn("Etiqueta comprada no Melhor Envio", html)

    def test_com_nota_fiscal(self):
        oid = self.pedido_com_sedex()
        self.comprar(oid, tipo="nota", chave="3526 1111 2223 3300 0181 5500 1000 0000 0110 0000 0019")
        carrinho = next(c["corpo"] for c in self.chamadas if c["caminho"] == "/api/v2/me/cart")
        self.assertEqual(carrinho["options"]["invoice"]["key"], "35261111222333000181550010000000011000000019")
        self.assertFalse(carrinho["options"]["non_commercial"])
        self.assertEqual(carrinho["from"]["state_register"], "123456789")

    def test_formulario_separa_o_endereco(self):
        oid = self.pedido_com_sedex()
        with app.app_context():
            f = etiquetas.preparar(db.session.get(Order, oid))
        self.assertEqual((f["logradouro"], f["numero"], f["bairro"], f["municipio"], f["uf"], f["servico"], f["tipo"]),
                         ("Rua das Flores", "250", "Centro", "São Paulo", "SP", "2", "declaracao"))

    def test_validacoes(self):
        oid = self.pedido_com_sedex()
        casos = [({"documento": "123"}, "CPF ou CNPJ do destinatário inválido"),
                 ({"numero": ""}, "Informe o número"),
                 ({"uf": "RJ"}, "não é de RJ"),
                 ({"tipo": "nota", "chave": "123"}, "44 números"),
                 ({"servico": "99"}, "Escolha o serviço")]
        for extra, msg in casos:
            self.comprar(oid, **extra)
            self.assertIn(msg, self.a.get(f"/admin/orders/{oid}").get_data(as_text=True), extra)
        self.assertEqual(self.etiquetas_do(oid), [])
        self.assertNotIn("/api/v2/me/cart", self.caminhos())

    def test_empresa_incompleta_nao_compra(self):
        oid = self.pedido_com_sedex(nfe_logradouro="", nfe_telefone="")
        self.chamadas.clear()
        self.comprar(oid)
        html = self.a.get(f"/admin/orders/{oid}").get_data(as_text=True)
        self.assertIn("Complete os dados da empresa", html)
        self.assertEqual(self.caminhos(), [])

    def test_sem_saldo_e_continuar(self):
        oid = self.pedido_com_sedex()
        self.recusar["/api/v2/me/shipment/checkout"] = "Saldo insuficiente"
        self.comprar(oid)
        situacao, _, _, _, erro = self.etiquetas_do(oid)[0]
        self.assertEqual(situacao, "carrinho")
        self.assertIn("Saldo insuficiente", erro)
        html = self.a.get(f"/admin/orders/{oid}").get_data(as_text=True)
        self.assertIn("Saldo insuficiente", html)
        self.assertIn('value="continuar"', html)
        del self.recusar["/api/v2/me/shipment/checkout"]
        self.chamadas.clear()
        self.post(self.a, f"/admin/orders/{oid}/etiqueta", {"acao": "continuar"}, f"/admin/orders/{oid}")
        self.assertEqual(self.caminhos(), ["/api/v2/me/shipment/checkout", "/api/v2/me/shipment/generate",
                                           "/api/v2/me/shipment/print"])  # não inclui no carrinho de novo
        self.assertEqual(self.etiquetas_do(oid)[0][0], "gerada")

    def test_paga_mas_nao_gerada_nao_paga_de_novo(self):
        oid = self.pedido_com_sedex()
        self.recusar["/api/v2/me/shipment/generate"] = "Transportadora indisponível"
        self.comprar(oid)
        self.assertEqual(self.etiquetas_do(oid)[0][0], "paga")
        del self.recusar["/api/v2/me/shipment/generate"]
        self.chamadas.clear()
        self.post(self.a, f"/admin/orders/{oid}/etiqueta", {"acao": "continuar"}, f"/admin/orders/{oid}")
        self.assertNotIn("/api/v2/me/shipment/checkout", self.caminhos())
        self.assertEqual(self.etiquetas_do(oid)[0][0], "gerada")

    def test_rastreio_marca_enviado_e_entregue(self):
        oid = self.pedido_com_sedex()
        self.comprar(oid)
        emails = len(self.emails)
        self.post(self.a, f"/admin/orders/{oid}/etiqueta", {"acao": "rastrear"}, f"/admin/orders/{oid}")
        with app.app_context():
            o = db.session.get(Order, oid)
            self.assertEqual((o.envio, o.transportadora, o.rastreio), ("enviado", "Correios", "ME123456BR"))
            self.assertIn("ME123456BR", o.rastreio_link)
        self.assertEqual(len(self.emails), emails + 1)
        self.assertIn("ME123456BR", self.emails[-1]["texto"])
        self.rastreio = {"status": "delivered", "tracking": "ME123456BR"}
        self.post(self.a, f"/admin/orders/{oid}/etiqueta", {"acao": "rastrear"}, f"/admin/orders/{oid}")
        with app.app_context():
            self.assertEqual(db.session.get(Order, oid).envio, "entregue")
        self.assertEqual(self.etiquetas_do(oid)[0][0], "entregue")

    def test_cancelar(self):
        oid = self.pedido_com_sedex()
        self.comprar(oid)
        with app.app_context():
            eid = EtiquetaEnvio.query.filter_by(order_id=oid).one().id
        self.chamadas.clear()
        self.post(self.a, f"/admin/orders/{oid}/etiqueta", {"acao": "cancelar", "eid": str(eid)}, f"/admin/orders/{oid}")
        self.assertEqual(self.caminhos(), ["/api/v2/me/shipment/cancel"])
        self.assertEqual(self.chamadas[-1]["corpo"]["order"]["reason_id"], "2")
        self.assertEqual(self.etiquetas_do(oid)[0][0], "cancelada")
        html = self.a.get(f"/admin/orders/{oid}").get_data(as_text=True)
        self.assertIn("Comprar etiqueta", html)  # dá para comprar outra
        self.assertIn("Canceladas:", html)

    def test_cancelar_no_carrinho_tira_do_carrinho(self):
        oid = self.pedido_com_sedex()
        self.recusar["/api/v2/me/shipment/checkout"] = "Saldo insuficiente"
        self.comprar(oid)
        with app.app_context():
            e = EtiquetaEnvio.query.filter_by(order_id=oid).one()
            eid, me_id = e.id, e.me_id
        self.chamadas.clear()
        self.post(self.a, f"/admin/orders/{oid}/etiqueta", {"acao": "cancelar", "eid": str(eid)}, f"/admin/orders/{oid}")
        self.assertEqual([(c["metodo"], c["caminho"]) for c in self.chamadas], [("DELETE", f"/api/v2/me/cart/{me_id}")])

    def test_varios_volumes_uma_etiqueta_por_volume(self):
        self.resposta = [dict(COTACAO[1], packages=[PACOTE, dict(PACOTE, weight="2.00")])]
        oid = self.pedido_com_sedex()
        self.comprar(oid)
        carrinhos = [c["corpo"] for c in self.chamadas if c["caminho"] == "/api/v2/me/cart"]
        self.assertEqual(len(carrinhos), 2)
        self.assertEqual([c["volumes"][0]["weight"] for c in carrinhos], [1.2, 2.0])
        self.assertEqual(len(self.etiquetas_do(oid)), 2)

    def test_sem_token_nao_mostra(self):
        oid = self.pedido_com_sedex()
        app.config.update(MELHOR_ENVIO_TOKEN="")
        self.assertNotIn('id="etiqueta"', self.a.get(f"/admin/orders/{oid}").get_data(as_text=True))

    def test_erro_da_api_legivel(self):
        self.assertEqual(melhor_envio._texto_erro('{"error": {"volumes": [["Peso do pacote 1 não informado"]]}}'),
                         "Peso do pacote 1 não informado")
        self.assertEqual(melhor_envio._texto_erro('{"message": "x", "errors": {"orders": ["Já paga."]}}'), "Já paga.")


if __name__ == "__main__":
    unittest.main()
