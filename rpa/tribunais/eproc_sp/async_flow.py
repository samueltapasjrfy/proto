"""Async flow eproc-SP — idêntico ao MG após login (mesmo eproc).

Diferença operacional: workers SP rodam cada um em sua própria sessão
(BrowserContext isolado), porque o eproc-SP penaliza concorrência interna
da mesma sessão durante a Fase 2 (Movimentar/Peticionar). O isolamento
de sessão é feito no main (N logins → N contexts), não aqui.
"""
from __future__ import annotations

from ..eproc_mg.async_flow import EprocMGAsyncFlow


class EprocSPAsyncFlow(EprocMGAsyncFlow):
    pass
