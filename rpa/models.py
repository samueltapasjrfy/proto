"""Modelos de domínio."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Cliente:
    id: str
    nome: str
    tribunal: str
    usuario: str
    senha_cifrada: str
    totp_secret_cifrado: str | None = None
    criado_em: str = field(default_factory=_now_iso)
    atualizado_em: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Cliente":
        # tolera chaves extras / faltantes
        return cls(
            id=str(data["id"]),
            nome=data.get("nome", ""),
            tribunal=data.get("tribunal", "eproc_mg"),
            usuario=data["usuario"],
            senha_cifrada=data["senha_cifrada"],
            totp_secret_cifrado=data.get("totp_secret_cifrado"),
            criado_em=data.get("criado_em", _now_iso()),
            atualizado_em=data.get("atualizado_em", _now_iso()),
        )


@dataclass
class ConsultaProcessoResultado:
    numero: str
    encontrado: bool
    numero_formatado: str | None = None  # ex.: '1003897-27.2026.8.13.0145'
    url: str | None = None
    titulo: str | None = None
    erro: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class CookieJar:
    cliente_id: str
    tribunal: str
    cookies: list[dict[str, Any]]
    atualizado_em: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CookieJar":
        return cls(
            cliente_id=str(data["cliente_id"]),
            tribunal=data["tribunal"],
            cookies=data["cookies"],
            atualizado_em=data.get("atualizado_em", _now_iso()),
        )
