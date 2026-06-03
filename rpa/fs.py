"""Utilitários de filesystem — nomeação segura de arquivos."""
from __future__ import annotations

import re
import unicodedata
from pathlib import PurePath


def sanitizar_nome(nome: str, *, fallback: str = "arquivo") -> str:
    """Devolve uma versão do nome com `[A-Za-z0-9_-]` no stem e apenas a extensão
    final preservada com `.`.

    - NFKD → descarta não-ASCII
    - Qualquer `.` no meio do stem vira `_` (eproc/qq-uploader às vezes rejeita
      silenciosamente nomes com múltiplos pontos, como CNJ embutido)
    - Tudo fora de `[A-Za-z0-9_-]` no stem vira `_`
    - Idempotente
    """
    nfkd = unicodedata.normalize("NFKD", str(nome))
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    p = PurePath(ascii_only)
    stem, suffix = p.stem, p.suffix.lower()
    stem_clean = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")
    suffix_clean = re.sub(r"[^A-Za-z0-9]+", "", suffix)
    if not stem_clean:
        return fallback
    return f"{stem_clean}.{suffix_clean}" if suffix_clean else stem_clean
