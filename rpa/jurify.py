"""Finalização no painel Jurify — replica POST /files/upload do RPAGateway.

Pipeline (a ordem importa):
  1. SELECT IdProc FROM tbitens WHERE CodItem = ?
  2. INSERT esqueleto em tbarquivosprocesso
  3. SELECT MAX(CodArquivo)            ← gera o id do anexo no painel
  4. Upload S3:  s3://netview-tw/jurify-{cod_arqv}.pdf
  5. CALL spAtualizaArquivoProcesso(...)   ← preenche nome/keyfile/creds S3 do anexo
  6. CALL spAtulizaItens(..., 9, ..., 'E', ...)   ← (sic typo na SP) marca CONCLUÍDO

Idempotência: se `codStatus` já é 9, pulamos tudo (retry seguro).
SQL é sempre parametrizado (driver substitui %s), evitando o pattern de
f-string do Gateway original (vulnerável a injection).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.client import Config as BotoConfig

from .db import conexao

_log = logging.getLogger("rpa.jurify")

# Convenção do Gateway: keyfile = 'jurify-{CodArquivo}.pdf'.
# É essa string que o painel usa pra resolver o link do anexo — não inventar outra.
_FORMATO_KEY = "jurify-{}.pdf"

# Estado terminal que o painel reconhece como concluído.
_COD_STATUS_CONCLUIDO = 9
_STATUS_ROBO_ENCERRADO = "E"


class JurifyError(RuntimeError):
    pass


def _config_jurify() -> dict[str, str]:
    """Lê credenciais Jurify do .env. Falha cedo com mensagem clara."""
    access = os.getenv("RPA_JURIFY_AWS_ACCESS_KEY", "").strip()
    secret = os.getenv("RPA_JURIFY_AWS_SECRET_KEY", "").strip()
    if not access or not secret:
        raise JurifyError(
            "Credenciais AWS do bucket Jurify ausentes — "
            "preencha RPA_JURIFY_AWS_ACCESS_KEY/SECRET_KEY no .env."
        )
    return {
        "access_key": access,
        "secret_key": secret,
        "bucket": os.getenv("RPA_JURIFY_AWS_BUCKET", "netview-tw").strip() or "netview-tw",
        "region": os.getenv("RPA_JURIFY_AWS_REGION", "us-east-1").strip() or "us-east-1",
    }


def upload_recibo_painel(
    recibo_path: Path | str,
    cod_item: int,
    *,
    id_user: int | None = None,
    nome_user: str | None = None,
    nome_arquivo_painel: str | None = None,
) -> dict[str, Any]:
    """Sobe o PDF do recibo pro bucket Jurify e marca o CodItem como concluído.

    Retorna dict com `cod_arqv`, `keyfile`, `bucket`, `id_proc`, etc.
    Se o item já estiver concluído (codStatus=9), retorna `{"ja_concluido": True, ...}`
    sem tocar em nada (idempotente).
    """
    path = Path(recibo_path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Recibo não encontrado: {path}")

    cfg = _config_jurify()
    id_user = id_user if id_user is not None else int(os.getenv("RPA_JURIFY_USER_ID", "1"))
    nome_user = nome_user or os.getenv("RPA_JURIFY_USER_NAME", "ROBO.RPAPROTOCOLO")
    nome_arquivo_painel = nome_arquivo_painel or path.name
    now = datetime.now()

    # --- DB Fase A: idempotência + INSERT esqueleto + recovery do CodArquivo ---
    with conexao() as conn:
        with conn.cursor() as cur:
            # No schema real a coluna é `CodStatusCheckin` (a doc do gateway
            # abreviava como "codStatus"). A SP spAtulizaItens recebe o valor 9
            # e internamente seta CodStatusCheckin.
            cur.execute(
                "SELECT CodStatusCheckin, StatusRobo, IdProc FROM tbitens WHERE CodItem = %s",
                (cod_item,),
            )
            row = cur.fetchone()
            if not row:
                raise JurifyError(f"CodItem {cod_item} não existe em tbitens")
            if row.get("CodStatusCheckin") == _COD_STATUS_CONCLUIDO:
                _log.warning(
                    "CodItem=%s já está em CodStatusCheckin=%s (StatusRobo=%r) — pulando finalização",
                    cod_item, row["CodStatusCheckin"], row.get("StatusRobo"),
                )
                return {
                    "cod_item": cod_item,
                    "ja_concluido": True,
                    "cod_status": row["CodStatusCheckin"],
                    "status_robo": row.get("StatusRobo"),
                }
            id_proc = row["IdProc"]

            cur.execute(
                """
                INSERT INTO tbarquivosprocesso
                    (IdProc, codItem, CodEscritorioOrigem, NomeArquivo,
                     NomeArquivoWeb, DtUpload, CodUserInclui, Status)
                VALUES (%s, %s, 0, '', '', %s, %s, 1)
                """,
                (id_proc, cod_item, now, id_user),
            )
            cur.execute(
                """
                SELECT MAX(CodArquivo) AS m FROM tbarquivosprocesso
                WHERE IdProc = %s AND codItem = %s
                """,
                (id_proc, cod_item),
            )
            cod_arqv = cur.fetchone()["m"]

    keyfile = _FORMATO_KEY.format(cod_arqv)
    _log.info(
        "Jurify: IdProc=%s CodItem=%s CodArquivo=%s keyfile=%s",
        id_proc, cod_item, cod_arqv, keyfile,
    )

    # --- S3 upload ---
    try:
        s3 = boto3.client(
            "s3",
            aws_access_key_id=cfg["access_key"],
            aws_secret_access_key=cfg["secret_key"],
            region_name=cfg["region"],
            config=BotoConfig(connect_timeout=2, retries={"max_attempts": 60}),
        )
        with open(path, "rb") as f:
            s3.upload_fileobj(f, cfg["bucket"], keyfile)
        _log.info("upload S3 OK → s3://%s/%s", cfg["bucket"], keyfile)
    except Exception as e:
        # cleanup: a linha esqueleto fica órfã se a gente não remover
        _log.warning("upload S3 falhou — removendo linha esqueleto CodArquivo=%s", cod_arqv)
        try:
            with conexao() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM tbarquivosprocesso WHERE CodArquivo = %s",
                        (cod_arqv,),
                    )
        except Exception as cleanup_err:
            _log.error("falha removendo esqueleto: %s", cleanup_err)
        raise JurifyError(f"upload S3 falhou: {e}") from e

    # --- DB Fase B: SPs (preenche metadados + marca concluído) ---
    try:
        with conexao() as conn:
            with conn.cursor() as cur:
                cur.callproc("spAtualizaArquivoProcesso", (
                    cod_arqv,                # CodArquivo
                    nome_arquivo_painel,     # NomeArquivo (rótulo no painel)
                    keyfile,                 # NomeArquivoWeb
                    now,                     # DtAtualizacao
                    nome_user,               # UserAtualizacao
                    cfg["bucket"],           # BucketS3
                    keyfile,                 # NomeArquivoBucketS3
                    None,                    # LinkArquivoBucket
                    cfg["access_key"],       # AccessKey
                    cfg["secret_key"],       # SecretKey
                    cfg["region"],           # Region
                ))
                # OBS: nome da SP tem typo intencional do schema (sem o "A" do meio).
                cur.callproc("spAtulizaItens", (
                    cod_item,                    # CodItem
                    _COD_STATUS_CONCLUIDO,       # codStatus = 9
                    now,                         # DtAtualizacao
                    nome_user,                   # OrigemAtualizacao
                    nome_user,                   # UserAtualizacao
                    nome_user,                   # UserConclusao
                    _STATUS_ROBO_ENCERRADO,      # StatusRobo = 'E'
                    now,                         # DtConclusao
                ))
        _log.info(
            "Jurify: CodItem=%s marcado como CONCLUÍDO (codStatus=9, StatusRobo='E')",
            cod_item,
        )
    except Exception as e:
        # S3 já está com o arquivo; a linha em tbarquivosprocesso ficou com
        # NomeArquivo='' (esqueleto não populado) e o item não está em status 9.
        # Reportar o suficiente pra rotina de cleanup encontrar.
        _log.exception(
            "FALHA nas SPs após S3 OK — órfão: s3://%s/%s + tbarquivosprocesso.CodArquivo=%s",
            cfg["bucket"], keyfile, cod_arqv,
        )
        raise JurifyError(f"falha nas SPs (após upload S3): {e}") from e

    # --- Verificação pós-condição (opcional mas barato) ---
    with conexao() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT CodStatusCheckin, StatusRobo FROM tbitens WHERE CodItem = %s",
                (cod_item,),
            )
            check = cur.fetchone() or {}
    if check.get("CodStatusCheckin") != _COD_STATUS_CONCLUIDO or check.get("StatusRobo") != _STATUS_ROBO_ENCERRADO:
        _log.warning(
            "Pós-verificação: CodItem=%s não ficou com (CodStatusCheckin=9, StatusRobo='E') — está %r",
            cod_item, check,
        )

    return {
        "cod_item": cod_item,
        "id_proc": id_proc,
        "cod_arqv": cod_arqv,
        "keyfile": keyfile,
        "bucket": cfg["bucket"],
        "nome_arquivo_painel": nome_arquivo_painel,
        "ja_concluido": False,
    }
