"""Editar um pedido que ainda aguarda pagamento: quantidades, preços, itens novos e frete, com o estoque acertado.

Só vale para pedidos "pendente", ainda não enviados e sem cobrança Pix gerada na Efí (a cobrança tem o valor
antigo e o mesmo identificador; mudar o pedido faria o cliente pagar um valor diferente do total).
"""
from decimal import Decimal, InvalidOperation

from sqlalchemy import update

import cupons
import financeiro
import formacao
import frete
from formato import brl, ler_numero
from b2b import ajustar_qtd, pedido_minimo, preco_fracionado, preco_unitario, separar_fracionado
from models import Order, OrderItem, Product, User, baixar_estoque, db, devolver_estoque

CENTAVO = Decimal("0.01")


def motivo_bloqueio(o):
    """Por que este pedido não pode ser editado (None = pode)."""
    if o.status != "pendente":
        return {"orcamento": "Orçamentos são ajustados na tela de revisão do orçamento.",
                "pago": "O pedido já foi pago.", "cancelado": "O pedido está cancelado."}.get(o.status, "")
    if o.envio:
        return "O pedido já foi enviado."
    if o.pix_txid:
        return ("Já existe uma cobrança Pix gerada na Efí com o valor atual. Para mudar, cancele este pedido e "
                "peça para o cliente fazer outro.")
    if o.cartao_aberto:
        return ("Já existe um link de pagamento com cartão na Efí com o valor atual. Para mudar, cancele este "
                "pedido e peça para o cliente fazer outro.")
    return None


def _achar_produto(texto):
    t = " ".join((texto or "").split())
    if not t:
        return None
    p = Product.query.filter(db.func.lower(Product.codigo) == t.lower()).first()
    if not p and t.lstrip("#").isdigit() and len(t) <= 9:
        p = db.session.get(Product, int(t.lstrip("#")))
    if not p:
        p = Product.query.filter(db.func.lower(Product.name) == t.lower()).first()
    return p


