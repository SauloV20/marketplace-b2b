"""Tarefas agendadas chamadas de fora (GitHub Actions), com token. Sem token configurado, não existem."""
import hmac

from flask import Blueprint, abort, current_app, jsonify, request

import backup_auto

tarefas = Blueprint("tarefas", __name__)


def _autorizado():
    token = current_app.config.get("TAREFAS_TOKEN", "")
    enviado = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        abort(404)
    return hmac.compare_digest(enviado.encode(), token.encode())


@tarefas.route("/saude", methods=["GET", "HEAD"])
def saude():
    """Página leve para o monitor externo (UptimeRobot) visitar a cada 5 min: mantém o site acordado no plano grátis
    do Render. Não usa o banco, para o Neon poder dormir (e não gastar as horas grátis dele)."""
    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store",
                       "X-Robots-Tag": "noindex"}


@tarefas.post("/efi/boleto")
def efi_boleto():
    """A Efí avisa que um boleto mudou (campo "notification"). Consultamos a API com o token antes de baixar."""
    import boleto_efi
    from security import pode_receber_aviso_efi
    if not pode_receber_aviso_efi():  # a Efí reenvia o aviso mais tarde
        return "", 429
    try:
        boleto_efi.notificacao(request.form.get("notification", "").strip())
    except boleto_efi.ErroBoleto as e:
        current_app.logger.warning("Notificação de boleto da Efí: %s", e)
    return "", 200


@tarefas.post("/tarefas/backup-semanal")
def backup_semanal():
    if not _autorizado():
        abort(403)
    ok, msg = backup_auto.enviar(forcar=request.args.get("forcar") == "1")
    import armazenamento
    try:  # aproveita a visita semanal do GitHub para conferir o espaço do banco
        armazenamento.avisar_se_cheio()
    except Exception:
        current_app.logger.warning("Não foi possível conferir o espaço do banco", exc_info=True)
    return jsonify(ok=ok, mensagem=msg), 200


@tarefas.post("/tarefas/rastreio")
def rastreio():
    """Conferência automática do rastreio das etiquetas do Melhor Envio (GitHub Actions, algumas vezes por dia)."""
    if not _autorizado():
        abort(403)
    import etiquetas
    return jsonify(etiquetas.rastrear_pendentes()), 200


@tarefas.post("/tarefas/carrinhos")
def carrinhos():
    """Lembrete do carrinho abandonado por e-mail (GitHub Actions, algumas vezes por dia)."""
    if not _autorizado():
        abort(403)
    import carrinho_abandonado
    return jsonify(carrinho_abandonado.enviar_lembretes()), 200


@tarefas.post("/webhooks/melhor-envio")
def webhook_melhor_envio():
    """Aviso do Melhor Envio quando uma etiqueta muda (só com aplicativo e MELHOR_ENVIO_SECRET configurados)."""
    if not current_app.config.get("MELHOR_ENVIO_SECRET"):
        abort(404)
    from security import pode_receber_aviso_melhor_envio
    if not pode_receber_aviso_melhor_envio():
        return "", 429
    import etiquetas
    return "", etiquetas.receber_webhook(request.get_data(cache=False), request.headers.get("X-ME-Signature", ""))
