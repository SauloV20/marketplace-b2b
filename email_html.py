"""Versão bonita (HTML) dos e-mails, montada a partir do texto simples.

O texto continua sendo enviado junto (quem não vê HTML lê o texto). Regras da conversão:
- linha "Algo: https://..." vira botão (o primeiro é o principal, azul);
- linhas que começam com "- " viram lista; "Total: ..." fica em negrito;
- outros links viram clicáveis.

Segurança: só viram link (ou botão) os endereços do próprio site, do WhatsApp e do rastreio, e os que o sistema
põe sozinhos numa linha (boleto) ou com o rótulo do rastreio. Um endereço digitado pelo cliente no nome ou no
endereço de entrega aparece como texto comum: não vira um botão "oficial" no e-mail que a loja recebe."""
import re
from html import escape

from flask import current_app

URL = re.compile(r"https?://[^\s<>\"]+")
BOTAO = re.compile(r"^([^:\n]{2,60}):\s*(https?://\S+)$")

AZUL, MARINHO, CINZA = "#1E4FD8", "#0B2A6B", "#5B6475"
# o texto diz "Acompanhe em: link"; no botão fica melhor um verbo direto
ROTULOS = {"Acompanhe em": "Acompanhar pedido", "Veja como pagar": "Ver como pagar", "Detalhes": "Ver detalhes",
           "Veja como pagar em": "Ver como pagar", "Detalhes e documentos": "Ver detalhes e documentos"}


CONFIAVEIS = {"wa.me", "melhorrastreio.com.br", "www.melhorrastreio.com.br"}
ROTULOS_DO_SISTEMA = {"Acompanhe a entrega"}  # link digitado pelo administrador (rastreio da transportadora)


def _confiavel(url):
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    site = (urlsplit(current_app.config.get("SITE_URL") or "").hostname or "").lower()
    return bool(host) and (host == site or host in CONFIAVEIS or host in ("localhost", "127.0.0.1"))


def _linkar(linha, sozinha=False):
    """Escapa a linha e transforma em link os endereços confiáveis (ou o endereço que é a linha inteira)."""
    partes, fim = [], 0
    for m in URL.finditer(linha):
        partes.append(escape(linha[fim:m.start()]))
        u = m.group(0)
        if _confiavel(u) or (sozinha and u == linha.strip()):
            partes.append(f'<a href="{escape(u)}" style="color:{AZUL}">{escape(u)}</a>')
        else:
            partes.append(escape(u))
        fim = m.end()
    partes.append(escape(linha[fim:]))
    return "".join(partes)


def _botao(rotulo, url, principal):
    cor = f"background:{AZUL};color:#ffffff" if principal else f"background:#EAF0FF;color:{AZUL}"
    return (f'<p style="margin:18px 0"><a href="{escape(url)}" style="{cor};display:inline-block;padding:12px 22px;'
            f'border-radius:8px;font-weight:700;text-decoration:none">{escape(rotulo)}</a></p>')


def _bloco(linhas, ja_tem_botao):
    """Um parágrafo (linhas sem linha em branco no meio). Devolve (html, usou o botão principal?)."""
    if len(linhas) == 1 and (m := BOTAO.match(linhas[0].strip())) and (
            _confiavel(m.group(2)) or m.group(1).strip() in ROTULOS_DO_SISTEMA):
        rotulo = m.group(1).strip()
        return _botao(ROTULOS.get(rotulo, rotulo), m.group(2), not ja_tem_botao), True
    saida, texto, lista = [], [], []

    def fechar_texto():
        if texto:
            saida.append('<p style="margin:0 0 14px">' + "<br>".join(texto) + "</p>")
            texto.clear()

    def fechar_lista():
        if lista:
            saida.append('<ul style="margin:0 0 14px;padding-left:20px">' + "".join(lista) + "</ul>")
            lista.clear()

    for l in linhas:
        if l.startswith("- "):
            fechar_texto()
            lista.append(f'<li style="margin:4px 0">{_linkar(l[2:])}</li>')
        else:
            fechar_lista()
            texto.append(f"<b>{_linkar(l)}</b>" if l.startswith("Total:") else _linkar(l, sozinha=True))
    fechar_texto()
    fechar_lista()
    return "".join(saida), False


def montar(assunto, texto):
    blocos, atual = [], []
    for linha in texto.replace("\r\n", "\n").split("\n"):
        if linha.strip():
            atual.append(linha.rstrip())
        elif atual:
            blocos.append(atual)
            atual = []
    if atual:
        blocos.append(atual)
    partes, tem_botao = [], False
    for b in blocos:
        html, usou = _bloco(b, tem_botao)
        tem_botao = tem_botao or usou
        partes.append(html)
    whatsapp = current_app.config.get("WHATSAPP_NUMBER", "")
    contato = (f' · <a href="https://wa.me/{escape(whatsapp)}" style="color:{CINZA}">WhatsApp</a>' if whatsapp else "")
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(assunto)}</title></head>
<body style="margin:0;padding:0;background:#F1F4F9">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F1F4F9"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px">
<tr><td style="background:{MARINHO};border-radius:12px 12px 0 0;padding:18px 28px;font:800 24px Arial,sans-serif;color:#ffffff">Marketplace B2B
<span style="font:600 12px Arial,sans-serif;color:#B9C8EE;margin-left:6px">marketplace industrial</span></td></tr>
<tr><td style="background:#ffffff;border-radius:0 0 12px 12px;padding:28px;font:15px/1.6 Arial,sans-serif;color:#1B2333">
{"".join(partes)}</td></tr>
<tr><td style="padding:16px 8px;font:12px/1.5 Arial,sans-serif;color:{CINZA};text-align:center">
Marketplace B2B · ferramentas industriais com preço de atacado{contato}<br>
Você recebeu este e-mail por causa da sua conta ou de um pedido no Marketplace B2B.</td></tr>
</table></td></tr></table></body></html>"""
