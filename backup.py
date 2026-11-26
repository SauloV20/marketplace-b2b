"""Backup e restauração de todos os dados da loja num arquivo só (.json.gz), pelo painel.

O arquivo leva todas as tabelas (inclusive fotos e imagens, em base64). Na restauração, os dados atuais são
apagados e substituídos pelos do arquivo, numa transação só: se algo der errado, nada muda.
Colunas que não existirem no arquivo (backup de uma versão antiga do site) ficam com o valor padrão.
"""
import base64
import gzip
import json
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, LargeBinary, Numeric, text

from models import agora_utc, db

FORMATO, VERSAO = "marketplace-backup", 1
# tabelas temporárias: não vale a pena guardar nem restaurar
IGNORADAS = {"login_falha", "limite_uso", "importacao_pendente"}
MAX_ARQUIVO = 100_000_000  # 100 MB
# backup "só dados" (o do e-mail): sem as fotos e os arquivos anexados aos pedidos, que são o que pesa.
# As notas fiscais (XML) ficam: elas estão na tabela nota_fiscal.
SEM_ARQUIVOS = {"produto_foto", "pedido_arquivo"}
COLUNAS_PESADAS = {("banner", "imagem")}
CIFRADO = b"MKTPBKP1"  # começo do arquivo protegido por senha (AES-256-GCM, chave derivada da senha com scrypt)


class ErroBackup(Exception):
    pass


def tabelas():
    return [t for t in db.metadata.sorted_tables if t.name not in IGNORADAS]  # pais antes dos filhos


def _para_json(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"b64": base64.b64encode(bytes(v)).decode()}
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _do_json(coluna, v):
    if v is None:
        return None
    tipo = coluna.type
    if isinstance(tipo, LargeBinary):
        return base64.b64decode(v["b64"]) if isinstance(v, dict) else None
    if isinstance(tipo, Numeric):
        return Decimal(str(v))
    if isinstance(tipo, DateTime):
        return datetime.fromisoformat(v)
    if isinstance(tipo, Date):
        return date.fromisoformat(v)
    if isinstance(tipo, Boolean):
        return bool(v)
    return v


def gerar(sem_arquivos=False):
    """Devolve (bytes do arquivo .json.gz, resumo {tabela: linhas}). sem_arquivos: sem fotos nem anexos."""
    dados, resumo = {}, {}
    for t in tabelas():
        if sem_arquivos and t.name in SEM_ARQUIVOS:
            continue
        pesadas = {c for tabela, c in COLUNAS_PESADAS if tabela == t.name} if sem_arquivos else set()
        linhas = [{c: (None if c in pesadas else _para_json(v)) for c, v in r._mapping.items()}
                  for r in db.session.execute(t.select())]
        dados[t.name], resumo[t.name] = linhas, len(linhas)
    conteudo = {"formato": FORMATO, "versao": VERSAO, "criado_em": agora_utc().isoformat(timespec="seconds"),
                "parcial": bool(sem_arquivos), "resumo": resumo, "tabelas": dados}
    return gzip.compress(json.dumps(conteudo, ensure_ascii=False, separators=(",", ":")).encode(), 6), resumo


def _chave(senha, sal):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=sal, length=32, n=2 ** 15, r=8, p=1).derive(senha.encode())


def cifrar(dados, senha):
    """Protege o arquivo com a senha. Sem a senha, ninguém abre (nem nós)."""
    import os
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    sal, nonce = os.urandom(16), os.urandom(12)
    return CIFRADO + sal + nonce + AESGCM(_chave(senha, sal)).encrypt(nonce, dados, CIFRADO)


def decifrar(dados, senha):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not senha:
        raise ErroBackup("Este backup é protegido por senha: informe a senha do backup (a criada ao baixar, ou a "
                         "BACKUP_SENHA do Render para o backup do e-mail).")
    sal, nonce, corpo = dados[8:24], dados[24:36], dados[36:]
    try:
        return AESGCM(_chave(senha, sal)).decrypt(nonce, corpo, CIFRADO)
    except InvalidTag as e:
        raise ErroBackup("Senha do backup errada (ou arquivo corrompido). Nada foi alterado.") from e