def aplicar(o, form):
    """Aplica a edição. Devolve (ok, mensagem). Em erro, nada muda (nem o estoque)."""
    bloqueio = motivo_bloqueio(o)
    if bloqueio:
        return False, bloqueio
    novos = {}
    try:
        for i in o.items:
            qtd = int(form.get(f"qtd_{i.id}", i.quantity))
            preco = ler_numero(form.get(f"preco_{i.id}", str(i.unit_price)), casas=CENTAVO)
            if qtd < 0 or qtd > 1_000_000 or not 0 <= preco < 100_000_000:
                raise ValueError
            novos[i.id] = (qtd, preco)
        frete = ler_numero(form.get("frete"), casas=CENTAVO) if (form.get("frete") or "").strip() else o.frete
        if not 0 <= frete < 1_000_000:
            raise ValueError
    except (ValueError, InvalidOperation):
        return False, "Confira quantidades (números inteiros), preços e frete (como 16,50)."

    adicionar = None
    if (form.get("novo_produto") or "").strip():
        p = _achar_produto(form.get("novo_produto"))
        try:
            qtd_nova = int(form.get("novo_qtd") or 0)
        except ValueError:
            qtd_nova = 0
        if not p:
            return False, f"Produto “{form.get('novo_produto').strip()}” não encontrado (use o código, o número ou o nome exato)."
        if qtd_nova <= 0:
            return False, "Informe a quantidade do produto novo."
        cliente = db.session.get(User, o.user_id) if o.user_id else None
        adicionar = (p, qtd_nova, preco_unitario(p, qtd_nova, cliente, formacao.uf_do_pedido(o)))

    if not any(q for q, _ in novos.values()) and not adicionar:
        return False, "O pedido ficaria sem itens. Para desistir dele, cancele ou exclua."
    import margem
    if margem.bloqueia():
        itens = [(i.product, novos[i.id][1], novos[i.id][0], i.custo_unitario) for i in o.items if novos[i.id][0]]
        if adicionar:
            itens.append((adicionar[0], adicionar[2], adicionar[1], None))
        problemas = margem.problemas_itens(itens, formacao.uf_do_pedido(o) or None, o.desconto)
        if problemas:
            return False, "Margem abaixo do mínimo: " + "; ".join(problemas) + ". Nada foi alterado."

    # estoque primeiro: se faltar para algum, nada é gravado
    faltou = []
    for i in o.items:
        delta = novos[i.id][0] - i.quantity
        frac = abs(delta) if i.fracionado else 0
        if delta > 0 and not baixar_estoque(i.product_id, delta, frac, "pedido", o.id):
            faltou.append(i.product.name)
        elif delta < 0:
            devolver_estoque(i.product_id, -delta, frac, "pedido", o.id)
    if adicionar and not baixar_estoque(adicionar[0].id, adicionar[1], 0, "pedido", o.id):
        faltou.append(adicionar[0].name)
    if faltou:
        db.session.rollback()
        return False, "Sem estoque para: " + ", ".join(faltou) + "."

    mudancas = []
    for i in list(o.items):
        qtd, preco = novos[i.id]
        if qtd == 0:
            mudancas.append(f"tirou {i.product.name}")
            o.items.remove(i)
            continue
        if qtd != i.quantity:
            mudancas.append(f"{i.product.name}: {i.quantity} → {qtd} un")
        if preco != i.unit_price:
            mudancas.append(f"{i.product.name}: preço ajustado")
        i.quantity, i.unit_price = qtd, preco
    if adicionar:
        p, qtd, preco = adicionar
        existente = next((i for i in o.items if i.product_id == p.id and not i.fracionado), None)
        if existente:
            existente.quantity += qtd
        else:
            o.items.append(OrderItem(product_id=p.id, quantity=qtd, unit_price=preco))
        mudancas.append(f"incluiu {qtd} un de {p.name}")
    if frete != o.frete or (o.frete_combinar and (form.get("frete") or "").strip()):
        mudancas.append("frete ajustado")
        o.frete, o.frete_combinar = frete, False
    db.session.flush()
    o.desconto = cupons.recalcular(o)
    total = o.subtotal - o.desconto + o.frete
    # só grava se o pedido ainda estiver aguardando pagamento (o cliente pode ter cancelado neste instante)
    r = db.session.execute(update(Order).where(Order.id == o.id, Order.status == "pendente", Order.envio == "",
                                               db.or_(Order.pix_txid == "", Order.pix_txid.is_(None)))
                           .values(total=total, desconto=o.desconto).execution_options(synchronize_session=False))
    if r.rowcount != 1:
        db.session.rollback()
        return False, "O pedido acabou de mudar (pago, cancelado ou enviado). Recarregue a página."
    o.total = total
    financeiro.pedido_mudou(o)  # as parcelas a receber acompanham o novo total
    db.session.commit()
    if not mudancas:
        return True, "Nada mudou no pedido."
    return True, f"Pedido #{o.id} atualizado: " + "; ".join(mudancas) + "."


# ---------- o próprio cliente altera o pedido ----------
def motivo_bloqueio_cliente(o):
    """Por que o cliente não pode alterar este pedido (None = pode)."""
    if o.status not in ("pendente", "orcamento", "aprovacao"):
        return "Só dá para alterar pedidos aguardando pagamento ou aprovação e orçamentos em análise."
    if o.envio:
        return "O pedido já foi enviado."
    if o.pix_txid:
        return ("O Pix deste pedido já foi gerado com o valor atual. Para mudar, cancele este pedido e faça um "
                "novo (os itens podem ir de volta ao carrinho com “Comprar novamente”).")
    if o.cartao_aberto:
        return ("O link de pagamento com cartão deste pedido já foi criado com o valor atual. Para mudar, cancele "
                "este pedido e faça um novo (os itens podem ir de volta ao carrinho com “Comprar novamente”).")
    return None


