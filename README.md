# Marketplace B2B

Loja B2B com painel de gestão (ERP), feita em Flask. Atende pessoa física e empresas (CNPJ aprovado, desconto, prazos, faturado com limite de crédito e equipe com aprovação de pedidos). O painel tem estoque, compras, financeiro, formação de preço, NF-e direto na SEFAZ e Pix, boleto e cartão de crédito pela Efí.

## Destaques
- **Stack:** Python 3.12+, Flask 3.1, SQLAlchemy 2.1, Jinja2, PostgreSQL (produção) ou MySQL, SQLite nos testes. Sem framework de front-end: HTML renderizado no servidor e JavaScript mínimo.
- **Domínio B2B:** preço por faixa de quantidade, fracionado com acréscimo, preço calculado por estado (custo, impostos e percentuais), desconto e limite de crédito por empresa, equipe de compradores com aprovação de pedidos.
- **ERP no painel:** pedidos e orçamentos, estoque com histórico, compras com custo médio, contas a pagar e receber, fluxo de caixa, lucro, NF-e direto na SEFAZ, relatórios em Excel e permissões por área.
- **Integrações:** Pix, boleto e cartão (link de pagamento e estorno) pela Efí; cotação, etiqueta e rastreio pelo Melhor Envio; consulta de CNPJ; atendente virtual com a API da Anthropic; e-mail por Resend, Brevo ou SMTP.
- **Automação:** tarefas agendadas pelo GitHub Actions (backup cifrado, rastreio, lembrete de carrinho abandonado) e avisos por e-mail a cada etapa do pedido.
- **Segurança:** CSP sem script inline, CSRF em todo formulário, sessão com prazo e revogação na troca de senha, verificação em duas etapas no painel, limites de tentativa no banco, backup em AES-256-GCM, LGPD (exportar e excluir a conta, aviso de cookies). Testado com dois pentests.
- **Qualidade:** mais de 500 testes automáticos em Python 3.12 e 3.13 a cada envio, dependências com versão travada e Dependabot.

> Projeto de portfólio, derivado de um sistema em produção. Nomes, domínios e dados de exemplo são fictícios.

**Produção:** site no Render (`gunicorn app:app`) e banco PostgreSQL no Neon.
**No seu computador:** MySQL ou PostgreSQL.

## Rodar no seu computador
1. Crie o banco. No MySQL: `CREATE DATABASE marketplace CHARACTER SET utf8mb4;`
2. `python -m venv venv && source venv/bin/activate` (Windows: `venv\Scripts\activate`)
3. `pip install -r requirements.txt`
4. Copie `.env.example` para `.env` e ajuste pelo menos `SECRET_KEY` e `DATABASE_URL`. Cada variável está explicada no próprio arquivo.
5. Crie as tabelas e os dados de exemplo com `flask --app app init-db`, ou importe `exemplo.sql` (`mysql -u usuario -p < exemplo.sql`).
6. `flask --app app run --debug`

- **Loja:** http://127.0.0.1:5000
- **Painel:** o endereço de `ADMIN_PATH` (padrão `/admin`). Não há botão no site: acesse digitando a URL.
- **Login inicial:** `admin@exemplo.com`. A senha vem de `ADMIN_PASSWORD` no primeiro `init-db`; sem ela, uma senha aleatória é gerada e aparece uma vez no log.

## Comandos
| Comando | O que faz |
|---|---|
| `flask --app app init-db` | Cria as tabelas novas e acrescenta as colunas novas. Roda a cada deploy e não apaga nada. |
| `flask --app app trocar-senha-admin` | Troca a senha do administrador. |
| `flask --app app trocar-email-admin` | Troca o e-mail do administrador. |
| `flask --app app trocar-catalogo` | Troca o catálogo pelo de `seed_data.py`. **Apaga produtos, categorias e pedidos** e mantém o admin. |

## Publicar no Render
- **Comando de início:** `gunicorn app:app`. O `init-db` roda antes, a cada deploy.
- **Variáveis:** as do `.env.example`, em *Environment*. São obrigatórias em produção `SECRET_KEY` (forte), `DATABASE_URL` (Neon) e `SITE_URL` (domínio próprio).
- **`ADMIN_PATH`:** use um endereço secreto (ex.: `/painel-k7x92qf`).
- **Certificados** da Efí (`.p12`) e da NF-e (`.pfx`): vão em *Secret Files*. A variável guarda só o caminho (ex.: `/etc/secrets/certificado.pfx`).
- **Auto-Deploy "After CI Checks Pass":** com essa opção, o site só é publicado quando os testes do GitHub passam.
- **Monitor externo:** aponte um monitor (ex.: UptimeRobot) para `/saude`, para manter o site acordado no plano grátis.
- **Domínio próprio:** o endereço `*.onrender.com` redireciona para o `SITE_URL`.

