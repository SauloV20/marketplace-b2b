"""Aviso por e-mail quando o site dá erro: o dono da loja fica sabendo antes de o cliente reclamar.

Todo erro registrado pelo site (página que quebrou, backup que falhou, e-mail que não saiu...) vira um e-mail com o
que aconteceu e em qual página. Para não lotar a caixa: o mesmo erro avisa no máximo uma vez por hora, e são no
máximo 20 avisos por dia. Vai para ALERTA_EMAIL (variável do Render, até 3 e-mails separados por vírgula) ou, sem
ela, para os mesmos e-mails do backup semanal. Só liga no ar (no Render), não no computador nem nos testes.
O aviso leva só a página e o erro: nunca os dados digitados pelo visitante (formulários, senhas, cookies).
"""
import logging
import threading
import time
import traceback
from datetime import datetime, timezone

from flask import has_request_context, request

MESMO_ERRO_S = 3600  # o mesmo erro avisa no máximo 1 vez por hora
POR_DIA = 20         # no máximo 20 avisos por dia
_trava = threading.Lock()
_dentro = threading.local()  # um erro ao mandar o aviso não gera outro aviso (sem laço)
_estado = {"ultimo": {}, "dia": "", "no_dia": 0}


def zerar():
    """Para os testes: esquece os avisos já mandados."""
    with _trava:
        _estado.update(ultimo={}, dia="", no_dia=0)


def _assinatura(registro):
    """O que identifica "o mesmo erro": o tipo da exceção e onde ela aconteceu (ou o texto fixo da mensagem)."""
    if registro.exc_info and registro.exc_info[1] is not None:
        tb = traceback.extract_tb(registro.exc_info[2])
        onde = f"{tb[-1].filename}:{tb[-1].lineno}" if tb else ""
        return f"{registro.exc_info[0].__name__}@{onde}"
    return f"{registro.name}:{registro.msg}"[:300]


def _pode_avisar(assinatura):
    agora = time.time()
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _trava:
        if _estado["dia"] != hoje:
            _estado.update(dia=hoje, no_dia=0)
        if _estado["no_dia"] >= POR_DIA or agora - _estado["ultimo"].get(assinatura, 0) < MESMO_ERRO_S:
            return False
        _estado["ultimo"][assinatura] = agora
        _estado["no_dia"] += 1
        return True


def _texto(registro):
    linhas = [f"Quando: {datetime.now(timezone.utc):%d/%m/%Y %H:%M:%S} (UTC; Brasília = UTC-3)"]
    if has_request_context():
        linhas.append(f"Página: {request.method} {request.path}")  # só o caminho: sem ?parâmetros nem formulário
        if request.endpoint:
            linhas.append(f"Tela: {request.endpoint}")
    linhas += ["", "O que aconteceu:", registro.getMessage()[:1000]]
    if registro.exc_info and registro.exc_info[1] is not None:
        detalhe = "".join(traceback.format_exception(*registro.exc_info, limit=15))
        linhas += ["", "Detalhe técnico (para quem for corrigir):", detalhe[-6000:]]
    linhas += ["", "O mesmo erro só avisa de novo depois de 1 hora (no máximo 20 avisos por dia).",
               "Os registros completos ficam em Render > Marketplace B2B > Logs."]
    return "\n".join(linhas)


def _destinatarios(app):
    lista = [e.strip() for e in (app.config.get("ALERTA_EMAIL") or "").split(",") if "@" in e]
    if lista:
        return lista[:3]
    try:
        import backup_auto
        return backup_auto.destinatarios()
    except Exception:  # o erro pode ser justamente no banco
        return []


def _enviar(app, assunto, texto):
    _dentro.ativo = True
    try:
        with app.app_context():
            from email_utils import enviar_email_detalhado
            for para in _destinatarios(app):
                enviar_email_detalhado(para, assunto, texto)
    except Exception:
        pass  # avisar é melhor esforço: nunca derruba nada
    finally:
        _dentro.ativo = False


class AvisoPorEmail(logging.Handler):
    def __init__(self, app):
        super().__init__(logging.ERROR)
        self.app = app

    def emit(self, registro):
        if getattr(_dentro, "ativo", False):
            return
        try:
            if not _pode_avisar(_assinatura(registro)):
                return
            onde = f" em {request.path}" if has_request_context() else ""
            assunto = f"Marketplace B2B: erro no site{onde}"[:150]
            texto = _texto(registro)  # montado aqui: depois a requisição já acabou
            if self.app.config.get("TESTING"):
                _enviar(self.app, assunto, texto)
            else:  # não atrasa a página do cliente esperando o serviço de e-mail
                threading.Thread(target=_enviar, args=(self.app, assunto, texto), daemon=True).start()
        except Exception:
            pass


def ligar(app):
    if app.config.get("PRODUCAO") or app.config.get("ALERTAS_ERRO"):
        app.logger.addHandler(AvisoPorEmail(app))
