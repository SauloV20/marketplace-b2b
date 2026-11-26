"""Carrinho abandonado: o carrinho de quem está na conta fica guardado no banco e, se ficar parado, a loja manda
um lembrete por e-mail.

- O carrinho da sessão é copiado para o banco sempre que muda (CarrinhoSalvo, um por pessoa: titular ou usuário da
  equipe). Ao entrar em outro aparelho com o carrinho vazio, ele volta.
- O lembrete sai uma vez para cada carrinho parado há mais de HORAS (Painel > Carrinhos abandonados) e há menos de
  JANELA_DIAS dias, só para e-mail confirmado e quem não pediu para parar. Depois de um lembrete, outro só se o
  carrinho mudar e já tiverem passado JANELA_DIAS dias.
- Quem fecha o pedido até JANELA_DIAS dias depois do lembrete conta como recuperado (Order.veio_do_lembrete).
- O envio roda pela tarefa agendada (/tarefas/carrinhos, GitHub Actions) ou pelo botão no painel.
"""
import json
from datetime import timedelta

from flask import current_app, request, session, url_for
from itsdangerous import BadSignature, URLSafeSerializer

from b2b import config, disponivel, salvar_config
from email_utils import enviar_email, link_externo
from models import CarrinhoSalvo, Order, Product, User, agora_utc, db

HORAS_PADRAO = 24
JANELA_DIAS = 7
MAX_POR_VEZ = 200  # e-mails por rodada (o resto sai na próxima)
MAX_ITENS_NO_EMAIL = 8
SEM_SINCRONIA = {None, "static", "tarefas.saude"}


# ---------- configuração ----------
def ligado():
    return config("carrinho_lembrete", "1") == "1"


def horas():
    try:
        return min(max(int(config("carrinho_horas", str(HORAS_PADRAO))), 1), 24 * JANELA_DIAS - 1)
    except ValueError:
        return HORAS_PADRAO


def salvar_configuracao(ativo, horas_txt):
    """Devolve a mensagem de erro, ou None se salvou."""
    try:
        h = int(str(horas_txt).strip())
    except ValueError:
        h = 0
    if not 1 <= h < 24 * JANELA_DIAS:
        return f"Informe de 1 a {24 * JANELA_DIAS - 1} horas."
    salvar_config("carrinho_lembrete", "1" if ativo else "0")
    salvar_config("carrinho_horas", str(h))
    return None


# ---------- carrinho guardado no banco ----------
def _pessoa_id():
    """Quem está montando o carrinho: o usuário da equipe, se for um, ou o titular. None = ninguém na conta."""
    return session.get("membro_id") or session.get("cliente_id")


def _texto(cart):
    return json.dumps({str(k): int(v) for k, v in (cart or {}).items()}, sort_keys=True)


def _ler(texto):
    try:
        dados = json.loads(texto or "{}")
        return {str(int(k)): int(v) for k, v in dados.items() if int(v) > 0}
    except (ValueError, TypeError, AttributeError):
        return {}


def ligar(app):
    """Sincroniza o carrinho da sessão com o banco, a cada visita de quem está na conta."""
    @app.before_request
    def _restaurar():
        if request.endpoint in SEM_SINCRONIA or request.blueprint == "admin" or "cart_sync" in session:
            return
        pessoa = _pessoa_id()
        if not pessoa:
            return
        salvo = CarrinhoSalvo.query.filter_by(user_id=pessoa).first()
        if salvo and not session.get("cart"):  # entrou em outro aparelho: o carrinho volta
            itens = _ler(salvo.itens)
            if itens:
                session["cart"] = itens
        session["cart_sync"] = _texto(session.get("cart"))

    @app.after_request
    def _guardar(resp):
        if request.endpoint in SEM_SINCRONIA or request.blueprint == "admin":
            return resp
        pessoa = _pessoa_id()
        if not pessoa or "cart_sync" not in session:
            return resp
        atual = _texto(session.get("cart"))
        if atual == session["cart_sync"]:
            return resp
        try:
            salvo = CarrinhoSalvo.query.filter_by(user_id=pessoa).first()
            if not salvo:
                salvo = CarrinhoSalvo(user_id=pessoa)
                db.session.add(salvo)
            salvo.itens, salvo.atualizado_em = atual, agora_utc()
            db.session.commit()
            session["cart_sync"] = atual
        except Exception:  # o carrinho da sessão continua valendo; tenta de novo na próxima visita
            db.session.rollback()
            current_app.logger.exception("Não foi possível guardar o carrinho de %s", pessoa)
        return resp


def pedido_fechado(order):
    """Chamado ao fechar o pedido: se veio depois de um lembrete recente, conta como recuperado."""
    pessoa = _pessoa_id()
    salvo = CarrinhoSalvo.query.filter_by(user_id=pessoa).first() if pessoa else None
    if salvo and salvo.avisado_em and salvo.avisado_em >= agora_utc() - timedelta(days=JANELA_DIAS):
        order.veio_do_lembrete = True


# ---------- lembrete por e-mail ----------
def _assinador():
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt="parar-lembrete-carrinho")


def link_parar(user):
    return link_externo(url_for("account.parar_lembrete", token=_assinador().dumps({"id": user.id, "e": user.email})))


