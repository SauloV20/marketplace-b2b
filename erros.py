"""Páginas de erro com a cara da loja (em vez da página branca em inglês do Flask)."""
from flask import render_template, request, url_for
from werkzeug.exceptions import HTTPException

TEXTOS = {
    400: ("Não deu para entender o pedido", "Algo veio incompleto. Volte e tente de novo."),
    403: ("Acesso não permitido", "Você não tem permissão para ver esta página."),
    404: ("Página não encontrada", "O endereço pode ter mudado ou o produto saiu do catálogo. "
                                   "Busque o que precisa ou volte para o início."),
    405: ("Página não encontrada", "Este endereço não funciona assim. Volte para o início."),
    413: ("Arquivo grande demais", "Envie um arquivo menor e tente de novo."),
    429: ("Muitas tentativas seguidas", "Espere alguns minutos e tente de novo."),
    500: ("Algo deu errado do nosso lado", "Já fomos avisados e vamos resolver. Tente de novo em alguns minutos."),
}

SIMPLES = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{t} · Marketplace B2B</title></head><body style="font-family:system-ui,sans-serif;text-align:center;padding:64px 16px">
<h1>{t}</h1><p>{x}</p><p><a href="/">Voltar ao início</a></p></body></html>"""


def _pagina(codigo):
    titulo, texto = TEXTOS.get(codigo, TEXTOS[500] if codigo >= 500 else TEXTOS[400])
    painel = request.blueprint == "admin"
    try:
        if codigo >= 500:
            from models import db
            db.session.rollback()  # o erro pode ter deixado a conexão com o banco no meio de algo
        inicio = url_for("admin.hoje") if painel else "/"
        return render_template("erro.html", codigo=codigo, titulo=titulo, texto=texto, inicio=inicio,
                               com_cabecalho=not painel, nao_indexar=True), codigo
    except Exception:  # se até a página bonita falhar (ex.: banco fora do ar), mostra a simples
        return SIMPLES.format(t=titulo, x=texto), codigo


def ligar(app):
    @app.errorhandler(HTTPException)
    def _erro_http(e):  # inclui o 500: o Flask registra o erro no log (e o alerta por e-mail) antes de chegar aqui
        if e.code is None or e.code < 400:
            return e
        return _pagina(e.code)

