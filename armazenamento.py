"""Quanto o banco de dados está ocupando, o que mais pesa e a compressão das fotos já enviadas.

O Postgres gratuito do Neon tem espaço limitado (LIMITE_BANCO_MB, padrão 512 = 0,5 GB). Fotos, banners e
documentos ficam no banco, então são eles que mais crescem. Quando passa de 80%, os donos recebem um e-mail.
"""
import time
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import func, text

import imagens
from models import Banner, PedidoArquivo, ProdutoFoto, db

LOTE = 15  # imagens por clique (cada uma pode ter alguns MB: sem estourar memória nem o tempo da requisição)
# acima disto a imagem provavelmente ainda não foi comprimida (uma foto já comprimida fica bem abaixo)
GRANDE_FOTO, GRANDE_BANNER = 600_000, 900_000


def tamanho_do_banco():
    """Tamanho total do banco em bytes (None se não der para medir)."""
    try:
        dialeto = db.engine.dialect.name
        if dialeto == "postgresql":
            return int(db.session.execute(text("SELECT pg_database_size(current_database())")).scalar())
        if dialeto == "mysql":
            return int(db.session.execute(text(
                "SELECT COALESCE(SUM(data_length + index_length), 0) FROM information_schema.tables "
                "WHERE table_schema = DATABASE()")).scalar())
        if dialeto == "sqlite":
            paginas = db.session.execute(text("PRAGMA page_count")).scalar()
            tamanho = db.session.execute(text("PRAGMA page_size")).scalar()
            return int(paginas * tamanho)
    except Exception:
        db.session.rollback()
        current_app.logger.warning("Não foi possível medir o tamanho do banco", exc_info=True)
    return None


def limite():
    return int(current_app.config.get("LIMITE_BANCO_MB") or 512) * 1_000_000


def percentual():
    """% do limite em uso (para o aviso do painel); None se não der para medir."""
    total = tamanho_do_banco()
    return None if total is None else round(total * 100 / limite())


def _fila(depois_foto=0, depois_banner=0):
    fotos = ProdutoFoto.query.filter(func.length(ProdutoFoto.dados) > GRANDE_FOTO, ProdutoFoto.tipo != "image/gif",
                                     ProdutoFoto.id > depois_foto)
    ban = Banner.query.filter(func.length(Banner.imagem) > GRANDE_BANNER, Banner.imagem_tipo != "image/gif",
                              Banner.id > depois_banner)
    return fotos, ban


def a_comprimir(depois_foto=0, depois_banner=0):
    fotos, ban = _fila(depois_foto, depois_banner)
    return fotos.count() + ban.count()


def uso():
    total = tamanho_do_banco()
    partes = []
    for nome, modelo, coluna, filtro in (
            ("Fotos de produtos", ProdutoFoto, ProdutoFoto.dados, None),
            ("Banners", Banner, Banner.imagem, Banner.imagem.isnot(None)),
            ("Documentos dos pedidos", PedidoArquivo, PedidoArquivo.dados, None)):
        q = db.session.query(func.count(modelo.id), func.coalesce(func.sum(func.length(coluna)), 0))
        if filtro is not None:
            q = q.filter(filtro)
        qtd, soma = q.one()
        partes.append({"nome": nome, "qtd": int(qtd), "bytes": int(soma)})
    arquivos = sum(p["bytes"] for p in partes)
    tabelas = maiores_tabelas()
    lim = limite()
    return {"total": total, "limite": lim, "pct": round(total * 100 / lim) if total is not None else None,
            "partes": partes, "arquivos": arquivos, "outros": max((total or 0) - arquivos, 0) if total else None, "tabelas": tabelas}


NOMES_TABELAS = {"produto_foto": "Fotos de produtos", "banner": "Banners", "pedido_arquivo": "Documentos dos pedidos",
                 "order": "Pedidos", "order_item": "Itens dos pedidos", "user": "Clientes e contas",
                 "product": "Produtos", "nota_fiscal": "Notas fiscais", "pedido_evento": "Histórico dos pedidos",
                 "movimento_estoque": "Histórico de estoque", "lancamento": "Financeiro",
                 "nota_recebida": "Notas de compra", "limite_uso": "Controle de acessos",
                 "login_falha": "Tentativas de login"}


def maiores_tabelas(quantas=6):
    """As tabelas que mais ocupam (só no Postgres; lista vazia nos outros bancos ou se der erro)."""
    if db.engine.dialect.name != "postgresql":
        return []
    try:
        linhas = db.session.execute(text(
            "SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'r' AND n.nspname = current_schema() "
            "ORDER BY 2 DESC LIMIT :n"), {"n": quantas}).all()
    except Exception:
        db.session.rollback()
        return []
    return [{"tabela": nome, "nome": NOMES_TABELAS.get(nome, nome), "bytes": int(b)} for nome, b in linhas]


# ---------- aviso por e-mail quando o banco está enchendo ----------
NIVEIS = (80, 90, 95)  # cada nível novo avisa na hora; no mesmo nível, repete a cada 3 dias
REPETIR = timedelta(days=3)
CHECAR_A_CADA_S = 6 * 3600
_checado = {"em": 0.0}


