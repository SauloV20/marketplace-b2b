import os
import random
import secrets

from sqlalchemy import inspect, text
from datetime import timedelta
from decimal import Decimal

import click
from flask import Flask

from config import Config
from models import Category, FaixaPreco, Order, OrderItem, Product, ProdutoFornecedor, User, agora_utc, db
from security import init_security
import estoque  # noqa: F401  (registra o histórico de estoque das mudanças pelo cadastro)
import lucro  # noqa: F401  (registra o custo de cada item vendido)
from seed_data import CATEGORIAS, CODIGOS, PRODUTOS, REGRAS_ATACADO


def carregar_catalogo_exemplo():
    """Cria categorias, produtos e pedidos de exemplo a partir de seed_data.py."""
    cats = [Category(name=n) for n in CATEGORIAS]
    db.session.add_all(cats)
    db.session.flush()
    prods = [Product(name=n, description="Produto de exemplo.", price=Decimal(p),
                     stock=random.randint(10, 200), category_id=cats[c].id) for n, p, c in PRODUTOS]
    db.session.add_all(prods)
    db.session.flush()
    for p in prods:  # exemplos de venda em atacado (mínimo, múltiplo e preço por quantidade)
        p.codigo = CODIGOS.get(p.name)
        regra = REGRAS_ATACADO.get(p.name, {})
        p.qtd_minima, p.multiplo = regra.get("minimo", 1), regra.get("multiplo", 1)
        if "fraciona" in regra:  # vende fora da embalagem com este acréscimo (%)
            p.fracionavel, p.acrescimo_fracionado = True, Decimal(regra["fraciona"])
        for qtd, preco in regra.get("faixas", []):
            p.faixas.append(FaixaPreco(qtd_minima=qtd, preco=Decimal(preco)))
    for _ in range(80):
        quando = agora_utc() - timedelta(days=random.randint(0, 29), hours=random.randint(0, 23))
        pedido = Order(customer_name="Cliente demo", created_at=quando, total=Decimal(0), status="pago")
        for p in random.sample(prods, random.randint(1, 3)):
            qtd = random.randint(1, 3)
            pedido.items.append(OrderItem(product_id=p.id, quantity=qtd, unit_price=p.price))
            pedido.total += p.price * qtd
        db.session.add(pedido)
    db.session.commit()


