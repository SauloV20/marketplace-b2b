"""Área do cliente: documentos de todos os pedidos, produtos já comprados (recompra) e endereços de entrega."""
from formato import hora_br
from frete import formatar_cep, normalizar_cep
from models import Endereco, Order, OrderItem, PedidoArquivo, Product

MAX_ENDERECOS = 20
MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
         "novembro", "dezembro"]


def nome_do_mes(chave):
    ano, mes = chave.split("-")
    return f"{MESES[int(mes) - 1]} de {ano}"


# ---------- documentos ----------
def documentos(cliente, mes="", tipo=""):
    """Documentos de todos os pedidos do cliente, mais novos primeiro. Devolve (lista, meses disponíveis)."""
    q = (PedidoArquivo.query.join(Order, Order.id == PedidoArquivo.order_id)
         .filter(Order.user_id == cliente.id).order_by(PedidoArquivo.criado_em.desc(), PedidoArquivo.id.desc()))
    todos = q.all()  # a coluna com o arquivo não é carregada aqui (deferred)
    meses = sorted({a.criado_em.strftime("%Y-%m") for a in todos if a.criado_em}, reverse=True)
    lista = [a for a in todos
             if (not mes or (a.criado_em and a.criado_em.strftime("%Y-%m") == mes)) and (not tipo or a.tipo == tipo)]
    return lista, meses


# ---------- produtos comprados ----------
def produtos_comprados(cliente):
    """O que o cliente já comprou (pedidos não cancelados e fora de orçamento), do mais recente para o mais antigo.

    Cada item: produto, data e quantidade da última compra, total comprado e em quantos pedidos."""
    linhas = (OrderItem.query.join(Order, Order.id == OrderItem.order_id)
              .filter(Order.user_id == cliente.id, Order.status.notin_(("cancelado", "orcamento")))
              .with_entities(OrderItem.product_id, OrderItem.quantity, Order.id, Order.created_at)
              .order_by(Order.created_at.desc(), Order.id.desc()).all())
    por_produto = {}
    for pid, qtd, oid, quando in linhas:
        r = por_produto.get(pid)
        if r is None:
            r = por_produto[pid] = {"pid": pid, "ultima_data": quando, "ultimo_pedido": oid, "ultima_qtd": 0,
                                    "total": 0, "pedidos": set()}
        if oid == r["ultimo_pedido"]:  # fechadas + fracionadas do mesmo pedido somam
            r["ultima_qtd"] += qtd
        r["total"] += qtd
        r["pedidos"].add(oid)
    produtos = {p.id: p for p in Product.query.filter(Product.id.in_(list(por_produto))).all()} if por_produto else {}
    resultado = []
    for r in por_produto.values():
        p = produtos.get(r["pid"])
        if p:
            resultado.append({**r, "p": p, "vezes": len(r["pedidos"])})
    return resultado


# ---------- exportar pedidos ----------
STATUS = {"orcamento": "Orçamento em análise", "aprovacao": "Aguardando aprovação da empresa",
          "pendente": "Aguardando pagamento", "pago": "Pago", "cancelado": "Cancelado"}
ENVIO = {"": "", "enviado": "Enviado", "entregue": "Entregue"}


def planilha_pedidos(cliente, metodos):
    """Excel com duas abas: Pedidos (um por linha) e Itens (um produto por linha). Devolve os bytes do .xlsx."""
    from openpyxl import Workbook

    pedidos = Order.query.filter_by(user_id=cliente.id).order_by(Order.id.desc()).all()
    wb = Workbook()
    ws = wb.active
    ws.title = "Pedidos"
    ws.append(["Pedido", "Data", "Situação", "Entrega", "Pagamento", "Prazo (dias)", "Subtotal", "Cupom",
               "Desconto", "Frete", "Total", "Transportadora", "Rastreio", "Endereço de entrega"])
    for o in pedidos:
        ws.append([o.id, hora_br(o.created_at), STATUS.get(o.status, o.status), ENVIO.get(o.envio or "", ""),
                   metodos.get(o.payment_method, o.payment_method or ""), o.prazo or "", float(o.subtotal),
                   o.cupom or "", float(o.desconto or 0), "a combinar" if o.frete_combinar else float(o.frete or 0),
                   float(o.total), o.transportadora or "", o.rastreio or "", o.address or ""])
    itens = wb.create_sheet("Itens")
    itens.append(["Pedido", "Data", "Situação", "Código", "Produto", "Quantidade", "Fracionado",
                  "Preço unitário", "Subtotal"])
    for o in pedidos:
        for i in o.items:
            itens.append([o.id, hora_br(o.created_at), STATUS.get(o.status, o.status), i.product.codigo or "",
                          i.product.name, i.quantity, "sim" if i.fracionado else "", float(i.unit_price),
                          float(i.unit_price * i.quantity)])
    larguras = {"Pedidos": [8, 17, 22, 10, 18, 12, 12, 12, 11, 11, 12, 18, 18, 45],
                "Itens": [8, 17, 22, 14, 45, 11, 11, 14, 12]}
    from planilha import arquivo_xlsx
    return arquivo_xlsx(wb, larguras)


# ---------- endereços ----------
def ler_endereco(form, cliente, eid=None):
    """Valida o endereço do formulário. Devolve (dados, erro)."""
    apelido = " ".join(form.get("apelido", "").split())[:60]
    endereco = " ".join(form.get("endereco", "").split())[:300]
    cep = normalizar_cep(form.get("cep", ""))
    if not apelido:
        return None, "Dê um nome ao endereço (ex.: Matriz, Filial Campinas, Obra Centro)."
    if not cep:
        return None, "Informe um CEP válido, com 8 números."
    if len(endereco) < 8:
        return None, "Informe o endereço completo: rua, número, bairro e cidade."
    repetido = Endereco.query.filter(Endereco.user_id == cliente.id, Endereco.apelido == apelido,
                                     Endereco.id != (eid or 0)).first()
    if repetido:
        return None, f"Você já tem um endereço chamado “{apelido}”."
    if not eid and Endereco.query.filter_by(user_id=cliente.id).count() >= MAX_ENDERECOS:
        return None, f"Cada conta guarda até {MAX_ENDERECOS} endereços. Remova algum antes."
    return dict(apelido=apelido, endereco=endereco, cep=cep), None


def guardar_do_checkout(cliente, endereco, cep):
    """'Guardar este endereço' na finalização: não duplica um endereço que já existe. Devolve o Endereco ou None."""
    endereco = " ".join(endereco.split())[:300]
    existentes = Endereco.query.filter_by(user_id=cliente.id).all()
    if any(e.cep == cep and e.endereco.lower() == endereco.lower() for e in existentes):
        return None
    if len(existentes) >= MAX_ENDERECOS:
        return None
    nomes = {e.apelido for e in existentes}
    n = len(existentes) + 1
    while f"Endereço {n}" in nomes:
        n += 1
    return Endereco(user_id=cliente.id, apelido=f"Endereço {n}", endereco=endereco, cep=cep,
                    padrao=not existentes)


def padrao(cliente):
    return Endereco.query.filter_by(user_id=cliente.id).order_by(Endereco.padrao.desc(), Endereco.id).first()


__all__ = ["documentos", "produtos_comprados", "ler_endereco", "guardar_do_checkout", "padrao", "formatar_cep"]
