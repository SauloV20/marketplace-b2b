"""Vários usuários na mesma empresa cliente: o titular (dono do cadastro com CNPJ) convida compradores e
aprovadores. Todos compram na conta da empresa (mesmos preços, endereços, listas e pedidos).

- Aprovador: faz pedidos direto e aprova os pedidos dos compradores.
- Comprador: o pedido vai para "aguardando aprovação" (sem prender estoque) e só segue para o Marketplace B2B quando o
  titular ou um aprovador aprovar. Com um limite, pedidos até esse valor seguem direto.
"""
import secrets
from decimal import Decimal, InvalidOperation

from models import User, db

PAPEIS = {"comprador": "Comprador (pedidos precisam de aprovação)", "aprovador": "Aprovador (compra e aprova)"}
MAX_MEMBROS = 30


def precisa_aprovacao(membro, total, metodo):
    if not membro or membro.papel != "comprador" or metodo == "orcamento":
        return False
    return membro.limite_aprovacao is None or total > membro.limite_aprovacao


def pode_aprovar(membro):
    """Quem está logado pode aprovar? (titular: membro None; ou um aprovador)"""
    return membro is None or membro.papel == "aprovador"


def aprovadores(titular):
    return [titular] + [m for m in titular.membros if m.papel == "aprovador"]


def ler(form, titular, uid=None):
    """Valida o formulário da equipe. Devolve (dados, erro)."""
    papel = form.get("papel", "")
    if papel not in PAPEIS:
        return None, "Escolha o papel: comprador ou aprovador."
    limite = None
    if papel == "comprador" and (form.get("limite") or "").strip():
        t = form.get("limite").replace("R$", "").strip()
        if "," in t:
            t = t.replace(".", "").replace(",", ".")
        try:
            limite = Decimal(t).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None, "Confira o limite (ex.: 1.000,00) ou deixe vazio para tudo precisar de aprovação."
        if not limite.is_finite() or not 0 <= limite < 100_000_000:
            return None, "Confira o limite."
    dados = {"papel": papel, "limite_aprovacao": limite}
    if uid is None:
        nome, email = " ".join(form.get("nome", "").split())[:120], form.get("email", "").strip().lower()[:120]
        if not nome or "@" not in email or "." not in email.split("@")[-1]:
            return None, "Informe o nome e um e-mail válido."
        if User.query.filter_by(email=email).first():
            return None, "Esse e-mail já tem conta no Marketplace B2B. Use o e-mail de trabalho da pessoa, que ainda não tenha cadastro."
        if len(titular.membros) >= MAX_MEMBROS:
            return None, f"Cada empresa tem até {MAX_MEMBROS} usuários."
        dados.update(name=nome, email=email)
    return dados, None


def novo_membro(titular, dados):
    u = User(empresa_id=titular.id, is_admin=False, tipo=titular.tipo, email_verified=False, permissoes="",
             **dados)
    u.set_password(secrets.token_urlsafe(32))  # ninguém sabe: a pessoa cria a dela pelo convite
    db.session.add(u)
    db.session.flush()
    return u


def nome_papel(u):
    if not u or not u.empresa_id:
        return "Titular da conta"
    return {"comprador": "Comprador", "aprovador": "Aprovador"}.get(u.papel, "Usuário")