def atualizar_esquema():
    """Adiciona as colunas novas em bancos já existentes (create_all não altera tabelas prontas)."""
    q = db.engine.dialect.identifier_preparer.quote
    inspetor = inspect(db.engine)
    novas = [("user", "phone", "VARCHAR(30)", None), ("user", "address", "VARCHAR(300)", None),
             ("order", "user_id", f"INTEGER REFERENCES {q('user')} (id)", None),
             # contas que já existiam antes da confirmação de e-mail ficam liberadas
             ("user", "email_verified", "BOOLEAN NOT NULL DEFAULT FALSE",
              f"UPDATE {q('user')} SET email_verified = TRUE"),
             # vendas para empresas
             ("user", "tipo", "VARCHAR(2) NOT NULL DEFAULT 'pf'", None),
             ("user", "documento", "VARCHAR(14)", None),
             ("user", "razao_social", "VARCHAR(160) DEFAULT ''", None),
             ("user", "inscricao_estadual", "VARCHAR(30) DEFAULT ''", None),
             ("user", "empresa_aprovada", "BOOLEAN NOT NULL DEFAULT FALSE", None),
             ("user", "desconto", "NUMERIC(5,2) NOT NULL DEFAULT 0", None),
             ("user", "prazos", "VARCHAR(120) DEFAULT ''", None),
             ("user", "receita_situacao", "VARCHAR(40) DEFAULT ''", None),
             ("user", "receita_dados", "TEXT", None),
             ("user", "receita_consultado_em", "TIMESTAMP", None),
             ("user", "telefone_verificado", "BOOLEAN NOT NULL DEFAULT FALSE", None),
             ("user", "termos_aceitos_em", "TIMESTAMP", None),
             ("user", "termos_versao", "VARCHAR(20) DEFAULT ''", None),
             ("order", "pix_txid", "VARCHAR(35) DEFAULT ''", None),
             ("order", "pix_copia_cola", "TEXT", None),
             ("order", "pagador_nome", "VARCHAR(160) DEFAULT ''", None),
             ("order", "pagador_documento", "VARCHAR(20) DEFAULT ''", None),
             ("order", "pagador_confere", "BOOLEAN", None),
             ("order", "cartao_id", "VARCHAR(30)", None),
             ("order", "cartao_link", "VARCHAR(300)", None),
             ("order", "cartao_situacao", "VARCHAR(15)", None),
             ("order", "veio_do_lembrete", "BOOLEAN NOT NULL DEFAULT FALSE", None),
             ("user", "lembrete_carrinho", "BOOLEAN NOT NULL DEFAULT TRUE", None),
             ("product", "codigo", "VARCHAR(40)", None),
             ("product", "qtd_minima", "INTEGER NOT NULL DEFAULT 1", None),
             ("product", "multiplo", "INTEGER NOT NULL DEFAULT 1", None),
             ("product", "fracionavel", "BOOLEAN NOT NULL DEFAULT FALSE", None),
             ("product", "acrescimo_fracionado", "NUMERIC(5,2) NOT NULL DEFAULT 0", None),
             ("product", "estoque_avulso", "INTEGER NOT NULL DEFAULT 0", None),
             ("product", "peso_kg", "NUMERIC(10,3) NOT NULL DEFAULT 0", None),
             ("order", "cep", "VARCHAR(8) DEFAULT ''", None),
             ("order", "frete", "NUMERIC(10,2) NOT NULL DEFAULT 0", None),
             ("order", "frete_prazo", "VARCHAR(60) DEFAULT ''", None),
             ("order", "frete_combinar", "BOOLEAN NOT NULL DEFAULT FALSE", None),
             ("order", "frete_servico", "VARCHAR(80) DEFAULT ''", None),
             ("order", "frete_codigo", "VARCHAR(20) DEFAULT ''", None),
             ("product", "altura_cm", "NUMERIC(6,1)", None),
             ("product", "largura_cm", "NUMERIC(6,1)", None),
             ("product", "comprimento_cm", "NUMERIC(6,1)", None),
             ("order_item", "fracionado", "BOOLEAN NOT NULL DEFAULT FALSE", None),
             ("order", "documento", "VARCHAR(14) DEFAULT ''", None),
             ("order", "razao_social", "VARCHAR(160) DEFAULT ''", None),
             ("order", "inscricao_estadual", "VARCHAR(30) DEFAULT ''", None),
             ("order", "prazo", "VARCHAR(60) DEFAULT ''", None),
             ("order", "cupom", "VARCHAR(30) DEFAULT ''", None),
             ("order", "desconto", "NUMERIC(10,2) NOT NULL DEFAULT 0", None),
             ("product", "estoque_alerta", "INTEGER NOT NULL DEFAULT 0", None),
             ("order", "envio", "VARCHAR(10) NOT NULL DEFAULT ''", None),
             ("order", "transportadora", "VARCHAR(80) DEFAULT ''", None),
             ("order", "rastreio", "VARCHAR(80) DEFAULT ''", None),
             ("order", "rastreio_link", "VARCHAR(300) DEFAULT ''", None),
             ("order", "enviado_em", "TIMESTAMP NULL", None),
             ("order", "entregue_em", "TIMESTAMP NULL", None),
             ("user", "totp_segredo", "VARCHAR(64) DEFAULT ''", None),
             ("user", "totp_ultimo", "BIGINT NOT NULL DEFAULT 0", None),
             ("user", "codigos_recuperacao", "TEXT", None),
             ("product", "preco_promocional", "NUMERIC(10,2)", None),
             ("product", "promo_inicio", "DATE", None),
             ("product", "promo_fim", "DATE", None),
             ("user", "permissoes", "VARCHAR(200) DEFAULT 'tudo'", None),
             ("user", "empresa_id", f"INTEGER REFERENCES {q('user')} (id)", None),
             ("user", "papel", "VARCHAR(10) DEFAULT ''", None),
             ("user", "limite_aprovacao", "NUMERIC(12,2)", None),
             ("order", "comprador_id", "INTEGER", None),
             ("order", "comprador_nome", "VARCHAR(120) DEFAULT ''", None),
             ("user", "limite_credito", "NUMERIC(12,2)", None),
             # formação de preço
             ("product", "preco_auto", "BOOLEAN NOT NULL DEFAULT FALSE", None),
             ("product", "markup", "NUMERIC(6,2)", None),
             ("category", "markup", "NUMERIC(6,2)", None),
             # ERP: custo médio e custo do item vendido (lucro)
             ("product", "custo_medio", "NUMERIC(12,4)", None),
             ("order_item", "custo_unitario", "NUMERIC(12,4)", None),
             # NF-e: dados fiscais do produto
             ("product", "ncm", "VARCHAR(8) DEFAULT ''", None),
             ("product", "cest", "VARCHAR(7) DEFAULT ''", None),
             ("product", "origem", "INTEGER NOT NULL DEFAULT 0", None),
             ("product", "unidade", "VARCHAR(6) NOT NULL DEFAULT 'UN'", None),
             ("product", "ean", "VARCHAR(14) DEFAULT ''", None),
             ("product", "perfil_fiscal_id", "INTEGER", None),
             ("produto_fornecedor", "fator", "INTEGER NOT NULL DEFAULT 1", None),
             ("lancamento", "boleto_id", "VARCHAR(30)", None),
             ("lancamento", "boleto_link", "VARCHAR(300) DEFAULT ''", None),
             ("lancamento", "boleto_linha", "VARCHAR(60) DEFAULT ''", None)]
    # lê as colunas de todas as tabelas antes de alterar: inspecionar no meio pode desfazer o que já foi feito
    # (tabela que ainda não existe é criada inteira pelo create_all: não há coluna para acrescentar)
    tabelas = set(inspetor.get_table_names())
    existentes = {t: {c["name"] for c in inspetor.get_columns(t)} for t in {t for t, *_ in novas} if t in tabelas}
    for tabela, coluna, ddl, depois in novas:
        if tabela in existentes and coluna not in existentes[tabela]:
            db.session.execute(text(f"ALTER TABLE {q(tabela)} ADD COLUMN {coluna} {ddl}"))
            if depois:
                db.session.execute(text(depois))
    db.session.commit()


