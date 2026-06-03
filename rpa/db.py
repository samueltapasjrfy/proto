"""Conexão MySQL + consultas de orquestração do RPA.

Lê credenciais de RPA_DB_HOST/PORT/USER/PASSWORD/NAME no .env.
A query principal (`listar_protocolos_aptos`) traz os itens elegíveis pra ser
processados pelo robô do eproc-MG.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

import pymysql
from pymysql.cursors import DictCursor


class DBConfigError(RuntimeError):
    pass


def _config_db() -> dict[str, Any]:
    host = os.getenv("RPA_DB_HOST", "").strip()
    user = os.getenv("RPA_DB_USER", "").strip()
    if not host or not user:
        raise DBConfigError(
            "Conexão MySQL não configurada — preencha RPA_DB_HOST/USER/PASSWORD/NAME no .env."
        )
    return {
        "host": host,
        "port": int(os.getenv("RPA_DB_PORT", "3306")),
        "user": user,
        "password": os.getenv("RPA_DB_PASSWORD", ""),
        "database": os.getenv("RPA_DB_NAME", "").strip(),
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "autocommit": True,
        "connect_timeout": 10,
    }


@contextmanager
def conexao() -> Iterator[pymysql.connections.Connection]:
    """Context manager que abre e fecha a conexão MySQL."""
    conn = pymysql.connect(**_config_db())
    try:
        yield conn
    finally:
        conn.close()


# Itens prontos pra serem processados. NumProcessoCNJ é o número em dígitos
# (usado pra montar pasta local e validar contra o conteúdo do PDF);
# NumProcesso costuma vir formatado (uso humano).
# Multi-tribunal (MG e RS) — o CASE devolve qual eproc base usar pra cada item.
# Cada bloco do OR é exclusivo por código de tribunal no CNJ.
_QUERY_APTOS = """
SELECT
    t.CodItem,
    t.IdProc,
    t2.NumProcesso,
    t2.NumProcessoCNJ,
    CASE
        WHEN t2.NumProcessoCNJ REGEXP '813[0-9]{4}$' THEN 'https://eproc1g.tjmg.jus.br/eproc/'
        WHEN t2.NumProcessoCNJ REGEXP '821[0-9]{4}$' THEN 'https://eproc1g.tjrs.jus.br/eproc/'
        WHEN t2.NumProcessoCNJ REGEXP '826[0-9]{4}$' THEN 'https://eproc1g.tjsp.jus.br/eproc/'
        WHEN t2.NumProcessoCNJ REGEXP '819[0-9]{4}$' THEN 'https://eproc1g.tjrj.jus.br/eproc/'
    END AS eproc_base
FROM tbitens t
LEFT JOIN tbprocessos t2 ON t.IdProc = t2.IdProc
WHERE t.CodStatusCheckin IN (1, 10)
  AND t.DtConclusao IS NULL
  AND t.CodTipoItem = 5
  AND t.CodTipoSubItem = 65
  AND t.DtCadastro >= %s
  AND (
        -- MG: só os que já migraram pro eproc (começa com '1' e ano >= 2025)
        (
            t2.NumProcessoCNJ REGEXP '813[0-9]{4}$'
            AND t2.NumProcessoCNJ LIKE '1%%'
            AND SUBSTRING(t2.NumProcessoCNJ, 10, 4) >= '2025'
        )
        OR
        -- RS: migrou tudo, basta ser tribunal 21
        (
            t2.NumProcessoCNJ REGEXP '821[0-9]{4}$'
        )
        OR
        -- SP: hipótese — começa com '4' e ano >= 2025 (apenas processos já migrados)
        (
            t2.NumProcessoCNJ REGEXP '826[0-9]{4}$'
            AND t2.NumProcessoCNJ LIKE '4%%'
            AND SUBSTRING(t2.NumProcessoCNJ, 10, 4) >= '2025'
        )
        OR
        -- RJ: hipótese — começa com '3' e ano >= 2025 (apenas processos já migrados)
        (
            t2.NumProcessoCNJ REGEXP '819[0-9]{4}$'
            AND t2.NumProcessoCNJ LIKE '3%%'
            AND SUBSTRING(t2.NumProcessoCNJ, 10, 4) >= '2025'
        )
  )
  AND EXISTS (
      SELECT 1 FROM tbarquivosprocesso ap WHERE ap.CodItem = t.CodItem
  )
"""

# Arquivos do item (com ponteiros S3 e credenciais por arquivo).
_QUERY_ARQUIVOS = """
SELECT
    t1.CodItem,
    t1.CodArquivo,
    t1.NomeArquivo,
    t1.NomeArquivoBucketS3,
    t1.BucketS3,
    t1.AcesseKey,
    t1.SecretKey,
    t1.Region
FROM tbarquivosprocesso t1
INNER JOIN tbitens t2 ON t1.CodItem = t2.CodItem
INNER JOIN tbprocessos t3 ON t1.IdProc = t3.IdProc
WHERE t2.CodItem = %s
"""


def listar_protocolos_aptos(
    *,
    dt_cadastro_minimo: str = "2026-01-01",
    cod_item: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Itens em status de check-in (1 ou 10) ainda não concluídos, do tipo
    petição (CodTipoItem=5 / CodTipoSubItem=65), com CNJ terminando em
    '813XXXX' (filtra processos MG), cadastrados a partir de `dt_cadastro_minimo`.

    `cod_item` é um filtro opcional pra apontar um item específico (útil em testes).
    """
    sql = _QUERY_APTOS
    params: list[Any] = [dt_cadastro_minimo]
    if cod_item is not None:
        sql += " AND t.CodItem = %s"
        params.append(cod_item)
    sql += " ORDER BY t.DtCadastro ASC"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(int(limit))

    with conexao() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def listar_arquivos_do_item(cod_item: int) -> list[dict[str, Any]]:
    """Arquivos vinculados a um CodItem, com bucket/key/credenciais S3 por arquivo."""
    with conexao() as conn:
        with conn.cursor() as cur:
            cur.execute(_QUERY_ARQUIVOS, [int(cod_item)])
            return [dict(r) for r in cur.fetchall()]


COD_STATUS_CHECKIN_EM_EXECUCAO = 11
COD_STATUS_CHECKIN_ERRO = 10


def _set_cod_status_checkin(cod_item: int, novo_status: int) -> int:
    with conexao() as conn:
        with conn.cursor() as cur:
            return cur.execute(
                "UPDATE tbitens SET CodStatusCheckin = %s WHERE CodItem = %s",
                (int(novo_status), int(cod_item)),
            )


def marcar_em_execucao(cod_item: int) -> int:
    """Sinaliza pra operação que o robô pegou o item — evita execução manual paralela."""
    return _set_cod_status_checkin(cod_item, COD_STATUS_CHECKIN_EM_EXECUCAO)


def marcar_como_erro(cod_item: int) -> int:
    """Devolve o item pro pool (status 10) — próxima rodada do robô vai pegar de novo."""
    return _set_cod_status_checkin(cod_item, COD_STATUS_CHECKIN_ERRO)


def buscar_processo_por_cod_item(cod_item: int) -> dict[str, Any] | None:
    """Retorna {CodItem, IdProc, NumProcesso, NumProcessoCNJ} pelo CodItem, ou None."""
    with conexao() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.CodItem, t.IdProc, t2.NumProcesso, t2.NumProcessoCNJ
                FROM tbitens t
                LEFT JOIN tbprocessos t2 ON t.IdProc = t2.IdProc
                WHERE t.CodItem = %s
                """,
                (int(cod_item),),
            )
            row = cur.fetchone()
            return dict(row) if row else None
