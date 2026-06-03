"""Automação eproc-RS.

O eproc do TJRS roda a mesma aplicação eproc do TJMG, exceto pelo login que
é via Keycloak SSO (form com IDs `username`/`password`/`kc-login`, hospedado
em `keycloak-eks.tjrs.jus.br`). Pós-login, redireciona pro mesmo eproc com
os mesmos seletores de movimentação/peticionar/confirmar.

Diferenças em relação ao MG:
- `LOGIN_URL` (domínio do eproc-RS)
- Seletores do form de login/2FA (Keycloak usa outros IDs)
- Tela intermediária 'Seleção de perfil' depois do 2FA — precisa escolher
  um perfil (advogado/procurador) antes de chegar no painel.
"""
from __future__ import annotations

import os
import re

from ..eproc_mg.adapter import EprocLoginError, EprocMGAdapter


class EprocRSAdapter(EprocMGAdapter):
    TRIBUNAL_ID = "eproc_rs"
    LOGIN_URL = "https://eproc1g.tjrs.jus.br/eproc/"

    # Form de login via Keycloak (não os IDs `txtUsuario/pwdSenha/sbmEntrar` do MG).
    SEL_USUARIO = "#username"
    SEL_SENHA = "#password"
    SEL_SUBMIT = "#kc-login"

    # 2FA também é via Keycloak — campo `#otp`, botão reusa `#kc-login`.
    SEL_2FA_CODIGO = "#otp"
    SEL_2FA_BTN = "#kc-login"

    # Título da página intermediária pós-2FA.
    TXT_SELECAO_PERFIL = "Seleção de perfil"

    # Preferência de perfil — sobrescreva via env se quiser.
    # Default: sigla começando com 'RS' seguida de alfanumérico (RS086106A etc).
    # O '\w+' evita casar com o rotulinho "RS" do header lateral.
    PERFIL_REGEX_DEFAULT = r"^RS\w+"

    # Termos que confirmam que é uma linha de perfil (cargo) e não outra coisa
    # qualquer com prefixo RS na tela.
    _RE_CARGO = r"\b(ADVOGADO|PROCURADOR|ASSISTENTE)\b"

    # Mensagens de erro de 2FA do Keycloak — vimos em casos reais que aparecem
    # mesmo quando o secret está correto (flake de timing entre janelas TOTP).
    _PADROES_RETRY_KEYCLOAK = (
        "código de uso único inválido",
        "codigo de uso unico invalido",
        "invalid otp",
        "invalid authenticator code",
    )

    def _mensagem_erro_2fa(self) -> str | None:
        """Inclui as mensagens do Keycloak além das do MG."""
        msg = super()._mensagem_erro_2fa()
        if msg:
            return msg
        if not self.page:
            return None
        # Procura textos de erro vermelhos típicos do Keycloak
        for pat in (
            "Código de uso único inválido",
            "Invalid OTP",
            "Authentication failed",
        ):
            try:
                loc = self.page.get_by_text(re.compile(pat, re.IGNORECASE))
                if loc.count() > 0 and loc.first.is_visible():
                    return loc.first.inner_text().strip()
            except Exception:
                continue
        return None

    def _classificar_erro_2fa(self, msg: str) -> str:
        """No Keycloak do RS, "código inválido" raramente é secret errado —
        quase sempre é flake de timing TOTP (janela flipou entre gerar e submeter).
        Tratar como 'reutilizado' pra disparar retry automático com nova janela.
        """
        low = msg.lower()
        if any(p in low for p in self._PADROES_RETRY_KEYCLOAK):
            return "reutilizado"
        return super()._classificar_erro_2fa(msg)

    def _apos_2fa_ok(self) -> bool:
        """No RS, "passou do 2FA" também inclui a tela de Seleção de perfil
        (intermediária, antes do painel real)."""
        assert self.page is not None
        try:
            title = self.page.title() or ""
            if self.TXT_SELECAO_PERFIL in title:
                return True
        except Exception:
            pass
        return super()._apos_2fa_ok()

    def _aguardar_pos_submit(self, timeout_s: int):
        """No Keycloak (RS/SP), há telas intermediárias entre login e painel
        em que `#otp` já está no DOM mas escondido. A guarda do MG que exige
        `SEL_2FA_CODIGO count==0` derruba esse caso. Aqui aceitamos a
        heurística baseada só em URL + ausência de `#username`.
        """
        import time as _t
        assert self.page is not None
        inicio = _t.time()
        fim = inicio + timeout_s
        delay_heuristica = 3.0
        while _t.time() < fim:
            if self._campo_2fa_visivel():
                return "2fa"
            if self._painel_carregado():
                return "painel"
            try:
                if (
                    _t.time() - inicio > delay_heuristica
                    and self.page.locator(self.SEL_USUARIO).count() == 0
                    and "login" not in self.page.url.lower()
                    and "eproc" in self.page.url.lower()
                ):
                    return "painel"
            except Exception:
                pass
            self.page.wait_for_timeout(250)
        return None

    def _finalizar_login(self) -> None:
        """Se caímos na 'Seleção de perfil', escolhe o perfil da UF do tribunal
        atual (regex configurável via `RPA_EPROC_{UF}_PERFIL_REGEX` onde UF é
        derivado de `TRIBUNAL_ID` — ex.: RPA_EPROC_RS_PERFIL_REGEX, RPA_EPROC_SP_PERFIL_REGEX).

        Pra peticionar processos do TJRS é preciso usar uma OAB/Procuradoria
        habilitada no RS — senão a petição vai ser associada à OAB de outra UF.
        Se nenhum perfil RS for encontrado, ABORTA — não usa fallback pra MG.

        Cada linha de perfil é um `<button>` com `data-descricao="SIGLA / CARGO"`
        (ex.: `data-descricao="RS086106A / PROCURADOR"`). Usamos esse atributo
        como fonte de verdade — mais robusto que enumerar tags.
        """
        assert self.page is not None
        title = self.page.title() or ""
        if self.TXT_SELECAO_PERFIL not in title:
            return

        # Var de env por tribunal: RPA_EPROC_RS_PERFIL_REGEX, RPA_EPROC_SP_PERFIL_REGEX, ...
        env_var = f"RPA_{self.TRIBUNAL_ID.upper()}_PERFIL_REGEX"
        regex_str = os.getenv(env_var, self.PERFIL_REGEX_DEFAULT)
        self.log.info("Tela de Seleção de perfil — buscando perfil que case %r", regex_str)
        pattern = re.compile(regex_str, re.IGNORECASE)

        # Espera o conteúdo dos perfis renderizar (cargo aparece no body)
        import time as _t
        cargo_re = re.compile(self._RE_CARGO, re.IGNORECASE)
        deadline = _t.time() + 15
        while _t.time() < deadline:
            body = (self.page.locator("body").inner_text() or "")
            if cargo_re.search(body):
                break
            self.page.wait_for_timeout(300)
        else:
            self.log.warning("conteúdo da Seleção não apareceu em 15s — seguindo mesmo assim")

        # Procura nos frames por elementos com data-descricao
        escolhido = None
        frame_escolhido = None
        perfis_vistos: list[str] = []
        for frame in self.page.frames:
            try:
                cands = frame.evaluate(
                    """() => Array.from(document.querySelectorAll('[data-descricao]'))
                        .filter(el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 || r.height > 0;
                        })
                        .map(el => el.getAttribute('data-descricao'))"""
                )
            except Exception:
                continue
            if not cands:
                continue
            perfis_vistos.extend(cands)
            self.log.info("frame %r: perfis disponíveis: %s", frame.url[:50], cands)
            matches = [d for d in cands if pattern.search(d)]
            if matches:
                escolhido = matches[0]
                frame_escolhido = frame
                self.log.info("perfil escolhido: %r", escolhido)
                break

        if escolhido is None or frame_escolhido is None:
            try:
                shot = self.settings.logs_dir / f"selecao_perfil_{int(_t.time())}.png"
                self.page.screenshot(path=str(shot), full_page=True)
                self.log.error("screenshot da tela em %s", shot)
            except Exception:
                pass
            raise EprocLoginError(
                f"Nenhum perfil RS encontrado (regex={regex_str!r}). "
                f"Perfis disponíveis: {perfis_vistos}. "
                f"Pra peticionar em processos RS é obrigatório ter um perfil "
                f"habilitado no RS — sem isso a petição ficaria com OAB de outra UF."
            )

        # Clica via JS por seletor CSS no atributo data-descricao
        frame_escolhido.evaluate(
            """(desc) => {
                // Escapa aspas e seleciona pelo atributo
                const sel = `[data-descricao="${desc.replace(/"/g, '\\\\"')}"]`;
                document.querySelector(sel)?.click();
            }""",
            escolhido,
        )

        # Aguarda painel carregar
        fim = _t.time() + 25
        while _t.time() < fim:
            if self._painel_carregado():
                self.log.info("perfil aceito — painel carregado")
                return
            self.page.wait_for_timeout(500)
        raise EprocLoginError(
            f"Painel não carregou em 25s após seleção de perfil. "
            f"url={self.page.url!r} title={self.page.title()!r}"
        )
