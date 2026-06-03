"""Automação eproc-SP.

Mesma estrutura do RS:
- Login via Keycloak SSO (hospedado em `sso.tjsp.jus.br`)
- Tela intermediária 'Seleção de perfil' depois do 2FA
- Pós-login, o eproc é a mesma aplicação dos outros tribunais

Por isso herda direto de `EprocRSAdapter`, sobrescrevendo apenas:
- `LOGIN_URL` (domínio do eproc-SP)
- `PERFIL_REGEX_DEFAULT` (sigla SP em vez de RS)
"""
from __future__ import annotations

from ..eproc_rs.adapter import EprocRSAdapter


class EprocSPAdapter(EprocRSAdapter):
    TRIBUNAL_ID = "eproc_sp"
    LOGIN_URL = "https://eproc1g.tjsp.jus.br/eproc/"

    # Sigla SP do tribunal (ex.: SP123456A). Configurável via
    # `RPA_EPROC_SP_PERFIL_REGEX` se a OAB cadastrada lá for de outra UF.
    PERFIL_REGEX_DEFAULT = r"^SP\w+"
