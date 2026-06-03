"""Logger compartilhado: stdout + arquivos em data/logs/."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False


def setup(logs_dir: Path, level: int = logging.INFO) -> None:
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

    full = RotatingFileHandler(logs_dir / "rpa.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    full.setLevel(logging.DEBUG)
    full.setFormatter(fmt)
    root.addHandler(full)

    errors = RotatingFileHandler(logs_dir / "errors.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    errors.setLevel(logging.ERROR)
    errors.setFormatter(fmt)
    root.addHandler(errors)

    _CONFIGURED = True


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"rpa.{name}")