def _versionar_estaticos(app):
    """CSS e JS ficam guardados no navegador por 1 ano: cada página não precisa perguntar de novo ao servidor.
    O endereço leva ?v=<impressão do conteúdo>, então quando o arquivo muda o navegador baixa o novo."""
    import hashlib
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 365 * 24 * 3600
    versoes = {}

    def versao(nome):
        if nome not in versoes or app.debug:  # rodando local com debug: sempre relê
            caminho = os.path.join(app.static_folder, nome)
            try:
                with open(caminho, "rb") as f:
                    versoes[nome] = hashlib.sha256(f.read()).hexdigest()[:10]
            except OSError:
                versoes[nome] = None
        return versoes[nome]

    @app.url_defaults
    def _com_versao(endpoint, values):
        if endpoint == "static" and "filename" in values and "v" not in values:
            v = versao(values["filename"])
            if v:
                values["v"] = v


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    if os.getenv("RENDER"):  # atrás do proxy do Render: IP real do visitante e HTTPS corretos
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    db.init_app(app)
    init_security(app)
    _versionar_estaticos(app)

    @app.after_request
    def _conferir_espaco(resp):  # de vez em quando (a cada 6 h), depois de uma visita de verdade
        from flask import request
        if request.endpoint not in (None, "static", "tarefas.saude") and not app.config.get("TESTING"):
            import armazenamento
            armazenamento.checar_de_vez_em_quando()
        return resp
    import alertas
    alertas.ligar(app)  # e-mail para o dono quando o site dá erro (só no ar)
    import carrinho_abandonado
    carrinho_abandonado.ligar(app)  # o carrinho de quem está na conta fica guardado no banco
    import erros
    erros.ligar(app)  # páginas de erro com a cara da loja

    from blueprints.account import account
    from blueprints.admin import admin
    from blueprints.shop import shop
    from blueprints.tarefas import tarefas

    app.register_blueprint(shop)
    app.register_blueprint(admin, url_prefix=app.config["ADMIN_PATH"])
    app.register_blueprint(account)
    app.register_blueprint(tarefas)

    import formato  # filtros dos templates: dinheiro, números para campos e horário de Brasília
    for nome in ("brl", "num", "uma_casa", "duas_casas", "hora_br", "dia_br"):
        app.add_template_filter(getattr(formato, nome), nome)
    app.add_template_global(formato.tamanho, "tamanho")
    app.add_template_global(formato.num, "num")

    @app.cli.command("init-db")
    def init_db():
        """Cria as tabelas, o admin e o catálogo de exemplo."""
        db.create_all()
        atualizar_esquema()
        if User.query.first():
            return click.echo("Banco já inicializado.")
        senha = os.getenv("ADMIN_PASSWORD") or secrets.token_urlsafe(12)
        admin_user = User(name="Admin Marketplace B2B", email="admin@exemplo.com", is_admin=True, email_verified=True)
        admin_user.set_password(senha)
        db.session.add(admin_user)
        carregar_catalogo_exemplo()
        click.echo("Pronto! Admin: admin@exemplo.com" + ("" if os.getenv("ADMIN_PASSWORD") else f" | senha gerada (anote): {senha}"))

    @app.cli.command("trocar-catalogo")
    @click.confirmation_option(prompt="Isto APAGA todos os produtos, categorias, pedidos, compras (com as contas deles) e o histórico de estoque e carrega o catálogo de seed_data.py. Continuar?")
    def trocar_catalogo():
        """Substitui produtos, categorias e pedidos pelo catálogo de seed_data.py (o admin é mantido)."""
        from models import Avaliacao, Compra, CompraItem, Devolucao, Lancamento, MovimentoEstoque
        Lancamento.query.filter(db.or_(Lancamento.order_id.isnot(None), Lancamento.compra_id.isnot(None))) \
            .delete(synchronize_session=False)
        MovimentoEstoque.query.delete()
        Devolucao.query.delete()
        Avaliacao.query.delete()
        CompraItem.query.delete()
        Compra.query.delete()
        OrderItem.query.delete()
        ProdutoFornecedor.query.delete()
        FaixaPreco.query.delete()
        Order.query.delete()
        Product.query.delete()
        Category.query.delete()
        db.session.commit()
        carregar_catalogo_exemplo()
        click.echo("Catálogo trocado.")

    @app.cli.command("trocar-senha-admin")
    @click.option("--email", default="admin@exemplo.com", show_default=True)
    @click.password_option(prompt="Nova senha")
    def trocar_senha_admin(email, password):
        """Define uma nova senha para o administrador (mínimo de 12 caracteres)."""
        user = User.query.filter_by(email=email, is_admin=True).first()
        if not user:
            return click.echo("Administrador não encontrado.")
        if len(password) < 12:
            return click.echo("Use pelo menos 12 caracteres.")
        user.set_password(password)
        db.session.commit()
        click.echo("Senha atualizada.")

    @app.cli.command("trocar-email-admin")
    @click.option("--email", default="admin@exemplo.com", show_default=True, help="E-mail atual do admin.")
    @click.argument("novo")
    def trocar_email_admin(email, novo):
        """Troca o e-mail de login do administrador. Ex.: flask --app app trocar-email-admin voce@exemplo.com"""
        novo = novo.strip().lower()
        user = User.query.filter_by(email=email.strip().lower(), is_admin=True).first()
        if not user:
            return click.echo("Administrador não encontrado.")
        if "@" not in novo or len(novo) > 120:
            return click.echo("E-mail inválido.")
        if User.query.filter(User.email == novo, User.id != user.id).first():
            return click.echo("Esse e-mail já é usado por outra conta (talvez uma conta de cliente da loja).")
        user.email = novo
        db.session.commit()
        click.echo(f"E-mail do admin trocado para {novo}.")

    return app


app = create_app()
