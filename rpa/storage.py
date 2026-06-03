"""Persistência em JSON com escrita atômica.

Substitui as tabelas `clientes_eproc` e `cliente_cookiejar_eproc` do SQL Server.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .crypto import Cipher
from .models import Cliente, CookieJar

ENV_CLIENTE_ID = "env"


def _atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


class ClienteStore:
    """CRUD de clientes em data/clientes.json. Senha/TOTP ficam cifrados em repouso."""

    def __init__(self, clientes_file: Path, cipher: Cipher):
        self.file = clientes_file
        self.cipher = cipher

    def _read_all(self) -> dict[str, dict]:
        return _read_json(self.file, default={})

    def _write_all(self, data: dict[str, dict]) -> None:
        _atomic_write_json(self.file, data)

    def get(self, cliente_id: str | int) -> Cliente:
        data = self._read_all()
        key = str(cliente_id)
        if key not in data:
            raise KeyError(f"Cliente id={key} não encontrado em {self.file}.")
        return Cliente.from_dict(data[key])

    def list(self) -> list[Cliente]:
        return [Cliente.from_dict(v) for v in self._read_all().values()]

    def upsert(
        self,
        *,
        id: str | int,
        nome: str,
        tribunal: str,
        usuario: str,
        senha: str,
        totp_secret: str | None = None,
    ) -> Cliente:
        data = self._read_all()
        key = str(id)
        criado_em = data.get(key, {}).get("criado_em")
        cliente = Cliente(
            id=key,
            nome=nome,
            tribunal=tribunal,
            usuario=usuario,
            senha_cifrada=self.cipher.encrypt(senha),  # type: ignore[arg-type]
            totp_secret_cifrado=self.cipher.encrypt(totp_secret),
            criado_em=criado_em or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            atualizado_em=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        data[key] = cliente.to_dict()
        self._write_all(data)
        return cliente

    def delete(self, cliente_id: str | int) -> bool:
        data = self._read_all()
        key = str(cliente_id)
        if key not in data:
            return False
        del data[key]
        self._write_all(data)
        return True

    # ---- decifragem sob demanda ----
    def credenciais(self, cliente_id: str | int) -> tuple[str, str, str | None]:
        """Retorna (usuario, senha_plaintext, totp_secret_plaintext|None)."""
        cliente = self.get(cliente_id)
        return (
            cliente.usuario,
            self.cipher.decrypt(cliente.senha_cifrada),  # type: ignore[return-value]
            self.cipher.decrypt(cliente.totp_secret_cifrado),
        )


class EnvClienteStore:
    """Cliente único cujas credenciais vêm do .env (modo single-client).

    Mesma interface pública de `ClienteStore` (`get`, `credenciais`) — o adapter
    consome qualquer um dos dois sem saber a diferença.
    """

    def __init__(
        self,
        *,
        tribunal: str,
        usuario: str,
        senha: str,
        totp_secret: str | None = None,
    ):
        if not usuario or not senha:
            raise RuntimeError(
                "Credenciais do .env incompletas. Preencha EPROC_MG_USUARIO e EPROC_MG_SENHA."
            )
        self._cliente = Cliente(
            id=ENV_CLIENTE_ID,
            nome="(credenciais do .env)",
            tribunal=tribunal,
            usuario=usuario,
            senha_cifrada="",  # não usado neste modo
            totp_secret_cifrado=None,
        )
        self._creds: tuple[str, str, str | None] = (usuario, senha, totp_secret)

    def get(self, cliente_id: str | int) -> Cliente:
        return self._cliente

    def credenciais(self, cliente_id: str | int) -> tuple[str, str, str | None]:
        return self._creds

    @classmethod
    def from_env_eproc_mg(cls) -> "EnvClienteStore":
        return cls(
            tribunal="eproc_mg",
            usuario=os.getenv("EPROC_MG_USUARIO", "").strip(),
            senha=os.getenv("EPROC_MG_SENHA", ""),
            totp_secret=(os.getenv("EPROC_MG_TOTP_SECRET", "").strip() or None),
        )

    @classmethod
    def from_env_eproc_rs(cls) -> "EnvClienteStore":
        return cls(
            tribunal="eproc_rs",
            usuario=os.getenv("EPROC_RS_USUARIO", "").strip(),
            senha=os.getenv("EPROC_RS_SENHA", ""),
            totp_secret=(os.getenv("EPROC_RS_TOTP_SECRET", "").strip() or None),
        )

    @classmethod
    def from_env_eproc_sp(cls) -> "EnvClienteStore":
        return cls(
            tribunal="eproc_sp",
            usuario=os.getenv("EPROC_SP_USUARIO", "").strip(),
            senha=os.getenv("EPROC_SP_SENHA", ""),
            totp_secret=(os.getenv("EPROC_SP_TOTP_SECRET", "").strip() or None),
        )

    @classmethod
    def from_env_eproc_rj(cls) -> "EnvClienteStore":
        return cls(
            tribunal="eproc_rj",
            usuario=os.getenv("EPROC_RJ_USUARIO", "").strip(),
            senha=os.getenv("EPROC_RJ_SENHA", ""),
            totp_secret=(os.getenv("EPROC_RJ_TOTP_SECRET", "").strip() or None),
        )

    @classmethod
    def from_env(cls, tribunal: str) -> "EnvClienteStore":
        """Dispatcher por tribunal — facilita escalar pra novos eprocs."""
        try:
            factory = getattr(cls, f"from_env_{tribunal}")
        except AttributeError:
            raise RuntimeError(f"Tribunal '{tribunal}' sem credenciais .env mapeadas") from None
        return factory()


class CookieStore:
    """Cookies por cliente em data/cookies/{id}.json."""

    def __init__(self, cookies_dir: Path):
        self.dir = cookies_dir

    def _path(self, cliente_id: str | int) -> Path:
        return self.dir / f"{cliente_id}.json"

    def save(self, jar: CookieJar) -> None:
        _atomic_write_json(self._path(jar.cliente_id), jar.to_dict())

    def load(self, cliente_id: str | int) -> CookieJar | None:
        path = self._path(cliente_id)
        if not path.exists():
            return None
        return CookieJar.from_dict(_read_json(path, default={}))

    def delete(self, cliente_id: str | int) -> bool:
        path = self._path(cliente_id)
        if path.exists():
            path.unlink()
            return True
        return False
