"""Permissões dos administradores: cada um vê só as áreas do painel que o dono liberou.

"tudo" = acesso total (o dono da loja). Uma tela do painel que não está no mapa abaixo só abre com acesso total:
se alguém esquecer de mapear uma tela nova, ela fica protegida, não aberta.
"""
AREAS = {
    "vendas": "Vendas e relatórios",
    "pedidos": "Pedidos, orçamentos, envio e documentos",
    "produtos": "Produtos, categorias, banners e planilhas",
    "clientes": "Clientes e aprovação de empresas",
    "marketing": "Cupons e ajuda (FAQ)",
    "custos": "Fornecedores, compras, custos e formação de preço",
    "financeiro": "Financeiro (contas a pagar e receber, fluxo de caixa) e lucro",
    "fiscal": "Nota fiscal (NF-e): emitir, cancelar, carta de correção e configuração",
}
TUDO = "tudo"

# telas que todo administrador usa (a própria conta, sair, verificação em duas etapas)
LIVRES = {"admin.login", "admin.login_codigo", "admin.logout", "admin.conta", "admin.duas_etapas",
          "admin.email_teste", "admin.convite", "admin.manual", "admin.hoje"}

AREA_DA_TELA = {
    "admin.dashboard": "vendas", "admin.relatorios_pagina": "vendas", "admin.relatorios_planilha": "vendas",
    **{f"admin.{e}": "pedidos" for e in (
        "orders", "order_status", "order_pix", "order_cartao", "devolucoes_lista", "devolucao", "orcamento", "pedido", "pedido_editar", "pedido_envio",
        "pedido_arquivo_enviar", "pedido_etiqueta", "pedido_arquivo_baixar", "pedido_arquivo_excluir", "orders_imprimir",
        "exportar_pedidos")},
    **{f"admin.{e}": "produtos" for e in (
        "products", "edit_product", "toggle", "categories", "rename_category", "delete_category", "banners_lista",
        "banner_editar", "banner_ativar", "banner_mover", "banner_excluir", "fotos_enviar", "foto_principal",
        "foto_excluir", "planilhas", "exportar_produtos", "importar_planilha", "confirmar_planilha", "estoque_lista",
        "estoque_produto")},
    **{f"admin.{e}": "clientes" for e in ("clientes", "cliente", "excluir_cliente", "cliente_receita")},
    **{f"admin.{e}": "marketing" for e in ("cupons_lista", "cupom_excluir", "ajuda_lista", "ajuda_mover",
                                            "ajuda_excluir", "carrinhos", "avaliacoes_lista", "metricas")},
    **{f"admin.{e}": "custos" for e in ("fornecedores", "fornecedor", "fornecedor_excluir", "produto_custo",
                                         "formacao_preco", "percentuais", "percentual", "compras_lista", "compra", "compra_imprimir",
                                         "notas_compra", "nota_compra", "nota_compra_xml", "nota_compra_danfe")},
    **{f"admin.{e}": "financeiro" for e in ("financeiro_pagina", "lancamento", "fluxo_caixa", "lucro_pagina",
                                             "lucro_planilha", "lucro_preencher", "pedido_boletos")},
    **{f"admin.{e}": "fiscal" for e in ("notas_lista", "notas_inutilizar", "notas_xmls", "fiscal_config", "fiscal_cep",
                                         "pedido_nfe", "nota", "nota_xml", "nota_danfe", "nota_evento_xml")},
    # regras de venda, frete, backup (inclusive o automático), equipe e excluir pedidos: só com acesso total
}

# primeira tela de cada área (para onde vai quem não pode ver a tela pedida)
INICIO_DA_AREA = {"vendas": "admin.dashboard", "pedidos": "admin.orders", "produtos": "admin.products",
                  "clientes": "admin.clientes", "marketing": "admin.cupons_lista", "custos": "admin.fornecedores",
                  "financeiro": "admin.financeiro_pagina", "fiscal": "admin.notas_lista"}


def areas(user):
    if not user or not user.is_admin:
        return set()
    lista = {a.strip() for a in (user.permissoes or "").split(",") if a.strip()}
    return set(AREAS) | {TUDO} if TUDO in lista else lista & set(AREAS)


def total(user):
    return TUDO in areas(user)


def pode(user, area):
    a = areas(user)
    return TUDO in a or area in a


def pode_tela(user, endpoint):
    if endpoint in LIVRES:
        return True
    area = AREA_DA_TELA.get(endpoint)
    return pode(user, area) if area else total(user)


def primeira_tela(user):
    for area, tela in INICIO_DA_AREA.items():
        if pode(user, area):
            return tela
    return "admin.conta"


def texto(user):
    if total(user):
        return "Acesso total"
    nomes = [AREAS[a] for a in AREAS if a in areas(user)]
    return "; ".join(nomes) or "Sem acesso a áreas (só a própria conta)"


def ler(form):
    """Áreas marcadas no formulário da equipe. Devolve o texto para gravar em User.permissoes."""
    if form.get("acesso") == TUDO:
        return TUDO
    return ",".join(a for a in AREAS if form.get(f"area_{a}"))
