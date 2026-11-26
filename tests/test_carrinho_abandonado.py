"""Carrinho abandonado: carrinho guardado na conta, lembrete por e-mail, descadastro e pedido recuperado."""
import re
import unittest
from datetime import timedelta

from test_b2b import BaseB2B, SENHA

from app import app  # noqa: E402
import carrinho_abandonado  # noqa: E402
from models import CarrinhoSalvo, Order, User, agora_utc, db  # noqa: E402

TOKEN = "token-das-tarefas-123"


class TesteCarrinhoAbandonado(BaseB2B):
    def setUp(self):
        super().setUp()
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=50)
        self.ajustar_produto(2, qtd_minima=1, multiplo=1, stock=50)

    def salvo(self, email="cli@teste.com"):
        with app.app_context():
            u = User.query.filter_by(email=email).first()
            c = CarrinhoSalvo.query.filter_by(user_id=u.id).first()
            return (carrinho_abandonado._ler(c.itens), c.avisado_em) if c else (None, None)

    def envelhecer(self, horas=30, email="cli@teste.com"):
        with app.app_context():
            u = User.query.filter_by(email=email).first()
            c = CarrinhoSalvo.query.filter_by(user_id=u.id).first()
            c.atualizado_em = agora_utc() - timedelta(hours=horas)
            db.session.commit()

    def enviar(self):
        with app.test_request_context():
            return carrinho_abandonado.enviar_lembretes()

    def lembretes(self):
        return [e for e in self.emails if "continuam no carrinho" in e["assunto"]]

    def test_carrinho_volta_em_outro_aparelho(self):
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 3)
        self.assertEqual(self.salvo()[0], {"1": 3})
        outro = self.cliente()
        self.post(outro, "/entrar", {"email": "cli@teste.com", "senha": SENHA}, "/entrar")
        self.assertIn("3", outro.get("/carrinho").get_data(as_text=True))
        with outro.session_transaction() as s:
            self.assertEqual(s.get("cart"), {"1": 3})

    def test_sair_nao_deixa_o_carrinho_no_navegador(self):
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 2)
        self.post(c, "/sair", {})
        with c.session_transaction() as s:
            self.assertNotIn("cart", s)
        self.cadastrar("outra@teste.com")
        self.assertEqual(self.salvo()[0], {"1": 2}, "o carrinho continua guardado na conta")

    def test_lembrete_sai_uma_vez_depois_do_prazo(self):
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 2)
        self.emails.clear()
        self.assertEqual(self.enviar()["enviados"], 0, "ainda dentro do prazo")
        self.envelhecer(30)
        self.assertEqual(self.enviar()["enviados"], 1)
        email = self.lembretes()[0]
        self.assertEqual(email["para"], "cli@teste.com")
        self.assertIn("2x", email["texto"])
        self.assertIn("/carrinho", email["texto"])
        self.assertEqual(self.enviar()["enviados"], 0, "não manda de novo")
        self.adicionar(c, 2, 1)
        self.envelhecer(30)
        self.assertEqual(self.enviar()["enviados"], 0, "mudou, mas o último lembrete foi há menos de 7 dias")

    def test_quem_fechou_o_pedido_nao_recebe_e_conta_como_recuperado(self):
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 2)
        self.envelhecer(30)
        self.enviar()
        self.fechar(c, "pix")
        with app.app_context():
            self.assertTrue(Order.query.order_by(Order.id.desc()).first().veio_do_lembrete)
        self.assertEqual(self.salvo()[0], {}, "o carrinho esvaziou com o pedido")
        a = self.admin()
        self.assertIn("1 pedido(s) recuperado(s)", a.get("/admin/carrinhos").get_data(as_text=True))

    def test_carrinho_vazio_ou_antigo_nao_recebe(self):
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 2)
        self.envelhecer(24 * 8)
        self.emails.clear()
        self.assertEqual(self.enviar()["enviados"], 0, "parado há mais de 7 dias")
        self.post(c, "/carrinho/esvaziar", {})
        self.envelhecer(30)
        self.assertEqual(self.enviar()["enviados"], 0)

    def test_produto_sem_estoque_fica_fora_e_carrinho_so_com_ele_nao_recebe(self):
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 2)
        self.ajustar_produto(1, stock=0)
        self.envelhecer(30)
        self.emails.clear()
        self.assertEqual(self.enviar(), {"enviados": 0, "pulados": 1})

    def test_parar_pelo_link_do_email_e_ligar_de_novo(self):
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 2)
        self.envelhecer(30)
        self.emails.clear()
        self.enviar()
        link = re.search(r"https?://\S+/lembretes/parar/\S+", self.lembretes()[0]["texto"]).group(0)
        caminho = link.split("localhost", 1)[-1]
        anonimo = self.cliente()
        self.assertIn("Não quero mais lembretes", anonimo.get(caminho).get_data(as_text=True))
        self.assertIn("Pronto", self.post(anonimo, caminho, {}, caminho).get_data(as_text=True))
        with app.app_context():
            self.assertFalse(User.query.filter_by(email="cli@teste.com").first().lembrete_carrinho)
        self.assertEqual(self.cliente().get(caminho[:-3] + "xyz").status_code, 404, "link adulterado")
        self.post(c, "/conta/lembretes", {"lembrete_carrinho": "on"}, "/conta/privacidade")
        with app.app_context():
            self.assertTrue(User.query.filter_by(email="cli@teste.com").first().lembrete_carrinho)

    def test_quem_pediu_para_parar_nao_recebe(self):
        c = self.cadastrar("cli@teste.com")
        self.post(c, "/conta/lembretes", {}, "/conta/privacidade")
        self.adicionar(c, 1, 2)
        self.envelhecer(30)
        self.emails.clear()
        self.assertEqual(self.enviar()["enviados"], 0)

    def test_desligado_no_painel_e_horas(self):
        a = self.admin()
        self.post(a, "/admin/carrinhos", {"horas": "500"}, "/admin/carrinhos")
        self.assertTrue(carrinho_abandonado_ligado())
        self.post(a, "/admin/carrinhos", {"horas": "2"}, "/admin/carrinhos")
        self.assertFalse(carrinho_abandonado_ligado())
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 2)
        self.envelhecer(3)
        self.emails.clear()
        self.assertTrue(self.enviar().get("desligado"))
        self.post(a, "/admin/carrinhos", {"horas": "2", "ligado": "on"}, "/admin/carrinhos")
        self.post(a, "/admin/carrinhos", {"acao": "enviar"}, "/admin/carrinhos")
        self.assertEqual(len(self.lembretes()), 1)
        self.assertIn("cli@teste.com", a.get("/admin/carrinhos").get_data(as_text=True))

    def test_tarefa_agendada_exige_token(self):
        app.config.update(TAREFAS_TOKEN=TOKEN)
        self.addCleanup(app.config.update, TAREFAS_TOKEN="")
        c = self.cliente()
        self.assertEqual(c.post("/tarefas/carrinhos").status_code, 403)
        r = c.post("/tarefas/carrinhos", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual((r.status_code, r.get_json()["enviados"]), (200, 0))

    def test_exportacao_lgpd_e_exclusao_levam_o_carrinho(self):
        c = self.cadastrar("cli@teste.com")
        self.adicionar(c, 1, 2)
        dados = c.get("/conta/meus-dados.json").get_json()
        self.assertEqual(dados["carrinho"][0]["quantidade"], 2)
        with app.app_context():
            u = User.query.filter_by(email="cli@teste.com").first()
            db.session.delete(u)
            db.session.commit()
            self.assertEqual(CarrinhoSalvo.query.count(), 0)


def carrinho_abandonado_ligado():
    with app.app_context():
        return carrinho_abandonado.ligado()


if __name__ == "__main__":
    unittest.main()
