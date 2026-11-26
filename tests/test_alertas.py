"""Aviso por e-mail quando o site dá erro (alertas.py)."""
import unittest
from unittest import mock

from test_b2b import BaseB2B

from app import app  # noqa: E402
import alertas  # noqa: E402


class TesteAvisoDeErro(BaseB2B):
    def setUp(self):
        super().setUp()
        alertas.zerar()
        self.aviso = alertas.AvisoPorEmail(app)
        app.logger.addHandler(self.aviso)  # nos testes não liga sozinho (só no ar)
        self.antes = {k: app.config.get(k) for k in ("PROPAGATE_EXCEPTIONS", "ALERTA_EMAIL")}
        app.config["PROPAGATE_EXCEPTIONS"] = False  # deixa o Flask tratar o erro como no ar (página 500 + registro)

    def tearDown(self):
        app.logger.removeHandler(self.aviso)
        app.config.update(self.antes)
        super().tearDown()

    def quebrar_cadastro(self, c=None, caminho="/cadastro", erro=RuntimeError("falha de teste")):
        with mock.patch("blueprints.account.render_template", side_effect=erro):
            return (c or app.test_client()).get(caminho)

    def avisos(self):
        return [e for e in self.emails if e["assunto"].startswith("Marketplace B2B: erro no site")]

    def test_pagina_que_quebra_avisa_o_dono(self):
        r = self.quebrar_cadastro()
        self.assertEqual(r.status_code, 500)
        [aviso] = self.avisos()
        self.assertEqual(aviso["para"], "admin@exemplo.com", "sem e-mail configurado: o dono da loja")
        self.assertEqual(aviso["assunto"], "Marketplace B2B: erro no site em /cadastro")
        self.assertIn("Página: GET /cadastro", aviso["texto"])
        self.assertIn("RuntimeError: falha de teste", aviso["texto"])

    def test_aviso_nao_leva_dados_do_visitante(self):
        c = app.test_client()
        c.set_cookie("session", "cookie-secreto-do-cliente")
        self.quebrar_cadastro(c, "/cadastro?cpf=52998224725&email=fulano@ex.com")
        texto = self.avisos()[0]["texto"]
        for dado in ("52998224725", "fulano@ex.com", "cookie-secreto-do-cliente"):
            self.assertNotIn(dado, texto)

    def test_mesmo_erro_avisa_uma_vez_por_hora(self):
        for _ in range(5):
            self.quebrar_cadastro()
        self.assertEqual(len(self.avisos()), 1)
        with mock.patch("alertas.time.time", return_value=alertas.time.time() + 3601):
            self.quebrar_cadastro()
        self.assertEqual(len(self.avisos()), 2, "depois de 1 hora avisa de novo")

    def test_no_maximo_20_avisos_por_dia(self):
        for i in range(30):  # 30 erros diferentes
            app.logger.error("Erro diferente número %s", i)  # noqa: (texto fixo muda: cada um é "outro erro")
            alertas._estado["ultimo"].clear()
        self.assertEqual(len(self.avisos()), 20)

    def test_erro_ao_mandar_o_aviso_nao_vira_laco(self):
        chamadas = []

        def enviar_que_falha(para, assunto, texto, anexos=None):
            chamadas.append(para)
            app.logger.error("E-MAIL NÃO CONFIGURADO: o e-mail para %s não foi enviado.", para)
            return False, "falhou"
        with mock.patch("email_utils.enviar_email_detalhado", side_effect=enviar_que_falha):
            self.quebrar_cadastro()
        self.assertEqual(len(chamadas), 1, "o erro do próprio aviso não gera outro aviso")

    def test_erro_registrado_sem_pagina_tambem_avisa(self):
        with app.app_context():
            app.logger.error("Backup automático: Defina BACKUP_SENHA no Render.")
        [aviso] = self.avisos()
        self.assertEqual(aviso["assunto"], "Marketplace B2B: erro no site")
        self.assertIn("Defina BACKUP_SENHA", aviso["texto"])

    def test_alerta_email_do_render_tem_prioridade(self):
        app.config["ALERTA_EMAIL"] = "dono@loja.com, socio@loja.com"
        self.quebrar_cadastro()
        self.assertEqual([a["para"] for a in self.avisos()], ["dono@loja.com", "socio@loja.com"])

    def test_aviso_so_liga_no_ar(self):
        falso = mock.Mock(config={})
        alertas.ligar(falso)
        falso.logger.addHandler.assert_not_called()
        falso = mock.Mock(config={"PRODUCAO": True})
        alertas.ligar(falso)
        falso.logger.addHandler.assert_called_once()

    def test_aviso_de_erro_nao_atrapalha_avisos_comuns(self):
        app.logger.warning("só um aviso")
        app.logger.info("informação")
        self.assertEqual(self.avisos(), [])


if __name__ == "__main__":
    unittest.main()
