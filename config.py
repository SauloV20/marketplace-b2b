import os

from dotenv import load_dotenv

load_dotenv()  # o gunicorn não lê o .env sozinho (o `flask` lê)

_SEGREDO = os.getenv("SECRET_KEY", "dev-troque-esta-chave")
_PRODUCAO = os.getenv("APP_ENV") == "production" or bool(os.getenv("RENDER"))  # o Render define RENDER sozinho
if _PRODUCAO and _SEGREDO.startswith(("dev-", "troque")):
    raise RuntimeError("Defina uma SECRET_KEY forte nas variáveis de ambiente antes de rodar em produção.")

# Endereço público do site, usado nos links dos e-mails (o Render informa RENDER_EXTERNAL_URL sozinho)
_SITE_URL = os.getenv("SITE_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
if _PRODUCAO and not _SITE_URL:
    raise RuntimeError("Defina SITE_URL (ex.: https://loja-exemplo.com.br) para os links de confirmação e de senha.")


def _db_url():
    url = os.getenv("DATABASE_URL", "mysql+pymysql://root:senha@localhost:3306/marketplace?charset=utf8mb4")
    if url.startswith("postgres://"):  # o Render entrega assim
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):  # força o psycopg2 (o SQLAlchemy 2.1 assumiria o psycopg 3)
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def _admin_path():
    """Endereço secreto do painel. Defina ADMIN_PATH (ex.: /painel-k7x92qf) para que ninguém adivinhe a URL."""
    p = "/" + os.getenv("ADMIN_PATH", "/admin").strip().strip("/")
    return p if p != "/" else "/admin"


