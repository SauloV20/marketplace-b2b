"""Backup semanal automático: os dados da loja (sem fotos e anexos), protegidos por senha, por e-mail.

Quem dispara: o GitHub Actions toda segunda de manhã (.github/workflows/backup.yml chama /tarefas/backup-semanal
com o TAREFAS_TOKEN) e, por garantia, o próprio painel quando alguém o abre e o último envio tem mais de 7 dias.
Precisa de BACKUP_SENHA no Render: o arquivo tem dados pessoais e só vai cifrado.
"""
from datetime import timedelta

from flask import current_app

import backup
from b2b import config, data_config, salvar_config
from models import User, agora_utc, db

INTERVALO = timedelta(days=7)
ESPERA_FALHA = timedelta(hours=6)  # depois de uma falha, tenta de novo só depois disso


def destinatarios():
    """E-mails configurados no painel; sem eles, os administradores com acesso total."""
    lista = [e.strip() for e in config("backup_email", "").replace(";", ",").split(",") if "@" in e]
    if not lista:
        lista = [u.email for u in User.query.filter_by(is_admin=True).order_by(User.id).all()
                 if (u.permissoes or "tudo") == "tudo"][:1]
    return lista[:3]


def situacao():
    return {"senha": bool(current_app.config.get("BACKUP_SENHA")), "token": bool(current_app.config.get("TAREFAS_TOKEN")),
            "ultimo": data_config("backup_auto_em"), "erro": config("backup_auto_erro", ""), "para": destinatarios(),
            "configurado": config("backup_email", "")}


def devido():
    ultimo, tentativa = data_config("backup_auto_em"), data_config("backup_auto_tentativa")
    agora = agora_utc()
    if tentativa and agora - tentativa < ESPERA_FALHA and (not ultimo or tentativa > ultimo):
        return False
    return not ultimo or agora - ultimo >= INTERVALO


def enviar(forcar=False):
    """Gera e manda o backup. Devolve (enviou?, mensagem)."""
    if not forcar and not devido():
        return False, "O último backup automático foi enviado há menos de 7 dias."
    senha = current_app.config.get("BACKUP_SENHA", "")
    salvar_config("backup_auto_tentativa", agora_utc().isoformat(timespec="seconds"))
    db.session.commit()
    if not senha:
        return _falhou("Defina BACKUP_SENHA no Render: o backup só vai por e-mail protegido por senha.")
    para = destinatarios()
    if not para:
        return _falhou("Informe o e-mail que recebe o backup.")
    from email_utils import enviar_email_detalhado
    conteudo, resumo = backup.gerar(sem_arquivos=True)
    nome_cifrado = f"backup-marketplace-{agora_utc():%Y%m%d}-dados.json.gz.cifrado"
    # o Brevo só aceita anexos de tipos conhecidos (recusa ".cifrado"): vai dentro de um .zip; a senha continua valendo
    arquivo = backup.em_zip(nome_cifrado, backup.cifrar(conteudo, senha))
    nome = nome_cifrado.replace(".json.gz.cifrado", ".zip")
    texto = ("Backup semanal automático da loja Marketplace B2B.\n\n"
             f"Pedidos: {resumo.get('order', 0)} · clientes e contas: {resumo.get('user', 0)} · produtos: "
             f"{resumo.get('product', 0)} · notas fiscais: {resumo.get('nota_fiscal', 0)} · contas do financeiro: "
             f"{resumo.get('lancamento', 0)}.\n\n"
             "O arquivo está protegido pela senha de backup (BACKUP_SENHA, nas variáveis do Render). Guarde este "
             "e-mail. Para restaurar: Painel > Backup e espaço > Restaurar backup, com a senha do backup.\n"
             "Este backup não leva as fotos dos produtos nem os PDFs anexados aos pedidos (baixe o backup completo no "
             "painel de vez em quando). Os XMLs das notas fiscais vão junto.")
    falhas = []
    for email in para:
        ok, motivo = enviar_email_detalhado(email, f"Marketplace B2B: backup semanal {agora_utc():%d/%m/%Y}", texto,
                                            [(nome, arquivo, "application/zip")])
        if not ok:
            falhas.append(f"{email}: {motivo}")
    if len(falhas) == len(para):
        return _falhou("; ".join(falhas))
    salvar_config("backup_auto_em", agora_utc().isoformat(timespec="seconds"))
    salvar_config("backup_auto_erro", "; ".join(falhas))
    db.session.commit()
    current_app.logger.info("Backup automático enviado para %s (%d bytes)", ", ".join(para), len(arquivo))
    return True, f"Backup enviado para {', '.join(para)} ({len(arquivo) // 1024 + 1} KB)."


def _falhou(motivo):
    salvar_config("backup_auto_erro", motivo[:200])
    db.session.commit()
    current_app.logger.error("Backup automático: %s", motivo)
    return False, motivo


def talvez_enviar():
    """Chamado pelo painel: se o GitHub Actions não rodou, o backup sai mesmo assim (no máximo uma tentativa a cada
    6 horas). Nunca derruba a página."""
    if current_app.config.get("TESTING") and not current_app.config.get("BACKUP_AUTO_NOS_TESTES"):
        return
    try:
        if devido():
            enviar()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Backup automático pelo painel")
