"""Download de arquivos do S3 com credenciais por arquivo.

O Jurify armazena os anexos dos processos em buckets dos próprios clientes —
cada linha em `tbarquivosprocesso` traz o bucket, a key e o par
(access_key, secret_key, region). Esse módulo recebe esses ponteiros e baixa
pra um Path local. Clientes boto3 são cacheados por (access, secret, region)
pra evitar handshake repetido quando vários arquivos compartilham credencial.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import boto3
from botocore.config import Config


@lru_cache(maxsize=32)
def _client(access_key: str, secret_key: str, region: str):
    return boto3.client(
        "s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def baixar(
    *,
    bucket: str,
    key: str,
    access_key: str,
    secret_key: str,
    region: str,
    destino: Path,
    pular_se_existir: bool = True,
) -> bool:
    """Baixa um objeto do S3 para `destino`.

    Retorna `True` se baixou agora, `False` se pulou porque já existia.
    Levanta a exceção do boto3/botocore em falha.
    """
    destino = Path(destino)
    if pular_se_existir and destino.exists() and destino.stat().st_size > 0:
        return False
    destino.parent.mkdir(parents=True, exist_ok=True)
    client = _client(access_key, secret_key, region)
    client.download_file(bucket, key, str(destino))
    return True
