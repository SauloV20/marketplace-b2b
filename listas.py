"""Listas de compra salvas pelo cliente (ex.: "Manutenção mensal", "Obra Centro").

O cliente salva produtos a partir da página do produto ou do carrinho e, depois, manda a lista inteira para o
carrinho. A quantidade segue as regras de cada produto (mínimo, múltiplo, estoque) na hora de ir ao carrinho.
"""
from b2b import ajustar_qtd, disponivel, preco_unitario
from models import ListaCompra, ListaItem, db

MAX_LISTAS = 30
MAX_ITENS = 200


def nome_valido(texto):
    return " ".join((texto or "").split())[:60]


def da_conta(cliente, lid):
    return ListaCompra.query.filter_by(id=lid, user_id=cliente.id).first()


def nova(cliente, nome):
    """Cria a lista. Devolve (lista, erro)."""
    nome = nome_valido(nome)
    if not nome:
        return None, "Dê um nome à lista (ex.: Manutenção mensal)."
    if ListaCompra.query.filter_by(user_id=cliente.id, nome=nome).first():
        return None, f"Você já tem uma lista chamada “{nome}”."
    if ListaCompra.query.filter_by(user_id=cliente.id).count() >= MAX_LISTAS:
        return None, f"Cada conta guarda até {MAX_LISTAS} listas. Apague alguma antes."
    lista = ListaCompra(user_id=cliente.id, nome=nome)
    db.session.add(lista)
    db.session.flush()
    return lista, None


def colocar(lista, produto, qtd, somar=True):
    """Põe o produto na lista (soma com o que já estava, se somar). Devolve erro ou None."""
    qtd = max(int(qtd or 1), 1)
    item = next((i for i in lista.itens if i.product_id == produto.id), None)
    if item:
        item.qtd = min(item.qtd + qtd if somar else qtd, 1_000_000)
        return None
    if len(lista.itens) >= MAX_ITENS:
        return f"A lista “{lista.nome}” já tem {MAX_ITENS} produtos."
    lista.itens.append(ListaItem(product_id=produto.id, qtd=min(qtd, 1_000_000)))
    return None


def para_o_carrinho(lista, carrinho):
    """Soma os itens da lista ao carrinho (dict da sessão). Devolve (carrinho novo, adicionados, nomes de fora)."""
    carrinho, adicionados, fora = dict(carrinho), 0, []
    for i in lista.itens:
        p = i.product
        nova_qtd = ajustar_qtd(p, int(carrinho.get(str(p.id), 0)) + i.qtd) if p and p.active else 0
        if nova_qtd:
            carrinho[str(p.id)] = nova_qtd
            adicionados += 1
        elif p:
            fora.append(p.name)
    return carrinho, adicionados, fora


def linhas(lista, cliente):
    """Itens com preço de agora e disponibilidade, para mostrar a lista."""
    resultado, total = [], 0
    for i in lista.itens:
        p = i.product
        ok = bool(p and p.active and disponivel(p))
        preco = preco_unitario(p, max(i.qtd, 1), cliente) if ok else None
        sub = preco * i.qtd if ok else 0
        total += sub
        resultado.append({"item": i, "p": p, "ok": ok, "preco": preco, "sub": sub})
    return resultado, total
