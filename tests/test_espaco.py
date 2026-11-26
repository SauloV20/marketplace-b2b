"""Aviso por e-mail quando o banco de dados está enchendo (armazenamento.avisar_se_cheio)."""
import unittest
from datetime import timedelta
from unittest import mock

from test_b2b import BaseB2B

from app import app  # noqa: E402
import armazenamento  # noqa: E402
from models import agora_utc  # noqa: E402

LIMITE = 512 * 1_000_000


class TesteAvisoDeEspaco(BaseB2B):
    def setUp(self):
        super().setUp()
        self.antes = {k: app.config.get(k) for k in ("LIMITE_BANCO_MB", "TAREFAS_TOKEN", "BACKUP_SENHA",
                                                      "ALERTA_EMAIL")}
        app.config.update(LIMITE_BANCO_MB=512, ALERTA_EMAIL="")
        self.emails.clear()

    def tearDown(self):
        app.config.update(self.antes)
        super().tearDown()

    def avisos(self):
        return [e for e in self.emails if "banco de dados" in e["assunto"]]

    def conferir(self, pct, quando=None):
        with app.app_context(), \
                mock.patch("armazenamento.tamanho_do_banco", return_value=LIMITE * pct // 100), \
                mock.patch("models.agora_utc", return_value=quando or agora_utc()):
            return armazenamento.avisar_se_cheio()

    def test_limite_padrao_e_o_do_neon_gratis(self):
        import config
        self.assertEqual(config.Config.LIMITE_BANCO_MB, 512)

    def test_abaixo_de_80_nao_avisa(self):
        self.assertIsNone(self.conferir(79))
        self.assertEqual(self.avisos(), [])

    def test_acima_de_80_avisa_o_dono_uma_vez(self):
        self.assertEqual(self.conferir(85), 80)
        [aviso] = self.avisos()
        self.assertEqual(aviso["para"], "admin@exemplo.com")
        self.assertEqual(aviso["assunto"], "Marketplace B2B: banco de dados 85% cheio")
        self.assertIn("435 MB de 512 MB", aviso["texto"])
        self.assertIn("Backup e espaço", aviso["texto"])
        self.assertIn("Fotos de produtos", aviso["texto"])
        self.assertIsNone(self.conferir(86), "mesmo nível: não repete logo")
        self.assertEqual(len(self.avisos()), 1)

    def test_mesmo_nivel_repete_depois_de_3_dias(self):
        self.conferir(85)
        self.assertIsNone(self.conferir(85, agora_utc() + timedelta(days=2)))
        self.assertEqual(self.conferir(85, agora_utc() + timedelta(days=3, minutes=1)), 80)
        self.assertEqual(len(self.avisos()), 2)

    def test_nivel_mais_alto_avisa_na_hora(self):
        self.conferir(85)
        self.assertEqual(self.conferir(91), 90)
        self.assertEqual(self.conferir(96), 95)
        self.assertEqual([a["assunto"] for a in self.avisos()][-1], "Marketplace B2B: banco de dados 96% cheio")

    def test_voltou_ao_normal_e_encheu_de_novo_avisa(self):
        self.conferir(85)
        self.conferir(50)  # comprimiu as fotos
        self.assertEqual(self.conferir(82), 80)
        self.assertEqual(len(self.avisos()), 2)

    def test_alerta_email_tem_prioridade(self):
        app.config["ALERTA_EMAIL"] = "dono@loja.com"
        self.conferir(85)
        self.assertEqual([a["para"] for a in self.avisos()], ["dono@loja.com"])

    def test_falha_ao_enviar_tenta_de_novo_depois(self):
        with mock.patch("email_utils.enviar_email_detalhado", return_value=(False, "fora do ar")):
            self.assertIsNone(self.conferir(85))
        self.assertEqual(self.conferir(85), 80, "não marcou como avisado")

    def test_tarefa_semanal_do_github_confere_o_espaco(self):
        app.config.update(TAREFAS_TOKEN="token-secreto-123", BACKUP_SENHA="senha forte 123")
        with mock.patch("armazenamento.tamanho_do_banco", return_value=LIMITE * 90 // 100):
            r = self.cliente().post("/tarefas/backup-semanal", headers={"Authorization": "Bearer token-secreto-123"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.avisos()), 1)

    def test_visitas_conferem_no_maximo_a_cada_6_horas(self):
        chamadas = []

        class LinhaNaHora:  # roda na hora, para o teste não depender de outra linha de execução
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                self.target()
        armazenamento._checado["em"] = 0.0
        with app.test_request_context(), mock.patch("threading.Thread", LinhaNaHora), \
                mock.patch("armazenamento.avisar_se_cheio", side_effect=lambda: chamadas.append(1)):
            armazenamento.checar_de_vez_em_quando()
            armazenamento.checar_de_vez_em_quando()
            self.assertEqual(len(chamadas), 1)
            with mock.patch("armazenamento.time.time", return_value=armazenamento.time.time() + 6 * 3600 + 1):
                armazenamento.checar_de_vez_em_quando()
        self.assertEqual(len(chamadas), 2)

    def test_monitor_e_arquivos_nao_conferem_o_espaco(self):
        app.config["TESTING"] = False
        try:
            with mock.patch("armazenamento.checar_de_vez_em_quando") as checar:
                self.cliente().get("/saude")
                self.cliente().get("/static/style.css").close()
                checar.assert_not_called()
                self.cliente().get("/")
                checar.assert_called_once()
        finally:
            app.config["TESTING"] = True

    def test_maiores_tabelas_so_no_postgres(self):
        with app.app_context():
            self.assertEqual(armazenamento.maiores_tabelas(), [], "no SQLite dos testes não mede por tabela")


if __name__ == "__main__":
    unittest.main()
