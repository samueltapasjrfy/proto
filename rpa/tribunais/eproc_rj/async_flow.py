"""Async flow eproc-RJ — idêntico ao MG após login (mesmo eproc).

Hipótese inicial: RJ não tem a penalidade server-side de concorrência da mesma
sessão que o SP apresenta. Se na prática manifestar, replicar o esquema de
1-sessão-por-worker do SP no `_processar_grupo_async` em main.py.
"""
from __future__ import annotations

from ..eproc_mg.async_flow import EprocMGAsyncFlow


class EprocRJAsyncFlow(EprocMGAsyncFlow):
    pass
