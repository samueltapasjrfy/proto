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

Modo stealth (env `RPA_RJ_STEALTH=1`): usa `patchright` em vez de `playwright`
puro pra evitar detecção do Cloudflare Turnstile, que o TJRJ adicionou no
login em 2026. `patchright` corrige CDP leaks que delatam automação.
"""
from __future__ import annotations

import os

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

    # Login do RJ é instável: o Cloudflare/rate-limit às vezes rejeita um login
    # válido com alert espúrio "Senha ou usuário Inválidos". Retry resolve.
    LOGIN_MAX_TENTATIVAS = 4
    LOGIN_RETRY_BACKOFF_S = 8.0

    def start(self) -> None:
        # `patchright` é drop-in do playwright que esconde sinais de automação
        # (navigator.webdriver, CDP enable leaks, etc.) que o Cloudflare usa
        # pra detectar bots. Liga por DEFAULT no RJ (3/3 logins OK com stealth
        # vs rejeição espúria sem) — desligue com RPA_RJ_STEALTH=0.
        if os.getenv("RPA_RJ_STEALTH", "1") != "0":
            from patchright.sync_api import sync_playwright as _stealth_sync
            self.log.info("iniciando patchright stealth (headless=%s)", self.headless)
            self._playwright = _stealth_sync().start()
            self._browser = self._playwright.chromium.launch(headless=self.headless)
            self.context = self._browser.new_context()
            self.page = self.context.new_page()
        else:
            super().start()

        # Captura dialogs (alert/confirm/prompt) — eproc-RJ usa pop-ups JS pra
        # mensagens de Cloudflare/manutenção/erro; sem listener, page.goto trava
        # esperando o user fechar.
        def _on_dialog(dialog):
            try:
                self.log.warning(
                    "DIALOG %s capturado: %r — aceitando",
                    dialog.type, dialog.message,
                )
                dialog.accept()
            except Exception as e:
                self.log.warning("falha tratando dialog: %s", e)
        self.page.on("dialog", _on_dialog)

    def login(self):
        # Retry-loop: o RJ rejeita logins válidos de forma intermitente (rate-limit
        # / Cloudflare → alert "Senha ou usuário Inválidos" espúrio). Cada tentativa
        # recarrega o form do zero (super().login() faz goto). Backoff entre elas
        # dá cooldown pro rate-limit.
        ultimo_erro: Exception | None = None
        for tentativa in range(1, self.LOGIN_MAX_TENTATIVAS + 1):
            try:
                return super().login()
            except Exception as e:
                ultimo_erro = e
                self._dump_login_fail()
                if tentativa < self.LOGIN_MAX_TENTATIVAS:
                    espera = self.LOGIN_RETRY_BACKOFF_S * tentativa
                    self.log.warning(
                        "login RJ falhou (tentativa %d/%d): %s — retry em %.0fs",
                        tentativa, self.LOGIN_MAX_TENTATIVAS, e, espera,
                    )
                    self.page.wait_for_timeout(int(espera * 1000))
        assert ultimo_erro is not None
        raise ultimo_erro

    def _dump_login_fail(self) -> None:
        """Screenshot + dump do estado da tela pra investigar Cloudflare/modal/alert."""
        try:
            import time as _t
            shot = self.settings.logs_dir / f"rj_login_fail_{int(_t.time())}.png"
            self.page.screenshot(path=str(shot), full_page=True)
            self.log.warning("screenshot do login fail salvo em %s", shot)
            info = self.page.evaluate("""() => {
                const turnstile = !!document.querySelector('.cf-turnstile') || /Verify you are human/i.test(document.body?.innerText || '');
                const alerts = Array.from(document.querySelectorAll('[role="alert"], .alert, .modal, .swal2-popup, .infraMensagem')).map(e => (e.innerText || '').substring(0,200));
                return {
                    url: window.location.href,
                    title: document.title,
                    turnstile_visivel: turnstile,
                    alert_texts: alerts.slice(0, 5),
                    body_head: (document.body?.innerText || '').substring(0, 500),
                };
            }""")
            self.log.warning("estado da tela: %r", info)
        except Exception as e2:
            self.log.warning("falha capturando estado: %s", e2)