def quem_parar(token):
    """A conta do link "não quero mais lembretes" (o link só vale para o mesmo e-mail)."""
    try:
        dados = _assinador().loads(token)
    except BadSignature:
        return None
    user = db.session.get(User, dados.get("id") or 0) if isinstance(dados, dict) else None
    return user if user and user.email == dados.get("e") else None


def parados():
    """Carrinhos com itens, parados há mais de `horas()` (e há menos de JANELA_DIAS dias), ainda sem lembrete."""
    agora = agora_utc()
    janela = agora - timedelta(days=JANELA_DIAS)
    return (CarrinhoSalvo.query.join(User, CarrinhoSalvo.user_id == User.id)
            .filter(CarrinhoSalvo.itens.notin_(("", "{}")),
                    CarrinhoSalvo.atualizado_em <= agora - timedelta(hours=horas()),
                    CarrinhoSalvo.atualizado_em >= janela,
                    db.or_(CarrinhoSalvo.conferido_em.is_(None), CarrinhoSalvo.conferido_em < CarrinhoSalvo.atualizado_em),
                    db.or_(CarrinhoSalvo.avisado_em.is_(None), CarrinhoSalvo.avisado_em < janela),
                    User.email_verified.is_(True), User.is_admin.is_(False), User.lembrete_carrinho.is_(True))
            .order_by(CarrinhoSalvo.atualizado_em).limit(MAX_POR_VEZ).all())


def produtos_guardados(salvo):
    """[(produto, quantidade)] do carrinho guardado, na ordem do carrinho (produtos excluídos ficam de fora)."""
    itens = _ler(salvo.itens)
    prods = {p.id: p for p in Product.query.filter(Product.id.in_([int(i) for i in itens])).all()} if itens else {}
    return [(prods[int(pid)], q) for pid, q in itens.items() if int(pid) in prods]


def _produtos(salvo):
    """[(produto, quantidade)] ainda à venda."""
    return [(p, q) for p, q in produtos_guardados(salvo) if p.active and disponivel(p)]


def _empresa_ativa(user):
    """Usuário da equipe que saiu da empresa (ou empresa excluída) não recebe lembrete."""
    if not user.empresa_id:
        return True
    empresa = db.session.get(User, user.empresa_id)
    return bool(empresa and not empresa.is_admin)


def enviar_lembretes():
    """Manda o lembrete para os carrinhos parados. Devolve {"enviados": n, "pulados": n}."""
    if not ligado():
        return {"enviados": 0, "pulados": 0, "desligado": True}
    enviados = pulados = 0
    for salvo in parados():
        user = salvo.user
        produtos = _produtos(salvo)
        comprou_depois = Order.query.filter(Order.user_id.in_({user.id, user.empresa_id or user.id}),
                                            Order.created_at >= salvo.atualizado_em).first()
        if not produtos or comprou_depois or not _empresa_ativa(user):
            salvo.conferido_em = agora_utc()  # nada a lembrar deste carrinho: só confere de novo se ele mudar
            db.session.commit()
            pulados += 1
            continue
        linhas = "\n".join(f"- {q}x {p.name}" for p, q in produtos[:MAX_ITENS_NO_EMAIL])
        if len(produtos) > MAX_ITENS_NO_EMAIL:
            linhas += f"\n- e mais {len(produtos) - MAX_ITENS_NO_EMAIL} produto(s)"
        texto = (f"Olá, {user.name}!\n\nVocê deixou estes produtos no carrinho do Marketplace B2B:\n\n{linhas}\n\n"
                 "Eles continuam guardados na sua conta. Os preços e o estoque são conferidos de novo quando você "
                 f"abrir o carrinho.\n\nFinalizar a compra: {link_externo(url_for('shop.cart'))}\n\n"
                 "Ficou alguma dúvida sobre preço, prazo ou frete? É só responder este e-mail.\n\nEquipe Marketplace B2B\n\n"
                 f"Não quer mais receber lembretes do carrinho? {link_parar(user)}")
        if enviar_email(user.email, "Seus produtos continuam no carrinho do Marketplace B2B", texto):
            salvo.avisado_em = salvo.conferido_em = agora_utc()
            db.session.commit()
            enviados += 1
        else:
            current_app.logger.warning("Lembrete do carrinho não enviado para a conta %s", user.id)
            break  # e-mail fora do ar: tenta de novo na próxima rodada
    return {"enviados": enviados, "pulados": pulados}


def resumo():
    """Números do painel: carrinhos com itens nos últimos dias, lembretes e pedidos recuperados (30 dias)."""
    agora = agora_utc()
    trinta = agora - timedelta(days=30)
    abertos = (CarrinhoSalvo.query.filter(CarrinhoSalvo.itens.notin_(("", "{}")),
                                          CarrinhoSalvo.atualizado_em >= agora - timedelta(days=JANELA_DIAS))
               .order_by(CarrinhoSalvo.atualizado_em.desc()).limit(100).all())
    recuperados = Order.query.filter(Order.veio_do_lembrete.is_(True), Order.created_at >= trinta,
                                     Order.status.notin_(("cancelado",))).all()
    return {"abertos": [(c, _produtos(c)) for c in abertos],
            "lembretes": CarrinhoSalvo.query.filter(CarrinhoSalvo.avisado_em >= trinta).count(),
            "recuperados": len(recuperados), "valor": sum((o.total for o in recuperados), 0)}
