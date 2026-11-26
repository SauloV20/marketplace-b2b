"""Testes do backup e da restauração pelo painel (ida e volta de verdade, num banco de teste)."""
import gzip
import io
import json
import unittest
from decimal import Decimal
from unittest import mock

from test_b2b import BaseB2B
from test_banners import PNG
from test_site import ADMIN_SENHA

from app import app  # noqa: E402
import backup  # noqa: E402
from models import Order, Product, ProdutoFoto, User, db  # noqa: E402


SENHA_ARQ = "senha-do-arquivo-123"


class TesteBackup(BaseB2B):
    def baixar(self, a, cifrado=False):
        """Baixa pelo painel (sempre cifrado). Devolve o .json.gz já aberto, ou o arquivo como veio (cifrado=True)."""
        r = self.post(a, "/admin/backup/baixar", {"senha_arquivo": SENHA_ARQ, "senha_arquivo2": SENHA_ARQ},
                      "/admin/backup")
        self.assertEqual(r.mimetype, "application/octet-stream")
        self.assertIn(".cifrado", r.headers["Content-Disposition"])
        self.assertTrue(r.data.startswith(backup.CIFRADO))
        return r.data if cifrado else backup.decifrar(r.data, SENHA_ARQ)

    def restaurar(self, a, arquivo, senha=ADMIN_SENHA, confirmacao="RESTAURAR"):
        dados = {"_csrf": self.token(a, "/admin/backup"), "senha": senha, "confirmacao": confirmacao,
                 "arquivo": (io.BytesIO(arquivo), "backup.json.gz")}
        return a.post("/admin/backup/restaurar", data=dados, content_type="multipart/form-data")

    def foto(self, pid):
        with app.app_context():
            db.session.add(ProdutoFoto(product_id=pid, dados=PNG, tipo="image/png", ordem=1))
            db.session.commit()

    def test_conteudo_do_backup(self):
        self.foto(1)
        c = self.cadastrar()
        self.adicionar(c, 1, 1)
        self.fechar(c, "pix")
        conteudo = json.loads(gzip.decompress(self.baixar(self.admin())))
        self.assertEqual(conteudo["formato"], "marketplace-backup")
        t = conteudo["tabelas"]
        self.assertEqual(len(t["product"]), 18)
        self.assertEqual(len(t["order"]), 81)
        self.assertIn("b64", t["produto_foto"][0]["dados"])
        self.assertNotIn("limite_uso", t, "tabelas temporárias ficam de fora")

    def test_ida_e_volta(self):
        self.foto(3)
        a = self.admin()
        arquivo = self.baixar(a)
        with app.app_context():
            antes = {p.id: (p.name, p.price, p.stock) for p in Product.query.all()}
            pedidos = Order.query.count()
        # muda tudo depois do backup
        self.ajustar_produto(1, price=Decimal("1.00"), name="Mudado")
        self.cadastrar("novo@teste.com")
        with app.app_context():
            ProdutoFoto.query.delete()
            db.session.commit()
        r = self.restaurar(a, arquivo)
        self.assertTrue(r.headers["Location"].endswith("/admin/login"))
        with app.app_context():
            self.assertEqual({p.id: (p.name, p.price, p.stock) for p in Product.query.all()}, antes)
            self.assertEqual(Order.query.count(), pedidos)
            self.assertIsNone(User.query.filter_by(email="novo@teste.com").first())
            self.assertEqual(ProdutoFoto.query.one().dados, PNG)
        self.assertEqual(a.get("/admin/").status_code, 302, "depois de restaurar, entra de novo")
        a2 = self.admin()
        self.assertEqual(a2.get("/admin/").status_code, 200)
        c = self.cadastrar("depois@teste.com")  # ids continuam funcionando depois da restauração
        self.adicionar(c, 2, 1)
        self.assertIn("/pedido/", self.fechar(c, "pix").headers["Location"])

    def test_restaurar_exige_confirmacao_senha_e_arquivo_valido(self):
        a = self.admin()
        arquivo = self.baixar(a)
        self.ajustar_produto(1, name="Não pode voltar")
        for r in (self.restaurar(a, arquivo, confirmacao="sim"), self.restaurar(a, arquivo, senha="errada"),
                  self.restaurar(a, b"lixo"), self.restaurar(a, gzip.compress(b'{"formato": "outro"}'))):
            self.assertTrue(r.headers["Location"].endswith("/admin/backup"))
        html = a.get("/admin/backup").get_data(as_text=True)
        self.assertIn("não é um backup da loja", html)
        with app.app_context():
            self.assertEqual(db.session.get(Product, 1).name, "Não pode voltar")

    def test_backup_sem_admin_e_recusado(self):
        a = self.admin()
        conteudo = json.loads(gzip.decompress(self.baixar(a)))
        conteudo["tabelas"]["user"] = [u for u in conteudo["tabelas"]["user"] if not u["is_admin"]]
        self.restaurar(a, gzip.compress(json.dumps(conteudo).encode()))
        self.assertIn("nenhuma conta de administrador", a.get("/admin/backup").get_data(as_text=True))

    def test_backup_antigo_sem_coluna_nova(self):
        a = self.admin()
        conteudo = json.loads(gzip.decompress(self.baixar(a)))
        for p in conteudo["tabelas"]["product"]:
            p.pop("peso_kg")  # como se o backup fosse de antes do frete
        self.restaurar(a, gzip.compress(json.dumps(conteudo).encode()))
        with app.app_context():
            self.assertEqual(Product.query.count(), 18)
            self.assertEqual(db.session.get(Product, 1).peso_kg, 0)

    def test_backup_maior_que_o_limite_do_site(self):
        """Backup acima do limite normal (40 MB em produção) é aceito na restauração; o resto do site não muda."""
        self.foto(3)
        a = self.admin()
        arquivo = self.baixar(a)
        self.ajustar_produto(1, name="Mudado")
        limite = len(arquivo) // 2  # simula o limite do site menor que o backup
        grande = b"x" * (limite * 3)
        with mock.patch.dict(app.config, MAX_CONTENT_LENGTH=limite):
            r = self.restaurar(a, arquivo)
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r.headers["Location"].endswith("/admin/login"), "restaurou")
            a2 = self.admin()
            # outra rota do painel continua com o limite normal
            r = a2.post("/admin/backup/baixar", data={"_csrf": self.token(a2, "/admin/backup"), "x": grande})
            self.assertEqual(r.status_code, 413)
            # sem login de admin, a rota de restaurar também fica no limite normal
            anonimo = app.test_client()
            r = anonimo.post("/admin/backup/restaurar", data={"arquivo": (io.BytesIO(grande), "b.gz")},
                             content_type="multipart/form-data")
            self.assertEqual(r.status_code, 413)
        with app.app_context():
            self.assertNotEqual(db.session.get(Product, 1).name, "Mudado")

    def test_backup_baixado_sai_cifrado_e_volta_com_a_senha(self):
        a = self.admin()
        arquivo = self.baixar(a, cifrado=True)
        self.assertNotIn(b"admin@exemplo.com", arquivo, "sem a senha, nada dos dados aparece no arquivo")
        self.ajustar_produto(1, name="Mudado")
        dados = {"_csrf": self.token(a, "/admin/backup"), "senha": ADMIN_SENHA, "confirmacao": "RESTAURAR",
                 "arquivo": (io.BytesIO(arquivo), "b.json.gz.cifrado")}
        r = a.post("/admin/backup/restaurar", data=dict(dados), content_type="multipart/form-data")
        self.assertIn("protegido por senha", a.get(r.headers["Location"]).get_data(as_text=True))
        dados.update(senha_backup="senha-errada-000", arquivo=(io.BytesIO(arquivo), "b.json.gz.cifrado"))
        r = a.post("/admin/backup/restaurar", data=dict(dados), content_type="multipart/form-data")
        self.assertIn("Senha do backup errada", a.get(r.headers["Location"]).get_data(as_text=True))
        dados.update(senha_backup=SENHA_ARQ, arquivo=(io.BytesIO(arquivo), "b.json.gz.cifrado"),
                     _csrf=self.token(a, "/admin/backup"))
        r = a.post("/admin/backup/restaurar", data=dados, content_type="multipart/form-data")
        self.assertTrue(r.headers["Location"].endswith("/admin/login"))
        with app.app_context():
            self.assertNotEqual(db.session.get(Product, 1).name, "Mudado")

    def test_senha_do_arquivo_curta_ou_diferente_nao_baixa(self):
        a = self.admin()
        for s1, s2 in (("curta", "curta"), ("senha-boa-1234", "senha-boa-9999")):
            r = self.post(a, "/admin/backup/baixar", {"senha_arquivo": s1, "senha_arquivo2": s2}, "/admin/backup")
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r.headers["Location"].endswith("/admin/backup"))
        self.assertIn("baixou um backup", a.get("/admin/").get_data(as_text=True), "nada foi baixado")

    def test_com_backup_senha_no_render_usa_ela(self):
        a = self.admin()
        self.assertIn("senha_arquivo", a.get("/admin/backup").get_data(as_text=True))
        with mock.patch.dict(app.config, BACKUP_SENHA="senha-do-render-77"):
            self.assertNotIn('name="senha_arquivo"', a.get("/admin/backup").get_data(as_text=True))
            r = self.post(a, "/admin/backup/baixar", {}, "/admin/backup")
            conteudo = backup.ler(r.data, "senha-do-render-77")
        self.assertEqual(conteudo["formato"], "marketplace-backup")

    def test_conexao_parada_e_testada_antes_de_usar(self):
        """Neon fecha conexões quando dorme: o site testa cada uma antes de usar (sem erro na primeira visita)."""
        with app.app_context():
            self.assertTrue(db.engine.pool._pre_ping)

    def test_lembrete_e_acesso(self):
        a = self.admin()
        self.assertIn("baixou um backup", a.get("/admin/").get_data(as_text=True))
        self.baixar(a)
        self.assertNotIn("baixou um backup", a.get("/admin/").get_data(as_text=True))
        self.assertIn("Último backup baixado", a.get("/admin/backup").get_data(as_text=True))
        c = self.cadastrar()
        self.assertEqual(c.get("/admin/backup").status_code, 302)
        self.assertEqual(self.post(c, "/admin/backup/baixar", {}).status_code, 302)


if __name__ == "__main__":
    unittest.main()
