"""Testes da segunda revisão de segurança (sessão presa à senha, 2FA por conta, limite do comprador, webhook da
Efí, links, XML de nota de compra, cabeçalhos e limites de uso)."""
import json
import re
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from itsdangerous import URLSafeTimedSerializer

from test_b2b import BaseB2B
from test_site import ADMIN_SENHA, SENHA

from app import app  # noqa: E402
import dois_fatores  # noqa: E402
from models import Banner, Order, User, db  # noqa: E402
from security import limpar_limites  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent


class TesteSessaoPresaASenha(BaseB2B):
    def test_trocar_senha_derruba_as_outras_sessoes_do_cliente(self):
        self.cadastrar()
        celular, _ = self.entrar()
        computador, _ = self.entrar()
        self.assertEqual(celular.get("/conta").status_code, 200)
        self.post(computador, "/conta/senha", {"atual": SENHA, "nova": "outra-senha-456", "nova2": "outra-senha-456"},
                  "/conta/dados")
        self.assertEqual(computador.get("/conta").status_code, 200, "quem trocou continua na conta")
        r = celular.get("/conta")
        self.assertIn("/entrar", r.headers["Location"], "o outro aparelho precisa entrar de novo")
        self.assertIn("/entrar", celular.get("/carrinho").headers["Location"])

    def test_link_de_nova_senha_derruba_sessoes_e_nao_revela_o_hash(self):
        self.cadastrar()
        aberto, _ = self.entrar()
        limpar_limites()  # o e-mail de confirmação do cadastro acabou de sair (1 por minuto por endereço)
        self.post(self.cliente(), "/esqueci-senha", {"email": "cli@teste.com"}, "/esqueci-senha")
        self.assertIn("Redefinir", self.emails[-1]["assunto"])
        link = self.link_do_ultimo_email()
        token = link.rsplit("/", 1)[-1]
        dados = URLSafeTimedSerializer("x").loads_unsafe(token)[1]  # o conteúdo do token é legível por qualquer um
        with app.app_context():
            hash_atual = User.query.filter_by(email="cli@teste.com").one().password_hash
        self.assertNotIn(dados["h"], hash_atual, "o link não leva pedaço do hash da senha")
        c = self.cliente()
        self.post(c, link, {"nova": "senha-nova-789", "nova2": "senha-nova-789"}, link)
        self.assertIn("/entrar", aberto.get("/conta").headers["Location"])
        self.assertEqual(self.entrar(senha="senha-nova-789")[1].status_code, 302)
        r = c.get(link)  # o link vale uma vez só
        self.assertIn("/esqueci-senha", r.headers["Location"])

    def test_trocar_senha_derruba_as_outras_sessoes_do_painel(self):
        a1, a2 = self.admin(), self.admin()
        self.post(a1, "/admin/conta", {"acao": "senha", "atual": ADMIN_SENHA, "nova": "admin-nova-senha-9",
                                       "nova2": "admin-nova-senha-9"}, "/admin/conta")
        self.assertEqual(a1.get("/admin/conta").status_code, 200)
        self.assertIn("/admin/login", a2.get("/admin/").headers["Location"])

    def test_sessao_antiga_sem_marca_nao_vale(self):
        with app.app_context():
            uid = User.query.filter_by(email="admin@exemplo.com").one().id
        c = self.cliente()
        with c.session_transaction() as s:  # cookie forjado/antigo: só o id, sem a marca da senha
            s["admin_id"] = uid
        self.assertIn("/admin/login", c.get("/admin/").headers["Location"])