class Config:
    PRODUCAO = _PRODUCAO
    ADMIN_PATH = _admin_path()
    # E-mail (confirmação de conta e recuperação de senha). Use Resend OU SMTP.
    SITE_URL = _SITE_URL
    MAIL_FROM = os.getenv("MAIL_FROM", "Marketplace B2B <nao-responda@exemplo.com>")
    RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
    # Brevo pela API HTTPS (funciona no plano grátis do Render, que bloqueia as portas de SMTP 25/465/587)
    BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
    MAIL_SERVER = os.getenv("MAIL_SERVER", "")  # ex.: smtp.gmail.com
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")
    SECRET_KEY = _SEGREDO
    # emergência: perdeu o celular e os códigos de recuperação? ADMIN_2FA_DESLIGADO=1 no Render, entre, ajuste e apague
    ADMIN_2FA_DESLIGADO = os.getenv("ADMIN_2FA_DESLIGADO", "").strip().lower() in ("1", "true", "sim")
    SQLALCHEMY_DATABASE_URI = _db_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # o banco pode fechar conexões paradas (o Neon "dorme" quando ninguém usa): testa antes de usar e renova a cada 5 min
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True,
                                 **({"pool_recycle": 300} if SQLALCHEMY_DATABASE_URI.startswith("postgresql") else {})}
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SECURE = _PRODUCAO or os.getenv("COOKIE_SECURE") == "1"
    # sessão do painel cai sozinha: depois de X minutos sem usar, ou X horas depois do login (mesmo usando)
    ADMIN_INATIVO_MIN = int(os.getenv("ADMIN_INATIVO_MIN", "120"))
    ADMIN_SESSAO_MAX_H = int(os.getenv("ADMIN_SESSAO_MAX_H", "12"))
    # sessão do cliente na loja: entra de novo depois de X dias do login
    CLIENTE_SESSAO_MAX_DIAS = int(os.getenv("CLIENTE_SESSAO_MAX_DIAS", "30"))
    LIMITE_BANCO_MB = int(os.getenv("LIMITE_BANCO_MB", "512"))  # espaço do banco no plano (Neon grátis: 0,5 GB)
    MAX_CONTENT_LENGTH = 40_000_000  # fotos de celular (comprimidas antes de ir para o banco) e planilhas
    # Pagamento: preencha no .env (ou nas variáveis de ambiente do Render)
    PIX_KEY = os.getenv("PIX_KEY", "")
    PIX_NAME = os.getenv("PIX_NAME", "Marketplace B2B")
    PIX_CITY = os.getenv("PIX_CITY", "SAO PAULO")
    WHATSAPP_NUMBER = os.getenv("WHATSAPP_NUMBER", "")  # só dígitos, ex.: 5511999998888
    # Confirmação do celular por SMS (Twilio Verify). Sem as três, fica desligada.
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
    TWILIO_VERIFY_SID = os.getenv("TWILIO_VERIFY_SID", "")
    # Pix pela Efí, com conferência do pagador (veja efi.py). Sem elas, o Pix usa o QR estático.
    EFI_CLIENT_ID = os.getenv("EFI_CLIENT_ID", "")
    EFI_CLIENT_SECRET = os.getenv("EFI_CLIENT_SECRET", "")
    EFI_CERTIFICADO = os.getenv("EFI_CERTIFICADO", "")  # ex.: /etc/secrets/efi.p12
    EFI_CERTIFICADO_SENHA = os.getenv("EFI_CERTIFICADO_SENHA", "")
    EFI_CHAVE_PIX = os.getenv("EFI_CHAVE_PIX", "")
    EFI_SANDBOX = os.getenv("EFI_SANDBOX") == "1"
    # NF-e: certificado digital A1 da empresa (arquivo .pfx como Secret File no Render) e a senha dele.
    # NFE_PROVEDOR = sefaz (direto na SEFAZ, padrão) | api (provedor de emissão, quando for configurado)
    NFE_PROVEDOR = os.getenv("NFE_PROVEDOR", "sefaz")
    NFE_CERTIFICADO = os.getenv("NFE_CERTIFICADO", "")  # ex.: /etc/secrets/certificado.pfx
    NFE_CERTIFICADO_SENHA = os.getenv("NFE_CERTIFICADO_SENHA", "")
    NFE_API_URL = os.getenv("NFE_API_URL", "")
    # Backup semanal por e-mail: senha que protege o arquivo e o token que o GitHub Actions usa para pedir o envio
    BACKUP_SENHA = os.getenv("BACKUP_SENHA", "")
    # aviso por e-mail quando o site dá erro (sem ela, vai para os e-mails do backup semanal)
    ALERTA_EMAIL = os.getenv("ALERTA_EMAIL", "")
    TAREFAS_TOKEN = os.getenv("TAREFAS_TOKEN", "")
    NFE_API_TOKEN = os.getenv("NFE_API_TOKEN", "")
    # Google Search Console: o código do "Prefixo do URL > Tag HTML" (só o valor de content="...")
    GOOGLE_SITE_VERIFICATION = os.getenv("GOOGLE_SITE_VERIFICATION", "")
    # Atendente virtual com IA (Claude, da Anthropic). Sem a chave, o chat não aparece no site.
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    ATENDENTE_MODELO = os.getenv("ATENDENTE_MODELO", "claude-opus-5-5")
    ATENDENTE_LIMITE_DIA = int(os.getenv("ATENDENTE_LIMITE_DIA", "1000"))  # mensagens por dia na loja toda
    # Cotação de frete pelo Melhor Envio (Correios e transportadoras). Sem o token, só a tabela própria.
    MELHOR_ENVIO_TOKEN = os.getenv("MELHOR_ENVIO_TOKEN", "")
    MELHOR_ENVIO_CONTATO = os.getenv("MELHOR_ENVIO_CONTATO", "")  # e-mail técnico que vai no User-Agent
    MELHOR_ENVIO_SANDBOX = os.getenv("MELHOR_ENVIO_SANDBOX") == "1"
    # secret do aplicativo do Melhor Envio: confere a assinatura dos avisos (webhook) de etiqueta
    MELHOR_ENVIO_SECRET = os.getenv("MELHOR_ENVIO_SECRET", "")
    # Consulta o CNPJ na Receita (BrasilAPI) no cadastro de empresas. CONSULTA_CNPJ=0 desliga.
    CONSULTA_CNPJ = os.getenv("CONSULTA_CNPJ", "0" if os.getenv("APP_ENV") == "test" else "1") == "1"