## Tarefas automáticas (GitHub Actions)
- **Testes:** rodam a cada envio, em Python 3.12 e 3.13.
- **Carrinho abandonado:** todo dia às 9h, 13h e 18h (Brasília), o GitHub pede ao site para mandar o lembrete a quem deixou produtos no carrinho. Configuração em Painel › Carrinhos abandonados. Usa os mesmos segredos do backup.
- **Rastreio automático:** de segunda a sábado, 5 vezes por dia (8h às 20h, Brasília), o GitHub pede ao site para conferir as etiquetas do Melhor Envio em andamento. Usa os mesmos segredos do backup.
- **Backup semanal:** toda segunda às 6h (Brasília), o GitHub pede ao site para mandar o backup por e-mail, em `.zip` com a senha de `BACKUP_SENHA`.
  - Configure os segredos `SITE_URL` e `TAREFAS_TOKEN` em *Settings › Secrets and variables › Actions*.
  - O `TAREFAS_TOKEN` tem o mesmo valor da variável no Render.

## Bibliotecas
As versões ficam travadas no `requirements.txt`, inclusive as dependências delas: o deploy instala exatamente o que passou nos testes. Toda segunda o **Dependabot** do GitHub abre PRs com as versões novas, e as atualizações de segurança chegam assim que saem. Cada PR passa pelos testes; com tudo verde, é só fazer o merge.

## Testes automáticos
`python -m unittest discover -s tests -v`. Usa SQLite em memória: não mexe no banco real nem envia e-mails.

## Estrutura
| Onde | O quê |
|---|---|
| `app.py` | Criação do site, comandos e atualização do banco (`init-db`) |
| `config.py` | Leitura das variáveis de ambiente |
| `models.py` | Tabelas do banco |
| `blueprints/shop.py`, `blueprints/account.py` | Loja e conta do cliente |
| `blueprints/admin.py` e `blueprints/painel_*.py` | Painel, dividido por área (pedidos, produtos, clientes, vendas, marketing, estoque, compras, custos, financeiro, fiscal, configurações) |
| `formacao.py`, `margem.py`, `lucro.py` | Formação de preço (custo, impostos por estado, percentuais por produto ou categoria, markup), proteção de margem e lucro |
| `estoque.py`, `compras.py`, `financeiro.py` | ERP: estoque, compras com custo médio, contas a pagar e receber |
| `fiscal.py`, `nfe_provedores.py`, `notas_compra.py` | NF-e (emissão, eventos, DANFE) e notas de compra buscadas na SEFAZ |
| `efi.py`, `boleto_efi.py`, `cartao_efi.py`, `pix.py` | Pix, boleto e cartão de crédito (link de pagamento) pela Efí, e Pix estático |
| `frete.py`, `melhor_envio.py`, `etiquetas.py` | Frete pela tabela própria, cotação de Correios e transportadoras e compra, impressão, rastreio e cancelamento de etiquetas pelo Melhor Envio |
| `carrinho_abandonado.py` | Carrinho guardado na conta e lembrete por e-mail do carrinho abandonado |
| `devolucoes.py`, `avaliacoes.py`, `analytics.py` | Devolução e troca (com estorno do cartão), avaliações de produto e Google Analytics com aviso de cookies |
| `backup.py`, `backup_auto.py`, `alertas.py` | Backup e restauração, backup semanal e avisos de erro por e-mail |
| `templates/`, `static/` | Páginas, CSS e JS. Os scripts ficam fora das páginas, por causa da proteção CSP. |
| `tests/` | Testes automáticos |

O manual completo do painel, com o passo a passo de cada área, fica no próprio painel, em **Manual**.

## E-mail
- **O que é enviado:** confirmação de conta (o link vale 48 h), recuperação de senha (1 h, uso único), avisos de pedido, envio, NF-e, convites da equipe, lembrete do carrinho abandonado, alertas e backup.
- **Como enviar:** configure `BREVO_API_KEY` (recomendado no Render), `RESEND_API_KEY` ou SMTP (`MAIL_SERVER`, `MAIL_USERNAME`, `MAIL_PASSWORD`), junto com `MAIL_FROM`.
- **Sem configuração:** o conteúdo do e-mail aparece no terminal.