class TesteSessaoDoPainelExpira(BaseB2B):
    """O painel sai sozinho: 2 h sem usar (ADMIN_INATIVO_MIN) ou 12 h depois do login (ADMIN_SESSAO_MAX_H)."""
    def relogio(self, minutos):
        base = time.time()
        return mock.patch("security.time.time", return_value=base + minutos * 60)

    def test_painel_parado_por_2_horas_sai_sozinho(self):
        a = self.admin()
        with self.relogio(119):
            self.assertEqual(a.get("/admin/").status_code, 200)
        with self.relogio(119 + 121):
            r = a.get("/admin/")
            self.assertIn("/admin/login", r.headers["Location"])
            self.assertIn("expirou", a.get(r.headers["Location"]).get_data(as_text=True))

    def test_usar_o_painel_mantem_ate_o_maximo_de_12_horas(self):
        a = self.admin()
        for hora in range(1, 12):  # usa a cada hora: nunca fica 2 h parado
            with self.relogio(hora * 60):
                self.assertEqual(a.get("/admin/").status_code, 200, f"hora {hora}")
        with self.relogio(12 * 60 + 1):
            self.assertIn("/admin/login", a.get("/admin/").headers["Location"])

    def test_navegar_na_loja_nao_conta_como_uso_do_painel(self):
        a = self.admin()
        with self.relogio(100):
            a.get("/")
        with self.relogio(125):
            self.assertIn("/admin/login", a.get("/admin/").headers["Location"])

    def test_sessao_aberta_antes_da_regra_comeca_a_contar_agora(self):
        a = self.admin()
        with a.session_transaction() as s:
            s.pop("admin_desde"), s.pop("admin_visto")
        self.assertEqual(a.get("/admin/").status_code, 200)
        with self.relogio(60):
            self.assertEqual(a.get("/admin/").status_code, 200)

    def test_tempo_vem_do_render(self):
        a = self.admin()
        with mock.patch.dict(app.config, ADMIN_INATIVO_MIN=10), self.relogio(11):
            self.assertIn("/admin/login", a.get("/admin/").headers["Location"])


class TesteSaudeParaOMonitor(BaseB2B):
    """/saude: o UptimeRobot visita a cada 5 min para o site não dormir; não pode tocar no banco (o Neon dorme)."""
    def test_saude_responde_sem_usar_o_banco(self):
        from sqlalchemy import event
        consultas = []
        with app.app_context():
            motor = db.engine
        ouvir = lambda *a, **k: consultas.append(1)  # noqa: E731
        event.listen(motor, "before_cursor_execute", ouvir)
        try:
            c = app.test_client()
            r = c.get("/saude")
            h = c.head("/saude")
        finally:
            event.remove(motor, "before_cursor_execute", ouvir)
        self.assertEqual((r.status_code, r.get_data(as_text=True)), (200, "ok"))
        self.assertEqual(h.status_code, 200)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(consultas, [], "a página do monitor não pode acordar o banco")


