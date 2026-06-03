"""Automação eproc-RJ.

Híbrido entre MG e RS/SP:
- Login *direto* no eproc (sem Keycloak SSO), com IDs idênticos ao MG
  (`#txtUsuario`, `#pwdSenha`, `#sbmEntrar`, `#txtAcessoCodigo`, `#btnValidar`).
  A página inicial `/eproc/` é pública; o form vive em `externo_controlador.php?acao=principal`.
- Pós-2FA cai em uma tela 'Seleção de perfil' (igual RS/SP) — precisa
  escolher um perfil RJ antes de chegar no painel. Toda a lógica de
  seleção de perfil vem do `EprocRSAdapter`.

Pra evitar duplicação, herdamos de `EprocRSAdapter` e sobrescrevemos os
seletores Keycloak voltando-os pros do MG/SIP do próprio eproc.
"""
from __future__ import annotations

from ..eproc_mg.adapter import EprocMGAdapter
from ..eproc_rs.adapter import EprocRSAdapter


class EprocRJAdapter(EprocRSAdapter):
    TRIBUNAL_ID = "eproc_rj"
    LOGIN_URL = "https://eproc1g.tjrj.jus.br/eproc/externo_controlador.php?acao=principal"

    # Sem Keycloak — IDs do form de login são os mesmos do MG.
    SEL_USUARIO = EprocMGAdapter.SEL_USUARIO
    SEL_SENHA = EprocMGAdapter.SEL_SENHA
    SEL_SUBMIT = EprocMGAdapter.SEL_SUBMIT
    SEL_2FA_CODIGO = EprocMGAdapter.SEL_2FA_CODIGO
    SEL_2FA_BTN = EprocMGAdapter.SEL_2FA_BTN

    # Sigla RJ (ex.: RJ123456A). Configurável via `RPA_EPROC_RJ_PERFIL_REGEX`.
    PERFIL_REGEX_DEFAULT = r"^RJ\w+"
