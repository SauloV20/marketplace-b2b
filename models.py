import json
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import select, update
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def agora_utc():
    """Data/hora UTC sem fuso (o banco guarda assim); substitui o datetime.utcnow(), que vai sair do Python."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    phone = db.Column(db.String(30), default="")
    address = db.Column(db.String(300), default="")
    email_verified = db.Column(db.Boolean, default=False, nullable=False)
    # vendas para empresas
    tipo = db.Column(db.String(2), default="pf", nullable=False)  # pf (CPF) | pj (CNPJ)
    documento = db.Column(db.String(14), unique=True)  # CPF ou CNPJ, só dígitos
    razao_social = db.Column(db.String(160), default="")
    inscricao_estadual = db.Column(db.String(30), default="")
    empresa_aprovada = db.Column(db.Boolean, default=False, nullable=False)  # libera desconto e prazos
    desconto = db.Column(db.Numeric(5, 2), default=0, nullable=False)  # % sobre a tabela
    prazos = db.Column(db.String(120), default="")  # ex.: "28; 30/60/90"
    limite_credito = db.Column(db.Numeric(12, 2))  # teto de boletos faturados em aberto (vazio = sem limite)
    # consulta do CNPJ na Receita (BrasilAPI): situação ("ATIVA"...; vazio = não verificado) e o resumo em JSON
    receita_situacao = db.Column(db.String(40), default="")
    receita_dados = db.Column(db.Text, default="")
    receita_consultado_em = db.Column(db.DateTime)
    telefone_verificado = db.Column(db.Boolean, default=False, nullable=False)  # confirmado por SMS
    termos_aceitos_em = db.Column(db.DateTime)  # LGPD: quando aceitou a política e os termos
    termos_versao = db.Column(db.String(20), default="")
    lembrete_carrinho = db.Column(db.Boolean, default=True, nullable=False)  # aceita o lembrete do carrinho
    # verificação em duas etapas (só para o painel): segredo do app autenticador e códigos de recuperação (hashes)
    totp_segredo = db.Column(db.String(64), default="")
    totp_ultimo = db.Column(db.BigInteger, default=0, nullable=False)  # último código usado (não vale de novo)
    codigos_recuperacao = db.Column(db.Text, default="")
    # administrador: áreas do painel ("tudo" = acesso total; ver permissoes.py)
    permissoes = db.Column(db.String(200), default="tudo")
    # usuário da empresa cliente: faz parte da conta de outra (o titular, que tem o CNPJ e as condições)
    empresa_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    papel = db.Column(db.String(10), default="")  # comprador | aprovador (vazio = titular ou pessoa física)
    limite_aprovacao = db.Column(db.Numeric(12, 2))  # comprador: pedidos até este valor não precisam de aprovação
    membros = db.relationship("User", backref=db.backref("titular", remote_side="User.id"),
                              cascade="all, delete-orphan", order_by="User.name")
    enderecos = db.relationship("Endereco", cascade="all, delete-orphan",
                                order_by="(Endereco.padrao.desc(), Endereco.id)")
    avisos_estoque = db.relationship("AvisoEstoque", cascade="all, delete-orphan")
    carrinho_salvo = db.relationship("CarrinhoSalvo", back_populates="user", cascade="all, delete-orphan")
    avaliacoes = db.relationship("Avaliacao", back_populates="user", cascade="all, delete-orphan")
    listas = db.relationship("ListaCompra", cascade="all, delete-orphan", order_by="ListaCompra.nome")

    @property
    def receita(self):
        try:
            return json.loads(self.receita_dados or "{}")
        except ValueError:
            return {}

    def guardar_receita(self, dados):
        self.receita_situacao = dados["situacao"]
        self.receita_dados = json.dumps(dados, ensure_ascii=False)
        self.receita_consultado_em = agora_utc()

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Endereco(db.Model):
    """Endereço de entrega guardado pelo cliente (matriz, filial, obra...)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    apelido = db.Column(db.String(60), nullable=False)
    cep = db.Column(db.String(8), nullable=False)
    endereco = db.Column(db.String(300), nullable=False)
    padrao = db.Column(db.Boolean, default=False, nullable=False)


class AvisoEstoque(db.Model):
    """'Avise-me quando chegar': o cliente recebe um e-mail quando o produto volta a ter estoque."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    criado_em = db.Column(db.DateTime, default=agora_utc)
    avisado_em = db.Column(db.DateTime)  # vazio = ainda esperando
    product = db.relationship("Product")


class CarrinhoSalvo(db.Model):
    """O carrinho de quem está na conta, guardado no banco: volta em outro aparelho e serve ao lembrete por e-mail."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)  # quem monta (titular ou da equipe)
    itens = db.Column(db.Text, default="{}")  # {"id do produto": quantidade}, como na sessão
    atualizado_em = db.Column(db.DateTime, default=agora_utc, index=True)
    avisado_em = db.Column(db.DateTime)  # último lembrete enviado
    conferido_em = db.Column(db.DateTime)  # última vez que o lembrete olhou este carrinho (enviando ou não)
    user = db.relationship("User", back_populates="carrinho_salvo")


