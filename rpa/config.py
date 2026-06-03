"""Carrega configuração do .env e expõe paths/flags do projeto."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    master_key: str | None
    data_dir: Path
    cookies_dir: Path
    logs_dir: Path
    clientes_file: Path
    headless: bool
    login_timeout: int

    @classmethod
    def load(cls) -> "Settings":
        master_key = os.getenv("RPA_MASTER_KEY", "").strip() or None

        data_dir = Path(os.getenv("RPA_DATA_DIR", PROJECT_ROOT / "data")).resolve()
        cookies_dir = data_dir / "cookies"
        logs_dir = data_dir / "logs"
        clientes_file = data_dir / "clientes.json"

        for d in (data_dir, cookies_dir, logs_dir):
            d.mkdir(parents=True, exist_ok=True)

        return cls(
            master_key=master_key,
            data_dir=data_dir,
            cookies_dir=cookies_dir,
            logs_dir=logs_dir,
            clientes_file=clientes_file,
            headless=_bool(os.getenv("RPA_HEADLESS"), default=False),
            login_timeout=int(os.getenv("RPA_LOGIN_TIMEOUT", "25")),
        )