class TesteDuasEtapasPorConta(BaseB2B):
    def setUp(self):
        super().setUp()
        self.addCleanup(app.config.update, ADMIN_2FA_DESLIGADO=False)

    def test_codigo_do_app_tem_limite_por_conta_mesmo_trocando_de_ip(self):
        a = self.admin()
        self.post(a, "/admin/conta/duas-etapas", {"acao": "iniciar", "atual": ADMIN_SENHA}, "/admin/conta")
        with a.session_transaction() as s:
            segredo = s["2fa_novo"]
        self.post(a, "/admin/conta/duas-etapas",
                  {"acao": "confirmar", "codigo": dois_fatores.codigo(segredo, int(time.time() // 30))}, "/admin/conta")

        def tentar(ip, codigo):
            c = self.cliente()
            c.environ_base["REMOTE_ADDR"] = ip
            r = self.post(c, "/admin/login", {"email": "admin@exemplo.com", "password": ADMIN_SENHA}, "/admin/login")
            self.assertIn("/admin/login/codigo", r.headers["Location"])
            r = self.post(c, "/admin/login/codigo", {"codigo": codigo}, "/admin/login/codigo")
            return c, r

        for n in range(10):  # cada IP erra só uma vez: o bloqueio por IP nunca dispara
            tentar(f"10.0.0.{n + 1}", "000000")
        c, r = tentar("10.0.1.1", dois_fatores.codigo(segredo, int(time.time() // 30) + 1))
        self.assertIn("/admin/login", r.headers["Location"])
        self.assertNotIn("/admin/login/codigo", r.headers["Location"])
        self.assertIn("Muitas tentativas de código", c.get("/admin/login").get_data(as_text=True))
        self.assertIn("/admin/login", c.get("/admin/").headers["Location"], "nem o código certo entra agora")


class TesteCompradorNaoFuraOLimite(BaseB2B):
    def test_comprador_nao_aumenta_pedido_acima_do_limite(self):
        self.ajustar_produto(1, qtd_minima=1, multiplo=1, stock=100, price=Decimal("100.00"))
        titular = self.empresa(desconto="0")
        self.post(titular, "/conta/equipe", {"acao": "convidar", "nome": "Carlos", "email": "carlos@empresa.com",
                                             "papel": "comprador", "limite": "500"}, "/conta/equipe")
        link = next(re.search(r"https?://\S+", e["texto"]).group(0) for e in reversed(self.emails)
                    if e["para"] == "carlos@empresa.com").split("localhost", 1)[-1]
        carlos = self.cliente()
        self.post(carlos, link, {"nova": "senha-carlos-1", "nova2": "senha-carlos-1"}, link)
        self.post(carlos, "/entrar", {"email": "carlos@empresa.com", "senha": "senha-carlos-1"}, "/entrar")
        self.adicionar(carlos, 1, 3)
        self.fechar(carlos, "pix")
        oid, status, total, *_ = self.ultimo_pedido()
        self.assertEqual(status, "pendente", "abaixo do limite: vai direto para a loja")
        pagina = f"/conta/pedidos/{oid}"
        self.post(carlos, f"/conta/pedidos/{oid}/alterar", {"qtd_1": "40"}, pagina)
        with app.app_context():
            o = db.session.get(Order, oid)
            self.assertEqual((o.total, sum(i.quantity for i in o.items)), (total, 3), "nada mudou")
        self.assertIn("passaria do seu limite", carlos.get(pagina).get_data(as_text=True))
        self.assertEqual(self.estoque(1), 97, "o estoque não foi mexido")
        # diminuir continua livre, e o titular pode aumentar
        self.post(carlos, f"/conta/pedidos/{oid}/alterar", {"qtd_1": "2"}, pagina)
        self.post(titular, f"/conta/pedidos/{oid}/alterar", {"qtd_1": "10"}, pagina)
        with app.app_context():
            self.assertEqual(sum(i.quantity for i in db.session.get(Order, oid).items), 10)


class TesteAvisoDaEfi(BaseB2B):
    def test_token_estranho_nao_vira_caminho_na_api(self):
        c = self.cliente()
        with mock.patch("boleto_efi._chamar", return_value={"data": []}) as chamar:
            for token in ("../charge/1", "abc?x=1", "a/b", "a%2Fb", "a" * 201):
                self.assertEqual(c.post("/efi/boleto", data={"notification": token}).status_code, 200)
            chamar.assert_not_called()
            c.post("/efi/boleto", data={"notification": "e5f2-ab_12"})
            chamar.assert_called_once_with("GET", "/v1/notification/e5f2-ab_12")

    def test_resposta_fora_do_formato_nao_derruba(self):
        with mock.patch("boleto_efi._chamar", return_value={"data": {"status": "x"}}):
            r = self.cliente().post("/efi/boleto", data={"notification": "abc"})
        self.assertEqual(r.status_code, 200)

    def test_avisos_em_excesso_tem_limite(self):
        c = self.cliente()
        with mock.patch("boleto_efi._chamar", return_value={"data": []}) as chamar:
            codigos = [c.post("/efi/boleto", data={"notification": f"t{n}"}).status_code for n in range(121)]
        self.assertEqual(codigos[:120], [200] * 120)
        self.assertEqual(codigos[120], 429, "a Efí reenvia depois")
        self.assertEqual(chamar.call_count, 120)


class TesteEntradasDoPainel(BaseB2B):
    def test_link_do_banner_com_tabulacao_e_recusado(self):
        a = self.admin()
        for link in ("/\t/evil.com", "/\n/evil.com", "/ /evil.com"):
            r = self.post(a, "/admin/banners/novo", {"titulo": "Promo", "link": link, "ativo": "on"},
                          "/admin/banners/novo")
            self.assertIn("O link do clique precisa", r.get_data(as_text=True))
        with app.app_context():
            self.assertEqual(Banner.query.count(), 0)

    def test_xml_de_nota_com_doctype_e_recusado(self):
        import notas_compra
        xml = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
               '<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe1">&b;</infNFe></NFe></nfeProc>')
        with app.app_context():
            with self.assertRaises(notas_compra.ErroNota) as erro:
                notas_compra.ler_xml(xml)
        self.assertIn("DOCTYPE", str(erro.exception))

    def test_atendente_so_cria_link_da_propria_loja(self):
        js = (RAIZ / "static" / "atendente.js").read_text(encoding="utf-8")
        trecho = re.search(r"\.replace\(/\\\[\(\[\^\\\]\\n\]\+\)\\\]\\\((.+?)\\\)/g", js).group(1)
        padrao = re.compile(trecho.replace("\\/", "/"))
        self.assertTrue(padrao.fullmatch("/produto/1-fresa"))
        self.assertTrue(padrao.fullmatch("https://loja-exemplo.com.br/x"))
        for ruim in ("//evil.com", "/\\evil.com", "javascript:alert(1)"):
            self.assertIsNone(padrao.fullmatch(ruim), ruim)


class TesteCabecalhosELimites(BaseB2B):
    def test_paginas_da_conta_e_do_painel_nao_ficam_no_cache(self):
        self.cadastrar()
        c, _ = self.entrar()
        for caminho in ("/conta", "/conta/dados", "/carrinho"):
            self.assertEqual(c.get(caminho).headers.get("Cache-Control"), "no-store", caminho)
        self.assertEqual(self.admin().get("/admin/").headers.get("Cache-Control"), "no-store")
        self.assertNotEqual(c.get("/").headers.get("Cache-Control"), "no-store", "a vitrine pode ficar no cache")

    def test_hsts_so_no_ar_com_https(self):
        self.assertNotIn("Strict-Transport-Security", self.cliente().get("/").headers)
        app.config["PRODUCAO"] = True
        self.addCleanup(app.config.update, PRODUCAO=False)
        r = self.cliente().get("/", base_url="https://localhost")
        self.assertEqual(r.headers.get("Strict-Transport-Security"), "max-age=31536000")

    def test_cadastro_tem_limite_por_ip(self):
        c = self.cliente()
        token = self.token(c, "/cadastro")
        codigos = [c.post("/cadastro", data={"_csrf": token, "nome": "", "email": "x"}).status_code for _ in range(16)]
        self.assertEqual(codigos[:15], [200] * 15)
        self.assertEqual(codigos[15], 429)

    def test_sql_de_exemplo_nao_cria_admin_com_senha_publica(self):
        sql = (RAIZ / "exemplo.sql").read_text(encoding="utf-8")
        self.assertNotIn("INSERT INTO `user`", sql)
        self.assertNotIn("admin123", sql)


class TesteSemScriptDentroDaPagina(unittest.TestCase):
    """A CSP não aceita script escrito dentro da página: um código injetado não roda."""

    def test_csp_bloqueia_script_dentro_da_pagina(self):
        import security
        script = [p for p in security.CSP.split(";") if p.strip().startswith("script-src ")][0]
        self.assertNotIn("unsafe-inline", script)
        self.assertNotIn("unsafe-eval", script)
        self.assertNotIn("https://cdn.jsdelivr.net;", security.CSP, "o CDN só para a biblioteca do gráfico")
        self.assertIn("script-src-attr 'none'", security.CSP)

    def test_paginas_nao_tem_script_nem_onclick_escrito_dentro(self):
        import pathlib
        import re
        pasta = pathlib.Path(__file__).resolve().parent.parent / "templates"
        problemas = []
        for p in pasta.rglob("*.html"):
            t = p.read_text(encoding="utf-8")
            for m in re.finditer(r"<script\b([^>]*)>", t):
                if "src=" not in m.group(1) and "application/ld+json" not in m.group(1):
                    problemas.append(f"{p.name}: <script> sem src")
            for m in re.finditer(r"[\s\"'}]on[a-z]+\s*=\s*[\"']", t):
                problemas.append(f"{p.name}: {m.group(0).strip()}")
            if "javascript:" in t:
                problemas.append(f"{p.name}: javascript:")
        self.assertEqual(problemas, [], "use static/acoes.js (data-confirmar, data-imprimir...) no lugar")


class TesteArquivosGuardadosNoNavegador(BaseB2B):
    """CSS e JS com ?v= (muda quando o arquivo muda) e guardados por 1 ano: as páginas abrem mais rápido."""

    def test_css_e_js_tem_versao_e_ficam_guardados(self):
        from app import app as aplicacao
        html = aplicacao.test_client().get("/").get_data(as_text=True)
        enderecos = re.findall(r'(?:href|src)="(/static/[^"]+)"', html)
        self.assertTrue(enderecos)
        for e in enderecos:
            self.assertRegex(e, r"\?v=[0-9a-f]{10}$", e)
        r = aplicacao.test_client().get(enderecos[0])
        self.assertEqual(r.status_code, 200)
        self.assertIn("max-age=31536000", r.headers["Cache-Control"])
        r.close()

    def test_versao_muda_quando_o_arquivo_muda(self):
        import hashlib
        import pathlib
        from app import app as aplicacao
        with aplicacao.test_request_context():
            from flask import url_for
            v = url_for("static", filename="acoes.js").split("v=")[1]
        conteudo = (pathlib.Path(aplicacao.static_folder) / "acoes.js").read_bytes()
        self.assertEqual(v, hashlib.sha256(conteudo).hexdigest()[:10])


if __name__ == "__main__":
    unittest.main()
