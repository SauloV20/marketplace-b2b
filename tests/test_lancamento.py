"""Pronto para lançar: dados da empresa no rodapé e lista correta de quem guarda os dados."""
import unittest
from pathlib import Path
from unittest import mock

from test_b2b import BaseB2B

from app import app  # noqa: E402
from b2b import salvar_config  # noqa: E402
import lgpd  # noqa: E402
from models import db  # noqa: E402


class TesteProntoParaLancar(BaseB2B):
    def test_rodape_sem_dados_da_empresa_nao_mostra_linha_vazia(self):
        self.assertNotIn("rodape-empresa", self.cliente().get("/").get_data(as_text=True))

    def test_rodape_mostra_dados_da_empresa(self):
        with app.app_context():
            for k, v in {"empresa_razao_social": "Marketplace B2B Ferramentas Ltda", "empresa_cnpj": "11222333000181",
                         "empresa_endereco": "Rua Industrial, 10 - São Paulo/SP",
                         "empresa_email_privacidade": "privacidade@loja-exemplo.com.br"}.items():
                salvar_config(k, v)
            db.session.commit()
        for caminho in ("/", "/termos", "/cadastro"):
            html = self.cliente().get(caminho).get_data(as_text=True)
            self.assertIn("Marketplace B2B Ferramentas Ltda · CNPJ 11.222.333/0001-81 · Rua Industrial, 10", html, caminho)
            self.assertIn("mailto:privacidade@loja-exemplo.com.br", html)

    def test_rodape_tem_whatsapp_quando_configurado(self):
        antes = app.config.get("WHATSAPP_NUMBER")
        app.config["WHATSAPP_NUMBER"] = "5511999998888"
        try:
            self.assertIn('href="https://wa.me/5511999998888"', self.cliente().get("/").get_data(as_text=True))
        finally:
            app.config["WHATSAPP_NUMBER"] = antes

    def test_privacidade_diz_onde_fica_o_banco(self):
        url = "postgresql+psycopg2://u:p@ep-x.us-east-2.aws.neon.tech/neondb"
        with app.test_request_context(), mock.patch.dict(app.config, {"SQLALCHEMY_DATABASE_URI": url}):
            nomes = dict(lgpd.operadores())
        self.assertEqual(nomes["Render"], "hospedagem do site")
        self.assertEqual(nomes["Neon"], "hospedagem do banco de dados")
        with app.test_request_context(), mock.patch.dict(app.config, {
                "SQLALCHEMY_DATABASE_URI": "postgresql+psycopg2://u:p@dpg-x.ohio-postgres.render.com/db"}):
            self.assertEqual(dict(lgpd.operadores())["Render"], "hospedagem do site e do banco de dados")


CSS = Path(__file__).resolve().parent.parent / "static" / "style.css"


class TesteNoCelular(BaseB2B):
    def css_celular(self):
        css = CSS.read_text(encoding="utf-8")
        return css[css.index("@media(max-width:640px){\n  .wrap"):]

    def test_busca_nao_da_zoom_no_iphone(self):
        self.assertIn(".busca input{font-size:16px}", self.css_celular().replace("input,select,textarea,", ""))

    def test_menu_da_conta_vira_uma_linha_que_rola(self):
        html = self.empresa(aprovar=False).get("/conta").get_data(as_text=True)
        self.assertIn('class="tabs conta-nav"', html)
        self.assertIn(".conta-nav{flex-wrap:nowrap;overflow-x:auto", self.css_celular())

    def test_resumo_do_carrinho_vem_antes_do_adicionar_por_codigo(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".cart-layout>.por-codigo{order:1}", css)

    def test_barra_de_baixo_com_conta_e_carrinho(self):
        c = self.cliente()
        html = c.get("/").get_data(as_text=True)
        self.assertIn('class="barra-baixo"', html)
        self.assertIn(">Entrar</a>", html, "sem login: atalho leva ao login")
        c = self.empresa(aprovar=False)
        self.adicionar(c, 1, 1)
        html = c.get("/carrinho").get_data(as_text=True)
        self.assertRegex(html, r'data-cart-badge\s*>1<')
        self.assertIn(">Conta</a>", html)

    def test_painel_tem_botao_de_menu_no_celular(self):
        html = self.admin().get("/admin/orders").get_data(as_text=True)
        self.assertIn('data-menu-painel', html)
        self.assertIn(".side>a,.side>form,.side>.side-grupo{display:none}",
                      CSS.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