def _mb(b):
    return f"{b / 1_000_000:.0f} MB"


def avisar_se_cheio():
    """Manda o e-mail se o banco passou de 80% do limite. Devolve o nível avisado (ou None)."""
    from b2b import config, salvar_config
    from models import agora_utc
    total = tamanho_do_banco()
    if total is None:
        return None
    pct = total * 100 / limite()
    nivel = max([n for n in NIVEIS if pct >= n], default=0)
    if not nivel:
        if config("espaco_aviso_nivel", ""):
            salvar_config("espaco_aviso_nivel", "")  # voltou ao normal: o próximo aperto avisa de novo
            db.session.commit()
        return None
    antes = int(config("espaco_aviso_nivel", "") or 0)
    try:
        quando = datetime.fromisoformat(config("espaco_aviso_em", ""))
    except ValueError:
        quando = None
    if nivel <= antes and quando and agora_utc() - quando < REPETIR:
        return None
    import alertas
    from email_utils import enviar_email_detalhado
    para = alertas._destinatarios(current_app)
    if not para:
        return None
    u = uso()
    linhas = [f"- {p['nome']}: {_mb(p['bytes'])}" for p in (u["tabelas"] or u["partes"])]
    texto = (f"O banco de dados da loja Marketplace B2B está com {pct:.0f}% do espaço do plano ocupado "
             f"({_mb(total)} de {_mb(limite())}).\n\n"
             "Quando enche, o banco para de aceitar dados novos: a loja deixa de receber pedidos e cadastros.\n\n"
             "O que mais ocupa:\n" + "\n".join(linhas) + "\n\n"
             "O que fazer: no painel, em Backup e espaço, comprima as fotos grandes e apague fotos, banners ou "
             "documentos que não usa mais. Se não der, é hora de passar para um plano pago do banco (e ajustar "
             "LIMITE_BANCO_MB no Render).")
    enviou = False
    for email in para:
        ok, _ = enviar_email_detalhado(email, f"Marketplace B2B: banco de dados {pct:.0f}% cheio", texto)
        enviou = enviou or ok
    if enviou:
        salvar_config("espaco_aviso_nivel", nivel)
        salvar_config("espaco_aviso_em", agora_utc().isoformat(timespec="seconds"))
        db.session.commit()
        return nivel
    return None


def checar_de_vez_em_quando():
    """Chamado depois das visitas de verdade: mede no máximo a cada 6 horas (por processo). Nunca derruba a página.
    Só roda quando alguém usa o site, então não acorda o banco à toa."""
    agora = time.time()
    if agora - _checado["em"] < CHECAR_A_CADA_S:
        return
    _checado["em"] = agora
    import threading
    app = current_app._get_current_object()

    def conferir():  # em outra linha de execução: sessão do banco própria e a página não espera
        with app.app_context():
            try:
                avisar_se_cheio()
            except Exception:
                db.session.rollback()
                app.logger.warning("Não foi possível conferir o espaço do banco", exc_info=True)
    threading.Thread(target=conferir, daemon=True).start()


def _reduzir(dados, tipo, lado, grande):
    novo, novo_tipo = imagens.comprimir(dados, tipo, lado)
    if len(novo) > grande:  # ainda grande (foto muito detalhada): um pouco menor e mais comprimida
        novo, novo_tipo = imagens.comprimir(dados, tipo, int(lado * 0.75))
    return novo, novo_tipo


def comprimir_existentes(depois_foto=0, depois_banner=0):
    """Comprime um lote das fotos e banners grandes já guardados.

    Devolve (quantas mudaram, bytes economizados, último id de foto, último id de banner) — os ids marcam até onde
    foi, para a próxima rodada continuar dali (uma imagem que não diminui não trava a fila)."""
    mudaram, economia = 0, 0
    fotos_q, ban_q = _fila(depois_foto, depois_banner)
    fotos = fotos_q.order_by(ProdutoFoto.id).limit(LOTE).all()
    for f in fotos:
        novo, tipo = _reduzir(f.dados, f.tipo, imagens.LADO_FOTO, GRANDE_FOTO)
        if len(novo) < len(f.dados):
            economia += len(f.dados) - len(novo)
            f.dados, f.tipo, f.versao = novo, tipo, (f.versao or 1) + 1
            mudaram += 1
        depois_foto = f.id
    if len(fotos) < LOTE:
        for b in ban_q.order_by(Banner.id).limit(LOTE - len(fotos)).all():
            novo, tipo = _reduzir(b.imagem, b.imagem_tipo, imagens.LADO_BANNER, GRANDE_BANNER)
            if len(novo) < len(b.imagem):
                economia += len(b.imagem) - len(novo)
                b.imagem, b.imagem_tipo, b.versao = novo, tipo, (b.versao or 1) + 1
                mudaram += 1
            depois_banner = b.id
    db.session.commit()
    return mudaram, economia, depois_foto, depois_banner
