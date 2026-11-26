"""Testes da consulta de CNPJ na Receita (BrasilAPI). A rede é simulada: nenhum teste acessa a internet."""
import io
import json
import unittest
import urllib.error
from unittest import mock

from test_site import SENHA, BaseTeste, novo_cnpj

from app import app  # noqa: E402
from models import User, db  # noqa: E402
import receita  # noqa: E402

ATIVA = {"razao_social": "METALURGICA OFICIAL LTDA", "nome_fantasia": "METAL OK", "descricao_situacao_cadastral": "ATIVA",
         "data_inicio_atividade": "2010-05-20", "cnae_fiscal": 2539001, "cnae_fiscal_descricao": "Usinagem",
         "descricao_tipo_de_logradouro": "RUA", "logradouro": "DAS INDUSTRIAS 100", "numero": "100",
         "complemento": "", "bairro": "CENTRO", "municipio": "SAO PAULO", "uf": "SP", "cep": "01001000"}


def resposta(dados):
    r = io.BytesIO(json.dumps(dados).encode())
    r.__enter__ = lambda s=r: s
    r.__exit__ = lambda *a: None
    return r


def erro_http(codigo):
    return urllib.error.HTTPError("url", codigo, "erro", {}, io.BytesIO(b"{}"))


class BaseReceita(BaseTeste):
    def setUp(self):
        super().setUp()
        app.config["CONSULTA_CNPJ"] = True
        receita.limpar_cache()
        self.addCleanup(app.config.update, CONSULTA_CNPJ=False)

    def api(self, efeito):
        p = mock.patch("receita.urllib.request.urlopen", side_effect=efeito)
        self.addCleanup(p.stop)
        return p.start()

    def cadastrar_pj(self, cnpj, razao="Digitada à mão"):
        c = self.cliente()
        return c, self.post(c, "/cadastro", {"nome": "Ana", "email": "ana@ex.com", "senha": SENHA, "senha2": SENHA, "aceite": "1",
                                             "tipo": "pj", "documento": cnpj, "razao_social": razao})

    def usuario(self):
        with app.app_context():
            return User.query.filter_by(email="ana@ex.com").first()


class TesteReceita(BaseReceita):
    def test_resumo_do_endereco(self):
        r = receita.resumir(ATIVA)
        self.assertEqual(r["endereco"], "RUA DAS INDUSTRIAS, 100, CENTRO, SAO PAULO/SP, CEP 01001-000")
        self.assertEqual((r["situacao"], r["cnae"]), ("ATIVA", "2539001 - Usinagem"))

    def test_cnpj_ativo_usa_a_razao_social_oficial(self):
        self.api(lambda *a, **k: resposta(ATIVA))
        _, r = self.cadastrar_pj(novo_cnpj())
        self.assertEqual(r.status_code, 302)
        u = self.usuario()
        self.assertEqual((u.razao_social, u.receita_situacao), ("METALURGICA OFICIAL LTDA", "ATIVA"))
        self.assertEqual(u.receita["nome_fantasia"], "METAL OK")
        self.assertIn("RUA DAS INDUSTRIAS", u.address)

    def test_cnpj_baixado_ou_inexistente_e_recusado(self):
        self.api(lambda *a, **k: resposta({**ATIVA, "descricao_situacao_cadastral": "BAIXADA"}))
        _, r = self.cadastrar_pj(novo_cnpj())
        self.assertIn("situação BAIXADA", r.get_data(as_text=True))
        self.api(erro_http(404))
        _, r = self.cadastrar_pj(novo_cnpj())
        self.assertIn("Não encontramos este CNPJ", r.get_data(as_text=True))
        self.assertIsNone(self.usuario())

    def test_api_fora_do_ar_nao_trava_o_cadastro(self):
        for falha in (erro_http(503), TimeoutError("lento"), OSError("sem rede")):
            receita.limpar_cache()
            self.api(falha)
            _, r = self.cadastrar_pj(novo_cnpj())
            self.assertEqual(r.status_code, 302)
            u = self.usuario()
            self.assertEqual((u.razao_social, u.receita_situacao), ("Digitada à mão", ""))
            with app.app_context():
                User.query.filter_by(email="ana@ex.com").delete()
                db.session.commit()

    def test_preenchimento_automatico_do_formulario(self):
        chamadas = self.api(lambda *a, **k: resposta(ATIVA))
        c, cnpj = self.cliente(), novo_cnpj()
        d = self.post(c, "/cadastro/cnpj", {"documento": cnpj}).get_json()
        self.assertTrue(d["ok"] and d["verificado"])
        self.assertEqual(d["razao_social"], "METALURGICA OFICIAL LTDA")
        self.post(c, "/cadastro/cnpj", {"documento": cnpj})
        self.assertEqual(chamadas.call_count, 1, "o mesmo CNPJ fica guardado por 1 hora")
        self.assertFalse(self.post(c, "/cadastro/cnpj", {"documento": "123"}).get_json()["ok"])

    def test_limite_de_consultas_por_ip(self):
        self.api(lambda *a, **k: resposta(ATIVA))
        c = self.cliente()
        codigos = [self.post(c, "/cadastro/cnpj", {"documento": novo_cnpj()}).status_code for _ in range(31)]
        self.assertEqual(codigos[-1], 429)

    def test_admin_consulta_de_novo(self):
        self.api(OSError("sem rede"))
        self.cadastrar_pj(novo_cnpj())
        uid = self.usuario().id
        a = self.admin()
        self.assertIn("Não verificado", a.get(f"/admin/clientes/{uid}").get_data(as_text=True))
        self.api(lambda *a, **k: resposta(ATIVA))
        self.post(a, f"/admin/clientes/{uid}/receita", {}, f"/admin/clientes/{uid}")
        html = a.get(f"/admin/clientes/{uid}").get_data(as_text=True)
        self.assertIn("METALURGICA OFICIAL LTDA", html)
        self.assertEqual(self.usuario().receita_situacao, "ATIVA")

    def test_cpf_nao_consulta_a_api(self):
        chamadas = self.api(lambda *a, **k: resposta(ATIVA))
        self.cadastrar()
        self.assertEqual(chamadas.call_count, 0)


if __name__ == "__main__":
    unittest.main()