def _linhas_do_produto(p, qtd, cliente, uf=""):
    """Itens do pedido para `qtd` unidades, com as regras da loja: preço da faixa e fracionado com acréscimo.

    `uf` é o estado de entrega do pedido (o preço calculado muda por estado)."""
    fechadas, frac = separar_fracionado(p, qtd)
    linhas = []
    if fechadas:
        linhas.append(OrderItem(product_id=p.id, quantity=fechadas, unit_price=preco_unitario(p, qtd, cliente, uf)))
    if frac:
        linhas.append(OrderItem(product_id=p.id, quantity=frac, unit_price=preco_fracionado(p, qtd, cliente, uf),
                                fracionado=True))
    return linhas, frac


def aplicar_cliente(o, cliente, form, membro=None):
    """O cliente muda quantidades, tira itens ou inclui um produto pelo código. Devolve (ok, mensagem, mudanças).

    `membro` = quem da equipe da empresa está alterando (None = o titular): um comprador não aumenta um pedido já
    liberado para além do limite dele sem passar pela aprovação da empresa.

    Produtos que não mudaram mantêm o preço do pedido (inclusive um preço combinado com a loja); os que mudaram
    seguem as regras de agora (faixa de quantidade, desconto da empresa, mínimo, múltiplo e estoque)."""
    bloqueio = motivo_bloqueio_cliente(o)
    if bloqueio:
        return False, bloqueio, []
    total_antes = Decimal(o.total or 0)
    atual = {}  # produto -> [quantidade, fracionadas]
    for i in o.items:
        a = atual.setdefault(i.product_id, [0, 0])
        a[0] += i.quantity
        a[1] += i.quantity if i.fracionado else 0
    desejado = {}
    try:
        for pid, (qtd, _) in atual.items():
            desejado[pid] = int(form.get(f"qtd_{pid}", qtd))
        if (form.get("novo_produto") or "").strip():
            p = _achar_produto(form.get("novo_produto"))
            if not p or not p.active:
                return False, f"Produto “{form.get('novo_produto').strip()}” não encontrado. Use o código que aparece na página do produto.", []
            desejado[p.id] = desejado.get(p.id, 0) + int(form.get("novo_qtd") or 1)
    except ValueError:
        return False, "Confira as quantidades: use números inteiros.", []
    if any(q < 0 or q > 1_000_000 for q in desejado.values()):
        return False, "Confira as quantidades.", []
    if not any(desejado.values()):
        return False, "O pedido ficaria sem itens. Para desistir dele, use “Cancelar pedido”.", []

    segura_estoque = o.status == "pendente"
    produtos = {p.id: p for p in Product.query.filter(Product.id.in_(list(desejado))).all()}
    mudancas, ajustes, faltou = [], [], []
    mudou = {pid for pid, q in desejado.items() if q != atual.get(pid, [0])[0]}
    novos_itens = {}
    for pid in mudou:
        p, qtd = produtos[pid], desejado[pid]
        if qtd == 0:
            mudancas.append(f"tirou {p.name}")
            continue
        if not p.active:
            faltou.append(p.name)
            continue
        livre = p.stock + (atual.get(pid, [0])[0] if segura_estoque else 0)  # o que o pedido já segura também conta
        certo = ajustar_qtd(p, qtd, livre)
        if not certo:
            faltou.append(p.name)
            continue
        if certo != qtd:
            ajustes.append(f"{p.name}: ajustado para {certo} un (mínimo {p.qtd_minima}"
                           + ("" if p.fracionavel or p.multiplo <= 1 else f", múltiplos de {p.multiplo}") + ")")
        novos_itens[pid] = certo
        antes = atual.get(pid, [0])[0]
        mudancas.append(f"incluiu {certo} un de {p.name}" if not antes else f"{p.name}: {antes} → {certo} un")
    if faltou:
        return False, "Sem estoque suficiente (ou fora de linha): " + ", ".join(faltou) + ".", []
    if not mudou:
        return True, "Nada mudou no pedido.", []

    # estoque: devolve o que o pedido segurava destes produtos e tira o novo (tudo ou nada)
    if segura_estoque:
        for pid in mudou:
            if pid in atual:
                devolver_estoque(pid, atual[pid][0], atual[pid][1], "pedido", o.id)
        for pid, qtd in novos_itens.items():
            _, frac = separar_fracionado(produtos[pid], qtd)
            if not baixar_estoque(pid, qtd, frac, "pedido", o.id):
                db.session.rollback()
                return False, f"O estoque de {produtos[pid].name} acabou de mudar. Confira e tente de novo.", []

    for i in [i for i in o.items if i.product_id in mudou]:
        o.items.remove(i)
    for pid, qtd in novos_itens.items():
        linhas, _ = _linhas_do_produto(produtos[pid], qtd, cliente, formacao.uf_do_pedido(o))
        o.items.extend(linhas)
    db.session.flush()

    subtotal = o.subtotal
    minimo = pedido_minimo()
    if subtotal < minimo:
        db.session.rollback()
        return False, f"O pedido mínimo da loja é de {brl(minimo)}. Inclua mais itens.", []
    o.desconto = cupons.recalcular(o)
    if o.cep and not o.frete_combinar:  # o frete muda com o peso e o valor (ex.: frete grátis acima de)
        import melhor_envio
        peso = sum((Decimal(i.product.peso_kg or 0) * i.quantity for i in o.items), Decimal(0))
        opcoes = frete.opcoes(o.cep, subtotal, peso, melhor_envio.itens_do_pedido(o))
        mesma = next((c for c in opcoes if o.frete_codigo and c["codigo"] == o.frete_codigo), None)
        calculo = mesma or (frete.escolher(opcoes) if not (o.frete_codigo or "").startswith("me:") else None)
        if calculo:  # mantém o serviço que o cliente escolheu (ex.: SEDEX), com o valor novo
            o.frete, o.frete_prazo = calculo["valor"], (calculo.get("prazo") or "")[:60]
            o.frete_servico, o.frete_codigo = frete.servico_do_pedido(calculo), calculo["codigo"][:20]
        elif (o.frete_codigo or "").startswith("me:"):
            pass  # o Melhor Envio não cotou agora (fora do ar ou serviço indisponível): o frete fica como estava
        else:
            o.frete, o.frete_combinar = Decimal(0), True
    total = subtotal - o.desconto + o.frete
    if membro is not None and o.status == "pendente" and total > total_antes:
        import equipe_empresa
        if equipe_empresa.precisa_aprovacao(membro, total, o.payment_method):
            db.session.rollback()
            return False, ("Com essa mudança o pedido passaria do seu limite sem aprovação da empresa. Para comprar "
                           "mais, faça um novo pedido (ele vai para a aprovação do titular ou de um aprovador)."), []
    if o.payment_method == "faturado" and o.status == "pendente":
        import credito
        if not credito.cabe(cliente, total, exceto=o.id):
            msg = credito.mensagem(cliente, exceto=o.id)
            db.session.rollback()
            return False, msg, []
    r = db.session.execute(update(Order).where(Order.id == o.id, Order.status == o.status, Order.envio == "",
                                               db.or_(Order.pix_txid == "", Order.pix_txid.is_(None)))
                           .values(total=total).execution_options(synchronize_session=False))
    if r.rowcount != 1:
        db.session.rollback()
        return False, "O pedido acabou de mudar na loja. Recarregue a página.", []
    o.total = total
    financeiro.pedido_mudou(o)
    db.session.commit()
    msg = "Pedido atualizado." + (" " + " ".join(ajustes) + "." if ajustes else "")
    return True, msg, mudancas
