"""Versão async da Fase 2 (eproc-RS).

Pós-login, o RS opera no mesmo eproc do MG (mesmos seletores de consulta,
movimentação, peticionar). Por isso reusa toda a lógica do MG async flow.
A única diferença que importa pós-login (perfil) já foi resolvida no sync.
"""
from __future__ import annotations

from ..eproc_mg.async_flow import EprocMGAsyncFlow


class EprocRSAsyncFlow(EprocMGAsyncFlow):
    """Idêntico ao MG na Fase 2; aqui só por clareza/futura extensão."""
    pass
