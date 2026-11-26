"""Consulta de CNPJ na Receita Federal pela BrasilAPI (gratuita, sem chave): https://brasilapi.com.br

Respostas da API: 200 = encontrado, 404 = CNPJ não existe, 400 = CNPJ inválido.
Se a API estiver fora do ar ou lenta, o cadastro segue e a empresa fica como "não verificada".
"""
import json
import time
import urllib.error
import urllib.request

from flask import current_app

URL = "https://brasilapi.com.br/api/cnpj/v1/{}"
TEMPO_LIMITE = 6  # segundos: o cliente está esperando na tela de cadastro
_cache = {}  # cnpj -> (quando, resultado); evita consultar o mesmo CNPJ várias vezes seguidas
CACHE_S = 3600

ENCONTRADO, NAO_ENCONTRADO, INDISPONIVEL = "ok", "nao_encontrado", "indisponivel"


def _texto(v):
    return " ".join(str(v or "").split())


def resumir(d):
    """Só o que a loja usa, com nomes nossos."""
    tipo, rua, numero = _texto(d.get("descricao_tipo_de_logradouro")), _texto(d.get("logradouro")), _texto(d.get("numero"))
    if numero and rua.endswith(" " + numero):  # a API às vezes repete o número no logradouro
        rua = rua[: -len(numero) - 1]
    cep = _texto(d.get("cep"))
    partes = [" ".join(p for p in (tipo, rua) if p), numero, _texto(d.get("complemento")), _texto(d.get("bairro")),
              "/".join(p for p in (_texto(d.get("municipio")), _texto(d.get("uf"))) if p),
              f"CEP {cep[:5]}-{cep[5:]}" if len(cep) == 8 else cep]
    cnae = " - ".join(p for p in (_texto(d.get("cnae_fiscal")), _texto(d.get("cnae_fiscal_descricao"))) if p)
    return {
        "razao_social": _texto(d.get("razao_social"))[:160],
        "nome_fantasia": _texto(d.get("nome_fantasia"))[:160],
        "situacao": _texto(d.get("descricao_situacao_cadastral")).upper()[:40],
        "abertura": _texto(d.get("data_inicio_atividade")),
        "cnae": cnae[:200],
        "endereco": ", ".join(p for p in partes if p)[:300],
    }


def consultar_cnpj(cnpj):
    """Devolve (ENCONTRADO, dados) | (NAO_ENCONTRADO, None) | (INDISPONIVEL, None)."""
    if not current_app.config.get("CONSULTA_CNPJ", True):
        return INDISPONIVEL, None
    guardado = _cache.get(cnpj)
    if guardado and time.time() - guardado[0] < CACHE_S:
        return guardado[1]
    req = urllib.request.Request(URL.format(cnpj), headers={"User-Agent": "marketplace-b2b/1.0",
                                                             "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TEMPO_LIMITE) as r:
            resultado = (ENCONTRADO, resumir(json.load(r)))
    except urllib.error.HTTPError as e:
        if e.code not in (400, 404):  # 429, 5xx...: tenta de novo numa próxima vez
            current_app.logger.warning("BrasilAPI respondeu %s para o CNPJ %s", e.code, cnpj)
            return INDISPONIVEL, None
        resultado = (NAO_ENCONTRADO, None)
    except (OSError, ValueError) as e:  # sem rede, tempo esgotado, resposta que não é JSON
        current_app.logger.warning("BrasilAPI indisponível: %s", e)
        return INDISPONIVEL, None
    _cache[cnpj] = (time.time(), resultado)
    if len(_cache) > 2000:
        _cache.clear()
    return resultado


def limpar_cache():
    _cache.clear()