def em_zip(nome, dados):
    """Põe o arquivo dentro de um .zip (o e-mail recusa anexo ".cifrado"). Sem compressão: já está cifrado."""
    import io
    import zipfile
    saida = io.BytesIO()
    with zipfile.ZipFile(saida, "w", zipfile.ZIP_STORED) as z:
        z.writestr(nome, dados)
    return saida.getvalue()


def _de_zip(dados):
    """Tira o backup de dentro do .zip do e-mail (o primeiro .cifrado ou .json.gz). Sem descompactar nada enorme."""
    import io
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(dados)) as z:
            for info in z.infolist():
                if info.filename.endswith((".cifrado", ".json.gz")) and info.file_size <= MAX_ARQUIVO:
                    return z.read(info)
    except zipfile.BadZipFile as e:
        raise ErroBackup("Esse arquivo .zip está corrompido.") from e
    raise ErroBackup("Esse .zip não tem um backup da loja dentro.")


def ler(arquivo_bytes, senha=""):
    """Abre e confere o arquivo. Devolve o conteúdo (dict). Levanta ErroBackup se não for um backup da loja."""
    if arquivo_bytes[:4] == b"PK\x03\x04":  # backup que veio por e-mail, dentro de um .zip
        arquivo_bytes = _de_zip(arquivo_bytes)
    if arquivo_bytes[:8] == CIFRADO:
        arquivo_bytes = decifrar(arquivo_bytes, senha)
    try:
        conteudo = json.loads(gzip.decompress(arquivo_bytes))
    except (OSError, EOFError, ValueError) as e:
        raise ErroBackup("Esse arquivo não é um backup da loja (envie o .json.gz baixado no painel).") from e
    if not isinstance(conteudo, dict) or conteudo.get("formato") != FORMATO or \
            not isinstance(conteudo.get("tabelas"), dict):
        raise ErroBackup("Esse arquivo não é um backup da loja (envie o .json.gz baixado no painel).")
    if conteudo.get("versao", 0) > VERSAO:
        raise ErroBackup("Esse backup foi feito por uma versão mais nova do site. Atualize o site antes.")
    if not any(u.get("is_admin") for u in conteudo["tabelas"].get("user", [])):
        raise ErroBackup("O backup não tem nenhuma conta de administrador: restaurar deixaria você sem acesso.")
    return conteudo


def restaurar(conteudo):
    """Apaga os dados atuais e grava os do backup. Devolve {tabela: linhas restauradas}."""
    lista = tabelas()
    feito = {}
    try:
        for t in reversed(lista):  # filhos antes dos pais, por causa das chaves estrangeiras
            db.session.execute(t.delete())
        for t in lista:
            linhas = conteudo["tabelas"].get(t.name) or []
            colunas = {c.name: c for c in t.columns}
            convertidas = [{nome: _do_json(colunas[nome], v) for nome, v in linha.items() if nome in colunas}
                           for linha in linhas if isinstance(linha, dict)]
            for i in range(0, len(convertidas), 500):
                db.session.execute(t.insert(), convertidas[i:i + 500])
            feito[t.name] = len(convertidas)
        _acertar_sequencias(lista)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return feito


def _acertar_sequencias(lista):
    """No PostgreSQL (Render), o próximo id de cada tabela precisa passar do maior id restaurado."""
    if db.engine.dialect.name != "postgresql":
        return  # MySQL e SQLite acertam sozinhos
    q = db.engine.dialect.identifier_preparer.quote
    for t in lista:
        if "id" in t.columns and t.columns["id"].primary_key and t.columns["id"].type.python_type is int:
            db.session.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{q(t.name)}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {q(t.name)}), 0) + 1, false)"))