class Devolucao(db.Model):
    """Pedido de devolução (com reembolso) ou troca feito pelo cliente; a loja aprova, recebe e conclui."""
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False, index=True)
    pedido_por = db.Column(db.String(120), default="")  # nome de quem pediu (titular ou alguém da equipe)
    tipo = db.Column(db.String(10), nullable=False, default="devolucao")  # devolucao | troca
    motivo = db.Column(db.Text, default="")
    itens = db.Column(db.Text, default="[]")  # [{"item": id do item, "produto": id, "nome", "qtd", "preco", "frac"}]
    status = db.Column(db.String(12), nullable=False, default="solicitada", index=True)
    resposta = db.Column(db.String(500), default="")  # instruções ou motivo da recusa, para o cliente
    estoque_devolvido = db.Column(db.Boolean, default=False, nullable=False)
    valor = db.Column(db.Numeric(12, 2))  # reembolso feito
    reembolso_forma = db.Column(db.String(20), default="")
    criado_em = db.Column(db.DateTime, default=agora_utc, index=True)
    atualizado_em = db.Column(db.DateTime, default=agora_utc)
    order = db.relationship("Order", back_populates="devolucoes")


class Avaliacao(db.Model):
    """Nota e comentário de quem comprou o produto. Só aparece na loja depois de aprovada no painel."""
    __table_args__ = (db.UniqueConstraint("user_id", "product_id"),)
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)  # quem avaliou (titular ou da equipe)
    nota = db.Column(db.Integer, nullable=False)  # 1 a 5
    comentario = db.Column(db.Text, default="")
    nome = db.Column(db.String(60), default="")  # como aparece na loja (ex.: "Ana S.")
    status = db.Column(db.String(10), nullable=False, default="pendente", index=True)  # pendente | aprovada | recusada
    resposta = db.Column(db.String(500), default="")  # resposta pública do Marketplace B2B
    criado_em = db.Column(db.DateTime, default=agora_utc, index=True)
    product = db.relationship("Product")
    user = db.relationship("User", back_populates="avaliacoes")


