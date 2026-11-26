"""Ajuda com perguntas frequentes (sem IA, sem custo): aparece no canto do site quando o atendente com IA está
desligado. As perguntas são editáveis no painel; enquanto não houver nenhuma cadastrada, valem as de PADRAO."""
import re

from markupsafe import Markup, escape

from models import Pergunta

PADRAO = [
    ("Como faço para comprar?",
     "Crie sua conta em /cadastro com CPF ou CNPJ e confirme o e-mail. Depois disso, os preços aparecem e você já "
     "pode montar o carrinho e finalizar o pedido."),
    ("Por que não vejo os preços?",
     "Os preços de atacado aparecem só para quem entrou na conta. Entre em /entrar ou crie sua conta em /cadastro."),
    ("Como funciona a compra para empresas?",
     "Cadastre-se com o CNPJ. O Marketplace B2B analisa o cadastro e, depois de aprovado, sua empresa pode ter desconto "
     "sobre a tabela e pagar com boleto faturado (a prazo). Enquanto isso, você já compra com os preços de tabela."),
    ("Quais são as formas de pagamento?",
     "Pix, boleto à vista, cartão de crédito (pago na página segura da Efí) e combinação pelo WhatsApp. Empresas "
     "aprovadas também podem pagar com boleto faturado, nos prazos liberados para elas."),
    ("Como funciona o preço de atacado?",
     "Na página de cada produto há a tabela de preço por quantidade: quanto mais unidades, menor o preço de cada "
     "uma. Alguns produtos têm pedido mínimo ou são vendidos em embalagem fechada; os fracionáveis também podem ser "
     "vendidos avulsos, com um pequeno acréscimo."),
    ("Posso pedir um orçamento?",
     "Sim. Monte o carrinho e, na finalização, escolha “Orçamento”. O Marketplace B2B confere preço, prazo e frete e "
     "responde por e-mail antes de faturar."),
    ("Como é calculado o frete?",
     "Informe seu CEP no carrinho: o frete e o prazo aparecem antes de finalizar. Quando não houver tabela para a "
     "sua região, o frete é combinado com o Marketplace B2B depois do pedido."),
    ("Como acompanho ou cancelo meu pedido?",
     "Em /conta/pedidos você vê a situação de cada pedido e pode cancelar os que ainda aguardam pagamento e os "
     "orçamentos em análise."),
    ("Como devolvo ou troco um produto?",
     "Em /conta/pedidos, abra o pedido entregue e use “Devolver ou trocar produtos”: escolha os itens, a quantidade "
     "e o motivo. O Marketplace B2B responde por e-mail com as instruções de envio, e o reembolso é feito quando os produtos "
     "chegam (no cartão, o estorno vai direto para a fatura)."),
    ("Meus dados estão seguros?",
     "Sim. Veja como cuidamos dos seus dados em /privacidade. Em /conta/privacidade você pode baixar seus dados "
     "ou excluir sua conta."),
]

_LINK = re.compile(r"(^|[\s(])(/(?:produto|conta|cadastro|entrar|privacidade|termos|carrinho)[^\s)<,]*"
                   r"|/\?cat=\d+|https://[^\s<)]+)")


def formatar(texto):
    """Texto simples -> HTML seguro: escapa tudo, transforma /caminhos da loja e https:// em links, quebra linhas."""
    seguro = str(escape(texto or ""))

    def link(m):
        url = m.group(2)
        externo = ' target="_blank" rel="noopener"' if url.startswith("https://") else ""
        return f'{m.group(1)}<a href="{url}"{externo}>{url}</a>'

    return Markup(_LINK.sub(link, seguro).replace("\n", "<br>"))


def perguntas():
    """Perguntas ativas do painel, ou as padrão se ainda não houver nenhuma cadastrada."""
    salvas = Pergunta.query.order_by(Pergunta.ordem, Pergunta.id).all()
    if not salvas:
        return [{"pergunta": p, "resposta": r} for p, r in PADRAO]
    return [{"pergunta": p.pergunta, "resposta": p.resposta} for p in salvas if p.ativo]
