"""Envio de e-mails: Resend ou Brevo (API HTTPS), SMTP (ex.: Gmail) ou, sem configuração, mostra no terminal.

No plano grátis do Render as portas de SMTP (25, 465, 587) são bloqueadas: lá use Resend ou Brevo."""
import base64
import json
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import parseaddr

from flask import current_app, request


def link_externo(caminho):
    """Monta o link do e-mail a partir de SITE_URL, nunca do cabeçalho Host enviado pelo visitante."""
    base = current_app.config["SITE_URL"] or request.url_root
    return base.rstrip("/") + caminho


def provedor_email():
    """Qual serviço envia os e-mails: "Resend", "SMTP (servidor)" ou "" se nenhum estiver configurado."""
    cfg = current_app.config
    if cfg["RESEND_API_KEY"]:
        return "Resend"
    if cfg.get("BREVO_API_KEY"):
        return "Brevo"
    return f"SMTP ({cfg['MAIL_SERVER']})" if cfg["MAIL_SERVER"] else ""


def enviar_email(para, assunto, texto, anexos=None):
    return enviar_email_detalhado(para, assunto, texto, anexos)[0]


MAX_ANEXOS = 9_000_000  # os serviços de e-mail recusam mensagens grandes (o Brevo aceita até uns 10 MB)


def enviar_email_detalhado(para, assunto, texto, anexos=None):
    """Envia e devolve (enviou?, motivo da falha em português claro).

    anexos: [(nome do arquivo, bytes, tipo MIME)]"""
    cfg = current_app.config
    anexos = list(anexos or [])
    if sum(len(a[1]) for a in anexos) > MAX_ANEXOS:
        return False, "Os anexos passam do tamanho que os serviços de e-mail aceitam (9 MB)."
    from email_html import montar
    html = montar(assunto, texto)
    caixa = cfg.get("EMAILS_ENVIADOS")  # usado pelos testes automáticos
    if caixa is not None:
        caixa.append({"para": para, "assunto": assunto, "texto": texto, "anexos": anexos, "html": html})
        return True, ""
    if not provedor_email():
        if cfg.get("PRODUCAO"):  # no ar, sem provedor ninguém recebe nada: é falha, não "enviado"
            current_app.logger.error("E-MAIL NÃO CONFIGURADO: o e-mail para %s não foi enviado (%s).", para, assunto)
            return False, ("Nenhum serviço de e-mail está configurado. Defina RESEND_API_KEY (Resend) ou "
                           "MAIL_SERVER, MAIL_USERNAME e MAIL_PASSWORD (SMTP) nas variáveis do Render.")
        current_app.logger.warning("E-MAIL NÃO CONFIGURADO. Para: %s | %s\n%s", para, assunto, texto)
        return True, ""
    try:
        if cfg["RESEND_API_KEY"]:
            dados = {"from": cfg["MAIL_FROM"], "to": [para], "subject": assunto, "text": texto, "html": html}
            if anexos:
                dados["attachments"] = [{"filename": n, "content": base64.b64encode(b).decode()} for n, b, _ in anexos]
            corpo = json.dumps(dados).encode()
            req = urllib.request.Request("https://api.resend.com/emails", data=corpo, headers={
                "Authorization": f"Bearer {cfg['RESEND_API_KEY']}", "Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=15).read()
            except urllib.error.HTTPError as e:  # a Resend explica o motivo no corpo (ex.: domínio não verificado)
                raise RuntimeError(f"Resend recusou ({e.code}): {e.read()[:300].decode('utf-8', 'replace')}") from e
        elif cfg.get("BREVO_API_KEY"):
            nome, remetente = parseaddr(cfg["MAIL_FROM"])
            dados = {"sender": {"name": nome or "Marketplace B2B", "email": remetente}, "to": [{"email": para}],
                     "subject": assunto, "textContent": texto, "htmlContent": html}
            if anexos:
                dados["attachment"] = [{"name": n, "content": base64.b64encode(b).decode()} for n, b, _ in anexos]
            corpo = json.dumps(dados).encode()
            req = urllib.request.Request("https://api.brevo.com/v3/smtp/email", data=corpo, headers={
                "api-key": cfg["BREVO_API_KEY"], "Content-Type": "application/json", "Accept": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=15).read()
            except urllib.error.HTTPError as e:  # ex.: remetente não verificado, chave errada
                raise RuntimeError(f"Brevo recusou ({e.code}): {e.read()[:300].decode('utf-8', 'replace')}") from e
        elif cfg["MAIL_SERVER"]:
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = cfg["MAIL_FROM"], para, assunto
            msg.set_content(texto)
            msg.add_alternative(html, subtype="html")
            for nome_arquivo, conteudo, mime in anexos:
                principal, _, sub = (mime or "application/octet-stream").partition("/")
                msg.add_attachment(conteudo, maintype=principal, subtype=sub or "octet-stream", filename=nome_arquivo)
            contexto = ssl.create_default_context()
            if cfg["MAIL_PORT"] == 465:
                smtp = smtplib.SMTP_SSL(cfg["MAIL_SERVER"], 465, timeout=15, context=contexto)
            else:
                smtp = smtplib.SMTP(cfg["MAIL_SERVER"], cfg["MAIL_PORT"], timeout=15)
                smtp.starttls(context=contexto)
            with smtp:
                if cfg["MAIL_USERNAME"]:
                    smtp.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
                smtp.send_message(msg)
        return True, ""
    except Exception as e:
        current_app.logger.exception("Falha ao enviar e-mail para %s", para)
        motivo = str(e)
        if provedor_email().startswith("SMTP") and isinstance(e, (TimeoutError, OSError)) \
                and not isinstance(e, smtplib.SMTPException):
            motivo = (f"Não foi possível conectar ao servidor SMTP ({e}). O plano grátis do Render bloqueia as "
                      "portas de SMTP (25, 465 e 587): use BREVO_API_KEY (API do Brevo) ou RESEND_API_KEY.")
        elif isinstance(e, smtplib.SMTPAuthenticationError):
            motivo = ("O servidor SMTP recusou usuário/senha. No Gmail, use uma \"senha de app\" "
                      "(Conta Google > Segurança > Senhas de app), não a senha normal.")
        return False, motivo[:400]