class ListaCompra(db.Model):
    """Lista de compras salva pelo cliente (ex.: Manutenção mensal), para mandar ao carrinho de uma vez."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    nome = db.Column(db.String(60), nullable=False)
    criada_em = db.Column(db.DateTime, default=agora_utc)
    itens = db.relationship("ListaItem", cascade="all, delete-orphan", order_by="ListaItem.id")


class ListaItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    lista_id = db.Column(db.Integer, db.ForeignKey("lista_compra.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    qtd = db.Column(db.Integer, nullable=False, default=1)
    product = db.relationship("Product")


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    markup = db.Column(db.Numeric(6, 2))  # % sobre o custo para os produtos com preço calculado (vazio = padrão)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(40), unique=True)  # código/SKU, usado para atualizar pela planilha
    name = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, default="")
    price = db.Column(db.Numeric(10, 2), nullable=False)
    stock = db.Column(db.Integer, default=0, nullable=False)
    image_url = db.Column(db.String(500), default="")
    active = db.Column(db.Boolean, default=True, nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=agora_utc)
    qtd_minima = db.Column(db.Integer, default=1, nullable=False)
    multiplo = db.Column(db.Integer, default=1, nullable=False)  # vende só em múltiplos (ex.: caixa com 10)
    # fracionar: vende fora do múltiplo abrindo uma embalagem; o que sobra dela vira estoque avulso
    fracionavel = db.Column(db.Boolean, default=False, nullable=False)
    acrescimo_fracionado = db.Column(db.Numeric(5, 2), default=0, nullable=False)  # % sobre a unidade fracionada
    estoque_avulso = db.Column(db.Integer, default=0, nullable=False)  # parte do stock que está fora da embalagem
    peso_kg = db.Column(db.Numeric(10, 3), default=0, nullable=False)  # por unidade, para o frete
    estoque_alerta = db.Column(db.Integer, default=0, nullable=False)  # avisa quando o estoque chega aqui (0 = não avisa)
    # medidas da embalagem de uma unidade, em cm, para a cotação de frete (vazio = a caixa padrão da configuração)
    altura_cm = db.Column(db.Numeric(6, 1))
    largura_cm = db.Column(db.Numeric(6, 1))
    comprimento_cm = db.Column(db.Numeric(6, 1))
    # promoção com data: no período, o cliente paga o menor entre o preço dele e o promocional (não acumula)
    preco_promocional = db.Column(db.Numeric(10, 2))
    promo_inicio = db.Column(db.Date)  # vazio = já vale
    promo_fim = db.Column(db.Date)  # último dia (vazio = até tirar)
    # formação de preço: o preço sai do custo do fornecedor + impostos do estado + markup (e muda por estado)
    preco_auto = db.Column(db.Boolean, default=False, nullable=False)
    markup = db.Column(db.Numeric(6, 2))  # vazio = o da categoria ou o padrão da loja
    custo_medio = db.Column(db.Numeric(12, 4))  # custo médio das entradas de compra (vazio = ainda sem compra)
    # dados fiscais (NF-e)
    ncm = db.Column(db.String(8), default="")
    cest = db.Column(db.String(7), default="")
    origem = db.Column(db.Integer, default=0, nullable=False)  # 0 = nacional (tabela de origem da mercadoria)
    unidade = db.Column(db.String(6), default="UN", nullable=False)
    ean = db.Column(db.String(14), default="")  # código de barras (vazio = SEM GTIN)
    perfil_fiscal_id = db.Column(db.Integer, db.ForeignKey("perfil_fiscal.id"))  # vazio = a regra padrão
    category = db.relationship("Category", backref="products")
    faixas = db.relationship("FaixaPreco", cascade="all, delete-orphan", order_by="FaixaPreco.qtd_minima")
    fotos = db.relationship("ProdutoFoto", cascade="all, delete-orphan", order_by="(ProdutoFoto.ordem, ProdutoFoto.id)")
    custos = db.relationship("ProdutoFornecedor", cascade="all, delete-orphan", back_populates="product",
                             order_by="ProdutoFornecedor.id")


class Fornecedor(db.Model):
    """Quem vende para a loja: contato, como a mercadoria chega e os créditos de impostos da compra."""
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(160), nullable=False)
    documento = db.Column(db.String(14), default="")  # CNPJ ou CPF, só dígitos
    uf = db.Column(db.String(2), default="")
    contato = db.Column(db.String(120), default="")
    telefone = db.Column(db.String(30), default="")
    email = db.Column(db.String(120), default="")
    observacoes = db.Column(db.Text, default="")
    # transporte da compra: fornecedor (ele entrega) | transportadora (paga pela loja) | proprio (veículo próprio)
    transporte = db.Column(db.String(15), default="fornecedor", nullable=False)
    frete_pct = db.Column(db.Numeric(5, 2), default=0, nullable=False)  # % sobre a compra (fornecedor/transportadora)
    custo_viagem = db.Column(db.Numeric(10, 2), default=0, nullable=False)  # veículo próprio: custo de cada viagem
    compra_viagem = db.Column(db.Numeric(12, 2), default=0, nullable=False)  # veículo próprio: valor trazido por viagem
    credito_pct = db.Column(db.Numeric(5, 2), default=0, nullable=False)  # créditos de impostos na compra (0 no Simples)
    ativo = db.Column(db.Boolean, default=True, nullable=False)
    criado_em = db.Column(db.DateTime, default=agora_utc)
    precos = db.relationship("ProdutoFornecedor", cascade="all, delete-orphan", back_populates="fornecedor")


class ProdutoFornecedor(db.Model):
    """Preço de compra de um produto num fornecedor (por unidade vendida na loja)."""
    __tablename__ = "produto_fornecedor"
    __table_args__ = (db.UniqueConstraint("product_id", "fornecedor_id", name="uq_produto_fornecedor"),)
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    fornecedor_id = db.Column(db.Integer, db.ForeignKey("fornecedor.id"), nullable=False, index=True)
    codigo = db.Column(db.String(60), default="")  # código do produto no fornecedor
    preco_compra = db.Column(db.Numeric(12, 4), nullable=False)
    ipi_pct = db.Column(db.Numeric(5, 2), default=0, nullable=False)
    st_pct = db.Column(db.Numeric(5, 2), default=0, nullable=False)  # ICMS-ST pago na compra
    principal = db.Column(db.Boolean, default=False, nullable=False)  # o preço usado no cálculo
    fator = db.Column(db.Integer, default=1, nullable=False)  # unidades da loja em cada unidade da nota (caixa com 10 = 10)
    atualizado_em = db.Column(db.DateTime, default=agora_utc, onupdate=agora_utc)
    product = db.relationship("Product", back_populates="custos")
    fornecedor = db.relationship("Fornecedor", back_populates="precos")


class MovimentoEstoque(db.Model):
    """Cada entrada ou saída de estoque: venda, cancelamento, compra, ajuste, inventário (o "kardex")."""
    __tablename__ = "movimento_estoque"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    quando = db.Column(db.DateTime, default=agora_utc, nullable=False, index=True)
    tipo = db.Column(db.String(20), nullable=False)  # venda | cancelamento | pedido | compra | ajuste | inventario | cadastro
    quantidade = db.Column(db.Integer, nullable=False)  # + entrou, - saiu
    saldo = db.Column(db.Integer)  # estoque depois do movimento
    custo_unitario = db.Column(db.Numeric(12, 4))  # entradas de compra
    order_id = db.Column(db.Integer, index=True)  # sem chave estrangeira: o histórico fica mesmo se o pedido for excluído
    compra_id = db.Column(db.Integer, index=True)
    autor = db.Column(db.String(120), default="")
    observacao = db.Column(db.String(200), default="")
    produto = db.relationship("Product")


class Compra(db.Model):
    """Pedido de compra ao fornecedor: rascunho -> enviado -> recebido (ou cancelado)."""
    id = db.Column(db.Integer, primary_key=True)
    fornecedor_id = db.Column(db.Integer, db.ForeignKey("fornecedor.id"), nullable=False, index=True)
    status = db.Column(db.String(10), default="rascunho", nullable=False)
    criado_em = db.Column(db.DateTime, default=agora_utc, nullable=False)
    enviado_em = db.Column(db.DateTime)
    recebido_em = db.Column(db.DateTime)
    previsao = db.Column(db.Date)  # previsão de entrega
    condicao = db.Column(db.String(60), default="")  # prazo de pagamento, ex.: "28" ou "30/60/90"
    frete = db.Column(db.Numeric(10, 2), default=0, nullable=False)  # frete pago à parte nesta compra (R$)
    nota = db.Column(db.String(60), default="")  # número da nota fiscal do fornecedor
    observacoes = db.Column(db.Text, default="")
    autor = db.Column(db.String(120), default="")
    fornecedor = db.relationship("Fornecedor")
    itens = db.relationship("CompraItem", cascade="all, delete-orphan", order_by="CompraItem.id", back_populates="compra")


class CompraItem(db.Model):
    __tablename__ = "compra_item"
    id = db.Column(db.Integer, primary_key=True)
    compra_id = db.Column(db.Integer, db.ForeignKey("compra.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    quantidade = db.Column(db.Integer, nullable=False)
    preco = db.Column(db.Numeric(12, 4), nullable=False)  # preço de compra de cada unidade
    ipi_pct = db.Column(db.Numeric(5, 2), default=0, nullable=False)
    st_pct = db.Column(db.Numeric(5, 2), default=0, nullable=False)
    recebido = db.Column(db.Integer)  # quantidade que chegou (vazio = ainda não recebido)
    compra = db.relationship("Compra", back_populates="itens")
    product = db.relationship("Product")


class Lancamento(db.Model):
    """Conta a receber ou a pagar. As de pedidos são criadas e baixadas sozinhas (financeiro.py)."""
    id = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(7), nullable=False)  # receber | pagar
    descricao = db.Column(db.String(160), default="")
    categoria = db.Column(db.String(40), default="")
    valor = db.Column(db.Numeric(12, 2), nullable=False)
    vencimento = db.Column(db.Date, nullable=False, index=True)
    pago_em = db.Column(db.Date, index=True)  # vazio = em aberto
    valor_pago = db.Column(db.Numeric(12, 2))
    forma = db.Column(db.String(20), default="")
    parcela = db.Column(db.String(10), default="")  # ex.: "2/3"
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), index=True)
    compra_id = db.Column(db.Integer, db.ForeignKey("compra.id"), index=True)
    fornecedor_id = db.Column(db.Integer, db.ForeignKey("fornecedor.id"), index=True)
    observacao = db.Column(db.String(300), default="")
    criado_em = db.Column(db.DateTime, default=agora_utc)
    # boleto registrado pela Efí (baixa automática quando o cliente paga)
    boleto_id = db.Column(db.String(30), index=True)
    boleto_link = db.Column(db.String(300), default="")
    boleto_linha = db.Column(db.String(60), default="")
    order = db.relationship("Order", back_populates="lancamentos")
    compra = db.relationship("Compra")
    fornecedor = db.relationship("Fornecedor")


class PerfilFiscal(db.Model):
    """Regra fiscal (como o contador mandar): CFOP, situação do ICMS, IPI, PIS e COFINS de um grupo de produtos."""
    __tablename__ = "perfil_fiscal"
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(60), nullable=False)
    padrao = db.Column(db.Boolean, default=False, nullable=False)
    cfop_estadual = db.Column(db.String(4), default="5102", nullable=False)
    cfop_interestadual = db.Column(db.String(4), default="6102", nullable=False)
    icms_situacao = db.Column(db.String(3), default="102", nullable=False)  # CSOSN (Simples) ou CST (regime normal)
    icms_aliquota = db.Column(db.Numeric(5, 2))  # vazio = a do estado na Formação de preço
    credito_sn = db.Column(db.Numeric(5, 2), default=0, nullable=False)  # CSOSN 101: % de crédito do Simples
    ipi_cst = db.Column(db.String(2), default="", nullable=False)  # vazio = sem IPI (comércio não equiparado)
    ipi_aliquota = db.Column(db.Numeric(5, 2), default=0, nullable=False)
    ipi_enquadramento = db.Column(db.String(3), default="999", nullable=False)
    pis_cst = db.Column(db.String(2), default="99", nullable=False)
    pis_aliquota = db.Column(db.Numeric(5, 2), default=0, nullable=False)
    cofins_cst = db.Column(db.String(2), default="99", nullable=False)
    cofins_aliquota = db.Column(db.Numeric(5, 2), default=0, nullable=False)
    info_adicional = db.Column(db.String(500), default="")  # vai nas informações complementares da nota


class NotaFiscal(db.Model):
    """NF-e (modelo 55) emitida pela loja. O XML autorizado fica guardado (o contador pede todo mês)."""
    __tablename__ = "nota_fiscal"
    __table_args__ = (db.UniqueConstraint("ambiente", "serie", "numero", name="uq_nota_numero"),)
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), index=True)
    ambiente = db.Column(db.Integer, nullable=False)  # 1 = produção | 2 = homologação (testes, sem valor fiscal)
    serie = db.Column(db.Integer, nullable=False)
    numero = db.Column(db.Integer, nullable=False)
    chave = db.Column(db.String(44), default="", index=True)
    status = db.Column(db.String(12), default="processando", nullable=False)
    # processando | autorizada | rejeitada | denegada | cancelada | erro | inutilizada
    cstat = db.Column(db.String(4), default="")
    motivo = db.Column(db.String(500), default="")
    protocolo = db.Column(db.String(20), default="")
    xml = db.deferred(db.Column(db.Text, default=""))  # autorizado (nfeProc) ou o último enviado
    valor = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    destinatario = db.Column(db.String(160), default="")
    provedor = db.Column(db.String(20), default="")
    autor = db.Column(db.String(120), default="")
    criado_em = db.Column(db.DateTime, default=agora_utc, nullable=False, index=True)
    autorizada_em = db.Column(db.DateTime)
    cancelada_em = db.Column(db.DateTime)
    order = db.relationship("Order", back_populates="notas")
    eventos = db.relationship("NotaEvento", cascade="all, delete-orphan", order_by="NotaEvento.id",
                              back_populates="nota")


class NotaEvento(db.Model):
    """Cancelamento, carta de correção ou inutilização de numeração registrados na SEFAZ."""
    __tablename__ = "nota_evento"
    id = db.Column(db.Integer, primary_key=True)
    nota_id = db.Column(db.Integer, db.ForeignKey("nota_fiscal.id"), index=True)  # vazio = inutilização avulsa
    tipo = db.Column(db.String(15), nullable=False)  # cancelamento | correcao | inutilizacao
    sequencia = db.Column(db.Integer, default=1, nullable=False)
    texto = db.Column(db.String(1000), default="")
    ok = db.Column(db.Boolean, default=False, nullable=False)
    cstat = db.Column(db.String(4), default="")
    motivo = db.Column(db.String(500), default="")
    protocolo = db.Column(db.String(20), default="")
    xml = db.deferred(db.Column(db.Text, default=""))
    quando = db.Column(db.DateTime, default=agora_utc, nullable=False)
    autor = db.Column(db.String(120), default="")
    nota = db.relationship("NotaFiscal", back_populates="eventos")


class NotaRecebida(db.Model):
    """NF-e de compra emitida por um fornecedor para o CNPJ da loja (buscada na SEFAZ ou enviada em XML)."""
    __tablename__ = "nota_recebida"
    id = db.Column(db.Integer, primary_key=True)
    chave = db.Column(db.String(44), unique=True, nullable=False)
    nsu = db.Column(db.String(15), default="")
    tipo = db.Column(db.String(10), default="resumo", nullable=False)  # resumo (sem itens) | completa (com o XML)
    situacao = db.Column(db.String(12), default="autorizada", nullable=False)  # autorizada | cancelada | denegada
    cnpj_emitente = db.Column(db.String(14), default="")
    nome_emitente = db.Column(db.String(160), default="")
    numero = db.Column(db.Integer)
    serie = db.Column(db.Integer)
    valor = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    emitida_em = db.Column(db.DateTime)
    manifestacao = db.Column(db.String(12), default="")  # "" | ciencia
    origem = db.Column(db.String(10), default="sefaz", nullable=False)  # sefaz | arquivo
    xml = db.deferred(db.Column(db.Text, default=""))
    compra_id = db.Column(db.Integer, db.ForeignKey("compra.id"))
    recebida_em = db.Column(db.DateTime, default=agora_utc, nullable=False, index=True)
    compra = db.relationship("Compra")


class ImpostoUF(db.Model):
    """Impostos e entrega da venda para cada estado (formação de preço)."""
    __tablename__ = "imposto_uf"
    uf = db.Column(db.String(2), primary_key=True)
    icms = db.Column(db.Numeric(5, 2))  # vazio = o mesmo do estado da loja
    adicional = db.Column(db.Numeric(5, 2))  # DIFAL, FCP, ST na venda (vazio = o mesmo do estado da loja)
    entrega = db.Column(db.String(15), default="transportadora", nullable=False)  # transportadora | propria
    custo_entrega = db.Column(db.Numeric(5, 2), default=0, nullable=False)  # frota própria: % sobre a venda


class Percentual(db.Model):
    """Percentual cadastrado pela loja na formação de preço: um imposto (PIS, COFINS, IPI...) ou uma despesa
    (comissão, taxa de cartão, embalagem, margem extra) que vale para todos os produtos, uma categoria ou um produto,
    e opcionalmente só para um estado de entrega.

    base "venda": % sobre o preço de venda (soma com o ICMS e os impostos da venda);
    base "custo": % sobre o preço de compra (soma no custo, como o IPI).
    Mesmo nome em mais de um nível: vale o mais específico (produto > categoria > todos; com estado > sem estado).
    Assim dá para ter "PIS 1,65%" para todos e "PIS 0%" num produto monofásico.
    """
    __tablename__ = "percentual"
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(60), nullable=False)
    base = db.Column(db.String(5), default="venda", nullable=False)  # venda | custo
    valor = db.Column(db.Numeric(6, 2), nullable=False)  # %
    escopo = db.Column(db.String(10), default="todos", nullable=False)  # todos | categoria | produto
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), index=True)
    uf = db.Column(db.String(2), default="", nullable=False)  # vazio = todos os estados (só na base venda)
    ativo = db.Column(db.Boolean, default=True, nullable=False)
    observacao = db.Column(db.String(200), default="")
    criado_em = db.Column(db.DateTime, default=agora_utc)
    categoria = db.relationship("Category", backref=db.backref("percentuais", cascade="all, delete-orphan"))
    produto = db.relationship("Product", backref=db.backref("percentuais", cascade="all, delete-orphan"))


class ProdutoFoto(db.Model):
    """Foto do produto enviada pelo painel (no banco: o disco do Render gratuito é apagado a cada deploy)."""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    dados = db.Column(db.LargeBinary, nullable=False)
    tipo = db.Column(db.String(30), nullable=False)
    ordem = db.Column(db.Integer, default=0, nullable=False)
    versao = db.Column(db.Integer, default=1, nullable=False)


class FaixaPreco(db.Model):
    """Preço por quantidade: a partir de qtd_minima unidades, cada uma sai por preco."""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    qtd_minima = db.Column(db.Integer, nullable=False)
    preco = db.Column(db.Numeric(10, 2), nullable=False)


class ImportacaoPendente(db.Model):
    """Planilha enviada no painel, aguardando a confirmação depois da prévia."""
    id = db.Column(db.Integer, primary_key=True)
    criado_em = db.Column(db.DateTime, default=agora_utc)
    nome_arquivo = db.Column(db.String(200), default="")
    conteudo = db.Column(db.LargeBinary, nullable=False)
    desativar_ausentes = db.Column(db.Boolean, default=False, nullable=False)


class Banner(db.Model):
    """Banner do carrossel da página inicial (produto em destaque ou imagem pronta)."""
    id = db.Column(db.Integer, primary_key=True)
    titulo = db.Column(db.String(120), default="")
    subtitulo = db.Column(db.String(200), default="")
    texto_botao = db.Column(db.String(40), default="")
    link = db.Column(db.String(500), default="")
    product_id = db.Column(db.Integer, db.ForeignKey("product.id", ondelete="SET NULL"))
    imagem = db.Column(db.LargeBinary)  # guardada no banco: o disco do Render gratuito é apagado a cada deploy
    imagem_tipo = db.Column(db.String(30), default="")
    imagem_url = db.Column(db.String(500), default="")
    cor = db.Column(db.String(20), default="azul")
    ordem = db.Column(db.Integer, default=0, nullable=False)
    ativo = db.Column(db.Boolean, default=True, nullable=False)
    versao = db.Column(db.Integer, default=1, nullable=False)  # muda quando a imagem muda (limpa o cache)
    product = db.relationship("Product")


class Pergunta(db.Model):
    """Pergunta frequente da ajuda do site (sem IA)."""
    id = db.Column(db.Integer, primary_key=True)
    pergunta = db.Column(db.String(200), nullable=False)
    resposta = db.Column(db.Text, nullable=False)
    ordem = db.Column(db.Integer, default=0, nullable=False)
    ativo = db.Column(db.Boolean, default=True, nullable=False)


class RegraFrete(db.Model):
    """Regra da tabela de frete: por faixa de CEP, por estados (UFs) ou padrão (sem os dois)."""
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(80), nullable=False)
    ufs = db.Column(db.String(120), default="")  # "SP,RJ"
    cep_de = db.Column(db.Integer)
    cep_ate = db.Column(db.Integer)
    valor = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    valor_kg = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    gratis_acima = db.Column(db.Numeric(12, 2))
    prazo = db.Column(db.String(60), default="")
    ordem = db.Column(db.Integer, default=0, nullable=False)
    ativo = db.Column(db.Boolean, default=True, nullable=False)


class Configuracao(db.Model):
    chave = db.Column(db.String(40), primary_key=True)
    valor = db.Column(db.String(200), default="")


class Cupom(db.Model):
    """Cupom de desconto digitado no carrinho. O desconto vale sobre os produtos (o frete não entra)."""
    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(30), unique=True, nullable=False)  # sempre em maiúsculas, sem espaços
    tipo = db.Column(db.String(10), default="percentual", nullable=False)  # percentual | valor
    valor = db.Column(db.Numeric(10, 2), nullable=False)  # % ou R$, conforme o tipo
    minimo = db.Column(db.Numeric(12, 2), default=0, nullable=False)  # valor mínimo dos produtos
    validade = db.Column(db.Date)  # último dia de uso (vazio = sem prazo)
    limite_usos = db.Column(db.Integer)  # pedidos no total (vazio = sem limite)
    um_por_cliente = db.Column(db.Boolean, default=True, nullable=False)
    ativo = db.Column(db.Boolean, default=True, nullable=False)
    criado_em = db.Column(db.DateTime, default=agora_utc)


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(120), nullable=False)
    customer_email = db.Column(db.String(120), default="")
    customer_phone = db.Column(db.String(30), default="")
    address = db.Column(db.String(300), default="")
    payment_method = db.Column(db.String(20), default="")
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)  # cliente logado que fez o pedido
    status = db.Column(db.String(20), default="pendente", nullable=False)  # orcamento | pendente | pago | cancelado
    documento = db.Column(db.String(14), default="")  # CPF/CNPJ do comprador na data do pedido (para a nota)
    razao_social = db.Column(db.String(160), default="")
    inscricao_estadual = db.Column(db.String(30), default="")
    prazo = db.Column(db.String(60), default="")  # faturado: ex. "30/60/90" dias
    # Pix pela Efí: cobrança e quem pagou (para conferir com o cadastro)
    pix_txid = db.Column(db.String(35), default="")
    pix_copia_cola = db.Column(db.Text, default="")
    pagador_nome = db.Column(db.String(160), default="")
    pagador_documento = db.Column(db.String(20), default="")
    pagador_confere = db.Column(db.Boolean)  # None = ainda não pago pela Efí
    cartao_id = db.Column(db.String(30), index=True)  # cobrança do link de pagamento com cartão na Efí
    cartao_link = db.Column(db.String(300))
    cartao_situacao = db.Column(db.String(15))
    veio_do_lembrete = db.Column(db.Boolean, default=False, nullable=False)  # fechado depois do lembrete do carrinho

    @property
    def cartao_aberto(self):
        """Tem link de pagamento com cartão valendo (com o valor atual do pedido)?"""
        from cartao_efi import RECUSADO
        return bool(self.cartao_id) and self.cartao_situacao not in RECUSADO
    # frete (o total do pedido já inclui o frete)
    cep = db.Column(db.String(8), default="")
    frete = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    frete_prazo = db.Column(db.String(60), default="")
    frete_combinar = db.Column(db.Boolean, default=False, nullable=False)  # sem regra para o CEP: combinar depois
    frete_servico = db.Column(db.String(80), default="")  # ex.: "Correios SEDEX" ou o nome da regra da tabela
    frete_codigo = db.Column(db.String(20), default="")  # tabela | propria | me:<id do serviço no Melhor Envio>
    # cupom de desconto (o total já desconta): total = subtotal - desconto + frete
    cupom = db.Column(db.String(30), default="", index=True)
    desconto = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    # quem da empresa fez o pedido (usuário da equipe); vazio = o próprio titular da conta
    comprador_id = db.Column(db.Integer)
    comprador_nome = db.Column(db.String(120), default="")
    # entrega (separada do pagamento: o boleto faturado sai antes de ser pago)
    envio = db.Column(db.String(10), default="", nullable=False)  # "" | enviado | entregue
    transportadora = db.Column(db.String(80), default="")
    rastreio = db.Column(db.String(80), default="")
    rastreio_link = db.Column(db.String(300), default="")
    enviado_em = db.Column(db.DateTime)
    entregue_em = db.Column(db.DateTime)

    @property
    def subtotal(self):
        return sum((i.unit_price * i.quantity for i in self.items), 0)
    total = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=agora_utc, index=True)
    items = db.relationship("OrderItem", backref="order", cascade="all, delete-orphan")
    arquivos = db.relationship("PedidoArquivo", cascade="all, delete-orphan", order_by="PedidoArquivo.id",
                               lazy="select")
    eventos = db.relationship("PedidoEvento", cascade="all, delete-orphan",
                              order_by="(PedidoEvento.quando, PedidoEvento.id)")
    lancamentos = db.relationship("Lancamento", cascade="all, delete-orphan", back_populates="order",
                                  order_by="(Lancamento.vencimento, Lancamento.id)")
    devolucoes = db.relationship("Devolucao", back_populates="order", cascade="all, delete-orphan",
                                 order_by="Devolucao.id")
    notas = db.relationship("NotaFiscal", back_populates="order", order_by="NotaFiscal.id")
    etiquetas = db.relationship("EtiquetaEnvio", cascade="all, delete-orphan", order_by="EtiquetaEnvio.id")


class EtiquetaEnvio(db.Model):
    """Etiqueta comprada no Melhor Envio para um pedido (Correios: uma por volume).

    situacao: carrinho (criada, ainda não paga) | paga | gerada (pronta para imprimir) | postada | entregue |
              cancelada. Se uma etapa falha, a etiqueta fica onde estava e a mensagem vai em `erro`."""
    __tablename__ = "etiqueta_envio"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False, index=True)
    me_id = db.Column(db.String(40), nullable=False, index=True)  # id da etiqueta no Melhor Envio
    protocolo = db.Column(db.String(40), default="")
    servico = db.Column(db.String(80), default="")  # ex.: "Correios SEDEX"
    valor = db.Column(db.Numeric(10, 2), default=0, nullable=False)  # o que a loja pagou pela etiqueta
    situacao = db.Column(db.String(10), default="carrinho", nullable=False)
    rastreio = db.Column(db.String(40), default="")
    link_impressao = db.Column(db.String(300), default="")
    sandbox = db.Column(db.Boolean, default=False, nullable=False)  # comprada no ambiente de testes
    erro = db.Column(db.String(500), default="")
    autor = db.Column(db.String(120), default="")
    criado_em = db.Column(db.DateTime, default=agora_utc, nullable=False)
    atualizado_em = db.Column(db.DateTime, default=agora_utc, onupdate=agora_utc)


class PedidoEvento(db.Model):
    """Uma linha do histórico do pedido: o que aconteceu, quando e quem fez."""
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False, index=True)
    quando = db.Column(db.DateTime, default=agora_utc, nullable=False)
    autor = db.Column(db.String(10), default="loja", nullable=False)  # loja | cliente | sistema
    autor_nome = db.Column(db.String(120), default="")
    texto = db.Column(db.String(500), nullable=False)


class PedidoArquivo(db.Model):
    """Nota fiscal (PDF/XML), boleto ou outro documento anexado ao pedido pela loja. No banco, como as fotos."""
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False, index=True)
    tipo = db.Column(db.String(10), default="outro", nullable=False)  # nota | xml | boleto | outro
    nome = db.Column(db.String(120), nullable=False)
    mime = db.Column(db.String(60), nullable=False)
    tamanho = db.Column(db.Integer, default=0, nullable=False)
    dados = db.deferred(db.Column(db.LargeBinary, nullable=False))  # só carrega ao baixar
    criado_em = db.Column(db.DateTime, default=agora_utc)


class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    fracionado = db.Column(db.Boolean, default=False, nullable=False)  # unidades vendidas fora da embalagem
    custo_unitario = db.Column(db.Numeric(12, 4))  # custo na hora da venda (para o lucro); vazio = sem custo
    product = db.relationship("Product")


class LimiteUso(db.Model):
    """Uso recente de algo limitado (e-mails, SMS, consultas, chat). No banco: vale para todos os workers e não
    zera quando o servidor reinicia. A chave é guardada só como hash (não guarda e-mail nem IP)."""
    id = db.Column(db.Integer, primary_key=True)
    chave = db.Column(db.String(64), nullable=False, index=True)
    momento = db.Column(db.BigInteger, nullable=False, index=True)  # epoch em segundos


class LoginFalha(db.Model):
    """Tentativas de login erradas por IP + e-mail (guardado só o hash). No banco, vale para todos os workers."""
    chave = db.Column(db.String(64), primary_key=True)
    falhas = db.Column(db.Integer, default=0, nullable=False)
    ultima = db.Column(db.BigInteger, default=0, nullable=False)  # epoch em segundos
    bloqueado_ate = db.Column(db.BigInteger, default=0, nullable=False)


def novo_avulso(estoque, avulso, multiplo, fechadas, fracionadas):
    """Quantas unidades ficam avulsas depois de tirar `fechadas` (em embalagens) e `fracionadas` (soltas).

    As embalagens saem das fechadas; as soltas saem primeiro das avulsas e, se não der, abre-se uma
    embalagem: o resto dela vira avulso. Se as contas não fecharem (estoque mexido à mão), unidade é unidade."""
    m = max(multiplo or 1, 1)
    av, fechado = max(min(avulso, estoque), 0), estoque - max(min(avulso, estoque), 0)
    de_fechado = min(fechadas, fechado)
    fechado -= de_fechado
    av -= fechadas - de_fechado
    if fracionadas:
        if av >= fracionadas:
            av -= fracionadas
        elif fechado >= m:
            av, fechado = av + m - fracionadas, fechado - m
        else:
            av, fechado = av + fechado - fracionadas, 0
    return max(min(av, estoque - fechadas - fracionadas), 0)


def baixar_estoque(pid, qtd, fracionadas=0, tipo="venda", order_id=None):
    """Tira do estoque só se houver o bastante, num único UPDATE: dois compradores não levam a mesma última unidade.

    `fracionadas` é quantas das `qtd` unidades saem fora da embalagem (abre uma, se precisar). O movimento fica no
    histórico de estoque (`tipo` e o pedido)."""
    r = db.session.execute(update(Product).where(Product.id == pid, Product.stock >= qtd)
                           .values(stock=Product.stock - qtd).execution_options(synchronize_session=False))
    if r.rowcount != 1:
        return False
    # a linha já está travada pelo UPDATE até o fim da transação: dá para ajustar o avulso com segurança
    estoque, avulso, multiplo = db.session.execute(
        select(Product.stock, Product.estoque_avulso, Product.multiplo).where(Product.id == pid)).one()
    if avulso or fracionadas:
        novo = novo_avulso(estoque + qtd, avulso, multiplo, qtd - fracionadas, fracionadas)
        db.session.execute(update(Product).where(Product.id == pid).values(estoque_avulso=novo)
                           .execution_options(synchronize_session=False))
    if qtd:
        db.session.add(MovimentoEstoque(product_id=pid, tipo=tipo, quantidade=-qtd, saldo=estoque, order_id=order_id))
    return True


def devolver_estoque(pid, qtd, fracionadas=0, tipo="cancelamento", order_id=None):
    """Devolve ao estoque; as unidades que tinham saído fracionadas voltam como avulsas."""
    db.session.execute(update(Product).where(Product.id == pid)
                       .values(stock=Product.stock + qtd, estoque_avulso=Product.estoque_avulso + fracionadas)
                       .execution_options(synchronize_session=False))
    if qtd:
        saldo = db.session.execute(select(Product.stock).where(Product.id == pid)).scalar()
        db.session.add(MovimentoEstoque(product_id=pid, tipo=tipo, quantidade=qtd, saldo=saldo, order_id=order_id))


def itens_por_produto(itens):
    """[(product_id, quantidade, fracionado, nome)] -> {product_id: [total, fracionadas, nome]}"""
    total = {}
    for pid, qtd, frac, nome in itens:
        t = total.setdefault(pid, [0, 0, nome])
        t[0] += qtd
        t[1] += qtd if frac else 0
    return total


SEGURA_ESTOQUE = ("pendente", "pago")  # orçamento e cancelado não prendem estoque


def mudar_status(pedido, novo):
    """Troca o status e ajusta o estoque de forma atômica. Devolve (ok, mensagem_de_erro).

    O UPDATE só vale se o status ainda for o que lemos: se o cliente e o admin cancelarem
    ao mesmo tempo, só um deles devolve o estoque."""
    antigo = pedido.status
    itens = itens_por_produto((i.product_id, i.quantity, i.fracionado, i.product.name) for i in pedido.items)
    r = db.session.execute(update(Order).where(Order.id == pedido.id, Order.status == antigo)
                           .values(status=novo).execution_options(synchronize_session=False))
    if r.rowcount != 1:
        db.session.rollback()
        return False, "Este pedido acabou de ser alterado. Recarregue a página e confira."
    if antigo in SEGURA_ESTOQUE and novo not in SEGURA_ESTOQUE:
        for pid, (qtd, frac, _) in itens.items():
            devolver_estoque(pid, qtd, frac, "cancelamento", pedido.id)
    elif novo in SEGURA_ESTOQUE and antigo not in SEGURA_ESTOQUE:
        faltam = [nome for pid, (qtd, frac, nome) in itens.items()
                  if not baixar_estoque(pid, qtd, frac, "venda", pedido.id)]
        if faltam:
            db.session.rollback()
            return False, "Sem estoque para este pedido: " + ", ".join(faltam) + "."
    import financeiro  # contas a receber do pedido: cria, baixa ou cancela as parcelas
    financeiro.pedido_mudou(pedido, novo)
    db.session.commit()
    return True, None


def excluir_pedido(pedido):
    """Apaga o pedido de vez, com itens e documentos. Se ele segurava estoque, devolve antes (como cancelar).

    Devolve (ok, mensagem_de_erro)."""
    if any(n.status == "autorizada" and n.ambiente == 1 for n in pedido.notas):
        return False, f"o pedido #{pedido.id} tem nota fiscal autorizada (cancele a nota antes)"
    if pedido.status in SEGURA_ESTOQUE:
        ok, erro = mudar_status(pedido, "cancelado")  # atômico: dois cliques não devolvem o estoque duas vezes
        if not ok:
            return False, erro
    for n in list(pedido.notas):  # a nota (rejeitada, cancelada...) fica no histórico fiscal, sem o pedido
        n.order_id = None
    db.session.delete(pedido)
    db.session.commit()
    return True, None
