"""Logger compartilhado: stdout + arquivos em data/logs/."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False


def setup(logs_dir: Path, level: int = logging.INFO, suffix: str | None = None) -> None:
    """Configura logger root da app.

    `suffix` quando setado é usado pro nome do arquivo (rpa-{suffix}.log /
    errors-{suffix}.log) — essencial em subprocessos paralelos, senão dois
    `RotatingFileHandler` apontando pro mesmo arquivo brigam pela rotação
    e perdem mensagens.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    logs_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("rpa")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setLevel(level)
    stdout.setFormatter(fmt)
    root.addHandler(stdout)

    tag = f"-{suffix}" if suffix else ""
    full = RotatingFileHandler(
        logs_dir / f"rpa{tag}.log",
        maxBytes=2_000_000, backupCount=3, encoding="utf-8",
    )
    full.setLevel(logging.DEBUG)
    full.setFormatter(fmt)
    root.addHandler(full)

    errors = RotatingFileHandler(
        logs_dir / f"errors{tag}.log",
        maxBytes=1_000_000, backupCount=3, encoding="utf-8",
    )
    errors.setLevel(logging.ERROR)
    errors.setFormatter(fmt)
    root.addHandler(errors)

    _CONFIGURED = True


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"rpa.{name}")
