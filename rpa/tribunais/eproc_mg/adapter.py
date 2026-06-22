"""Automação eproc-MG.

Etapas: login (com/sem 2FA) e consulta processual. Próximas etapas (protocolo
de petição, juntada, etc.) entram como métodos novos nesta classe.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from ...models import ConsultaProcessoResultado
from ...totp import InvalidTOTPSecret, gerar_codigo
from ..base import BaseAdapter


class EprocLoginError(RuntimeError):
    pass


class EprocConsultaError(RuntimeError):
    pass


class EprocPeticionarError(RuntimeError):
    pass


class EprocMGAdapter(BaseAdapter):
    TRIBUNAL_ID = "eproc_mg"
    LOGIN_URL = "https://eproc1g.tjmg.jus.br/eproc/"

    # ---- seletores: login ----
    SEL_USUARIO = "#txtUsuario"
    SEL_SENHA = "#pwdSenha"
    SEL_SUBMIT = "#sbmEntrar"
    SEL_2FA_CODIGO = "#txtAcessoCodigo"
    SEL_2FA_BTN = "#btnValidar"
    TXT_PAINEL = "Painel do Advogado"
    # ---- captcha de login (InfraCaptcha, adicionado pelo TJMG em jun/2026) ----
    SEL_CAPTCHA_INPUT = "#txtInfraCaptcha"
    SEL_CAPTCHA_AUDIO_BTN = "#infraImgAudioCaptcha"
    SEL_CAPTCHA_ENVIAR = 'button[value="Enviar"]'
    URL_CAPTCHA_AUDIO = "/infra_js/infra_gerar_audio_captcha.php"
    CAPTCHA_MAX_TENTATIVAS = 6

    # ---- seletores: consulta processual ----
    SEL_MENU_CONSULTA = 'a[aria-label="Consulta Processual"]'
    SEL_MENU_SUB_CONSULTAR = 'a[aria-label="Consultar Processos"], a:has(span:text-is("Consultar Processos"))'
    SEL_NUM_PROCESSO = "#numNrProcesso"
    SEL_BTN_CONSULTAR = "#sbmConsultar"

    # ---- seletores: movimentar / peticionar ----
    SEL_BTN_MOVIMENTAR = 'a.infraButton:has-text("Movimentar/Peticionar")'
    SEL_EVENTO_INPUT = "#txtEvento"
    SEL_FLD_PRAZO = "#fldPrazo"
    SEL_CHK_PRAZO = 'input[name="selPrazo[]"]'

    # ---- seletores: documentos (templates, n = índice 1-based do Documento N) ----
    SEL_DOC_FIELDSET_TPL = "#fldInfDocumento{n}"
    SEL_DOC_FILE_INPUT_TPL = "#fldInfDocumento{n} input[type=file]"
    SEL_DOC_UPLOAD_LIST_TPL = "#fldInfDocumento{n} .qq-upload-list"
    SEL_DOC_FLE_HIDDEN_TPL = "#fleArquivo_{n}"
    SEL_DOC_TIPO_INPUT_TPL = "#txtTipo_{n}"
    SEL_DOC_TIPO_HIDDEN_TPL = "#selTipoArquivo_{n}"
    SEL_BTN_ADICIONAR_DOC = "#lblAdicionarDocumento"
    SEL_BTN_CONFIRMAR_DOCS = "#btnEnviarArquivos"
    # OBS: 4 botões compartilham `id="sbmMovimentar"` (Peticionar e Movimentação
    # Sucessiva, top+bottom bar). Distinguimos pelo accesskey="t" do Peticionar
    # (Mov. Sucessiva usa "s"). Mesmo assim sobram 2 (top+bottom) — usamos .first.
    SEL_BTN_PETICIONAR = 'button[accesskey="t"][value="Movimentar"]'

    def login(self) -> dict[str, Any]:
        if self.page is None:
            raise RuntimeError("Adapter não iniciado. Use 'with EprocMGAdapter(...) as a:' ou chame start().")

        usuario, senha, totp_secret = self.cliente_store.credenciais(self.cliente.id)
        if not usuario or not senha:
            raise EprocLoginError("usuario/senha vazios após decifrar — verifique o cadastro do cliente.")

        self.log.info("acessando %s", self.LOGIN_URL)
        self.page.goto(self.LOGIN_URL)
        self.page.fill(self.SEL_USUARIO, usuario)
        self.page.fill(self.SEL_SENHA, senha)
        self.page.click(self.SEL_SUBMIT)

        # eproc-MG passou a exigir captcha no login (jun/2026). Se a tela de
        # captcha aparecer, resolve via áudio (STT) antes de esperar o 2FA.
        self._resolver_captcha_se_necessario()

        estado = self._aguardar_pos_submit(timeout_s=self.settings.login_timeout)

        if estado == "2fa":
            if not totp_secret:
                raise EprocLoginError("Tela de 2FA exibida, mas o cliente não tem totp_secret cadastrado.")
            self._resolver_2fa(totp_secret)
            estado = "painel"
        elif estado == "painel":
            self.log.info("login sem 2FA")
        else:
            raise EprocLoginError(
                f"Não foi possível detectar 2FA nem painel em {self.settings.login_timeout}s."
            )

        # Hook pra etapas extras após 2FA (no MG é no-op; no RS clica em perfil etc.)
        self._finalizar_login()

        jar = self.persistir_cookies()
        return {"estado": estado, "cookies": jar.cookies}

    # ---- internos ----
    def _captcha_presente(self) -> bool:
        assert self.page is not None
        try:
            loc = self.page.locator(self.SEL_CAPTCHA_INPUT)
            return loc.count() > 0 and loc.is_visible()
        except Exception:
            return False

    def _resolver_captcha_se_necessario(self) -> None:
        """Se a tela de captcha do eproc aparecer após o submit, resolve via
        áudio (STT) num retry-loop — cada submit errado regenera o captcha.

        No-op se não houver captcha. Levanta EprocLoginError se não resolver
        em CAPTCHA_MAX_TENTATIVAS.
        """
        assert self.page is not None
        self.page.wait_for_timeout(1500)  # deixa a tela de captcha montar
        if not self._captcha_presente():
            return  # sem captcha — fluxo antigo

        from ...captcha_audio import transcrever_codigo

        # Base do eproc pro endpoint do áudio. NÃO usar LOGIN_URL direto: no RJ
        # ele tem path+query ('.../eproc/externo_controlador.php?acao=principal'),
        # o que montaria uma URL quebrada. Extrai até '/eproc'.
        m = re.match(r"(https?://[^/]+/eproc)", self.LOGIN_URL)
        base = m.group(1) if m else self.LOGIN_URL.rstrip("/")
        for tentativa in range(1, self.CAPTCHA_MAX_TENTATIVAS + 1):
            try:
                # dispara a geração do áudio e baixa o WAV (mesma sessão = mesmo código)
                self.page.locator(self.SEL_CAPTCHA_AUDIO_BTN).click()
                self.page.wait_for_timeout(1200)
                resp = self.context.request.get(f"{base}{self.URL_CAPTCHA_AUDIO}")
                codigo = transcrever_codigo(resp.body())
            except Exception as e:
                self.log.warning("captcha tentativa %d: erro baixando/transcrevendo áudio: %s",
                                  tentativa, e)
                codigo = ""

            if not codigo:
                self.log.info("captcha tentativa %d: transcrição vazia — regenerando", tentativa)
                continue

            self.page.fill(self.SEL_CAPTCHA_INPUT, codigo)
            self.page.locator(self.SEL_CAPTCHA_ENVIAR).first.click()
            self.page.wait_for_timeout(4000)

            if not self._captcha_presente():
                self.log.info("captcha resolvido na tentativa %d (código=%r)", tentativa, codigo)
                return
            self.log.info("captcha tentativa %d falhou (código=%r) — nova tentativa",
                          tentativa, codigo)

        raise EprocLoginError(
            f"captcha de áudio não resolvido em {self.CAPTCHA_MAX_TENTATIVAS} tentativas."
        )

    def _aguardar_pos_submit(self, timeout_s: int) -> str | None:
        """Retorna '2fa', 'painel' ou None."""
        assert self.page is not None
        inicio = time.time()
        fim = inicio + timeout_s
        # Tempo mínimo antes da heurística disparar — protege contra a janela
        # de redirect onde `#txtUsuario` já sumiu mas `#txtAcessoCodigo`
        # ainda não montou (visto em RJ). 3s cobre o caso na prática.
        delay_heuristica = 3.0
        while time.time() < fim:
            if self._campo_2fa_visivel():
                return "2fa"
            if self._painel_carregado():
                return "painel"
            try:
                if (
                    time.time() - inicio > delay_heuristica
                    and self.page.locator(self.SEL_USUARIO).count() == 0
                    and self.page.locator(self.SEL_2FA_CODIGO).count() == 0
                    and "login" not in self.page.url.lower()
                    and "eproc" in self.page.url.lower()
                ):
                    return "painel"
            except Exception:
                pass
            self.page.wait_for_timeout(250)
        return None

    def _campo_2fa_visivel(self) -> bool:
        assert self.page is not None
        try:
            loc = self.page.locator(self.SEL_2FA_CODIGO)
            return loc.count() > 0 and loc.is_visible()
        except Exception:
            return False

    def _painel_carregado(self) -> bool:
        """Detecta que estamos autenticados de verdade — não só num estado
        transitório com título já trocado mas sessão ainda não estabelecida.

        Critérios (todos obrigatórios):
        - Título bate em 'Painel d[oa] X' (Advogado/Procurador/Estagiário...)
          OU contém 'Sistema Eproc'
        - Campo de login (#txtUsuario) não está mais no DOM
        - Cookie PHPSESSID presente (prova de sessão server-side)
        - Menu lateral renderizado (o link 'Consulta Processual' existe)
        """
        assert self.page is not None
        try:
            title = self.page.title() or ""
            # Aceita qualquer perfil — Advogado, Procurador, Assistente, etc.
            tem_painel = bool(re.search(r"Painel d[oa] \w", title, re.IGNORECASE))
            if not tem_painel and "Sistema Eproc" not in title:
                return False
            if self.page.locator(self.SEL_USUARIO).count() > 0:
                return False
            cookies = self.context.cookies() if self.context else []
            if not any(c.get("name") == "PHPSESSID" for c in cookies):
                return False
            # menu lateral é o que usamos depois — se não está aí, não estamos prontos
            if self.page.locator(self.SEL_MENU_CONSULTA).count() == 0:
                return False
            return True
        except Exception:
            return False

    def _resolver_2fa(self, totp_secret: str, max_retries: int = 2) -> None:
        """Submete o código TOTP. Faz retry automático se o eproc reportar que
        'O código X já foi utilizado' — espera a próxima janela TOTP (30s).

        Também aguarda uma janela TOTP fresca se a atual está pra acabar
        (< 5s restantes), pra evitar que o código flipe entre gerar e submeter
        (o Keycloak do TJRS é estrito sobre isso).
        """
        assert self.page is not None

        for tentativa in range(max_retries + 1):
            restante = 30 - (time.time() % 30)
            if restante < 5:
                self.log.info(
                    "2FA: janela TOTP termina em %.1fs — esperando próxima janela",
                    restante,
                )
                time.sleep(restante + 0.5)
            self.log.info("2FA: gerando código TOTP (tentativa %d/%d)", tentativa + 1, max_retries + 1)
            try:
                codigo = gerar_codigo(totp_secret)
            except InvalidTOTPSecret as e:
                raise EprocLoginError(f"TOTP secret do .env inválido: {e}") from e

            self.page.fill(self.SEL_2FA_CODIGO, codigo)
            self.page.click(self.SEL_2FA_BTN)

            estado, msg = self._aguardar_resultado_2fa(timeout_s=20)
            if estado == "painel":
                self.log.info("painel detectado — login com 2FA concluído")
                return
            if estado == "reutilizado":
                if tentativa < max_retries:
                    self._aguardar_proxima_janela_totp()
                    continue
                raise EprocLoginError(
                    f"Código TOTP marcado como reutilizado após {max_retries + 1} tentativas: {msg}"
                )
            if estado == "rejeitado":
                raise EprocLoginError(f"eproc rejeitou o código TOTP: {msg}")
            # timeout
            self._dump_screenshot("timeout_2fa")
            raise EprocLoginError(
                f"Painel não detectado em 20s pós-2FA. "
                f"url={self.page.url!r} title={self.page.title()!r}"
            )

    def _aguardar_resultado_2fa(self, timeout_s: int) -> tuple[str, str | None]:
        """Aguarda o desfecho do submit de 2FA.

        Retorna uma tupla `(estado, mensagem)` onde estado é um de:
          - 'painel'      → autenticado com sucesso (passou do 2FA)
          - 'reutilizado' → erro que deve disparar retry com nova janela TOTP
          - 'rejeitado'   → erro definitivo (não tenta retry)
          - 'timeout'     → nenhum sinal em `timeout_s` segundos

        Usa `_apos_2fa_ok()` em vez de `_painel_carregado()` diretamente. Permite
        que subclasses (RS, etc.) aceitem telas intermediárias como "Seleção de
        perfil" como sinal de sucesso, e tratem a navegação extra em `_finalizar_login()`.
        """
        assert self.page is not None
        fim = time.time() + timeout_s
        while time.time() < fim:
            msg = self._mensagem_erro_2fa()
            if msg:
                return (self._classificar_erro_2fa(msg), msg)
            if self._apos_2fa_ok():
                return ("painel", None)
            self.page.wait_for_timeout(500)
        return ("timeout", None)

    def _classificar_erro_2fa(self, msg: str) -> str:
        """Decide se uma mensagem de erro do 2FA é 'reutilizado' (retry com nova
        janela TOTP) ou 'rejeitado' (sem retry).

        Default do MG: só 'já foi utilizado' pede retry; resto é rejeição definitiva
        (provavelmente secret errado, conta bloqueada, etc.). Subclasses sobrescrevem
        pra tribunais com mensagens diferentes (RS via Keycloak, por exemplo).
        """
        low = msg.lower()
        if "já foi utilizado" in low or "ja foi utilizado" in low:
            return "reutilizado"
        return "rejeitado"

    def _apos_2fa_ok(self) -> bool:
        """Default: passou do 2FA == painel carregado. Subclasses podem
        aceitar telas intermediárias (ex.: 'Seleção de perfil' no RS) aqui."""
        return self._painel_carregado()

    def _finalizar_login(self) -> None:
        """Hook chamado depois do 2FA confirmado, antes de persistir cookies.
        Default: no-op. Subclasses lidam aqui com telas extras (seleção de
        perfil, escolha de lotação, etc.).
        """
        return None

    def _mensagem_erro_2fa(self) -> str | None:
        """Captura o texto de qualquer mensagem de erro/aviso visível na tela de 2FA."""
        assert self.page is not None
        for pat in ("Erro verificando", "já foi utilizado", "ja foi utilizado", "código inválido"):
            try:
                loc = self.page.get_by_text(re.compile(pat, re.I))
                if loc.count() > 0 and loc.first.is_visible():
                    return loc.first.inner_text().strip()
            except Exception:
                continue
        return None

    def _aguardar_proxima_janela_totp(self) -> None:
        """TOTP renova a cada 30s alinhado ao relógio. Espera o próximo período + 1s margem."""
        espera = 30 - (time.time() % 30) + 1
        self.log.info("código TOTP reutilizado — aguardando %.1fs para próxima janela", espera)
        time.sleep(espera)
        # limpa o campo pra próximo digitar
        try:
            self.page.fill(self.SEL_2FA_CODIGO, "")
        except Exception:
            pass

    def _dump_screenshot(self, prefix: str) -> None:
        try:
            shot = self.settings.logs_dir / f"{prefix}_{int(time.time())}.png"
            self.page.screenshot(path=str(shot), full_page=True)
            self.log.warning("screenshot salvo em %s", shot)
        except Exception:
            pass

    # ==========================================================================
    # CONSULTA PROCESSUAL
    # ==========================================================================
    def consultar_processo(self, numero: str) -> ConsultaProcessoResultado:
        """Vai em Consulta Processual > Consultar Processos, busca o número CNJ
        e devolve um resultado tipado (encontrado/erro/url/título).
        """
        if self.page is None:
            raise RuntimeError("Adapter não iniciado.")

        numero_limpo = re.sub(r"\D", "", numero)
        if len(numero_limpo) != 20:
            raise ValueError(
                f"Número CNJ precisa de 20 dígitos; recebido {len(numero_limpo)} ({numero!r})."
            )

        self.log.info("consultando processo %s", numero_limpo)
        self._abrir_consulta_processual()
        self.page.fill(self.SEL_NUM_PROCESSO, numero_limpo)
        self.page.click(self.SEL_BTN_CONSULTAR)

        return self._aguardar_resultado_consulta(numero_limpo)

    def _abrir_consulta_processual(self) -> None:
        """Navega via menu lateral até a tela de busca. Idempotente."""
        assert self.page is not None

        # Já estamos na tela?
        campo = self.page.locator(self.SEL_NUM_PROCESSO)
        if campo.count() > 0 and campo.first.is_visible():
            self.log.debug("já na tela de consulta processual")
            return

        self.log.debug("abrindo menu > Consulta Processual > Consultar Processos")
        self.page.click(self.SEL_MENU_CONSULTA)
        # subitem aparece logo após a expansão do submenu
        sub = self.page.get_by_role("link", name="Consultar Processos")
        sub.first.click()
        self.page.wait_for_selector(self.SEL_NUM_PROCESSO, state="visible", timeout=15_000)

    def _aguardar_resultado_consulta(
        self, numero: str, timeout_s: int = 20
    ) -> ConsultaProcessoResultado:
        """Após o submit: ou navega pra página do processo, ou mostra erro inline."""
        assert self.page is not None
        fim = time.time() + timeout_s
        while time.time() < fim:
            # Página inteira de erro do eproc (regra de negócio etc.) — falha rápido.
            self._abortar_se_erro_eproc("consultar_processo", EprocConsultaError)

            erro = self._mensagem_erro_consulta()
            if erro:
                self.log.info("processo %s não encontrado: %s", numero, erro)
                return ConsultaProcessoResultado(numero=numero, encontrado=False, erro=erro)

            if self._processo_carregado(numero):
                titulo = self.page.title() or ""
                # primeiro segmento antes de '::' costuma ser o CNJ formatado
                m = re.match(r"\s*([\d\-.]+)\s*::", titulo)
                numero_fmt = m.group(1) if m else None
                self.log.info("processo %s encontrado (%s)", numero, numero_fmt or titulo)
                return ConsultaProcessoResultado(
                    numero=numero,
                    encontrado=True,
                    numero_formatado=numero_fmt,
                    url=self.page.url,
                    titulo=titulo,
                )
            self.page.wait_for_timeout(500)

        # Timeout: salva screenshot
        try:
            shot = self.settings.logs_dir / f"timeout_consulta_{int(time.time())}.png"
            self.page.screenshot(path=str(shot), full_page=True)
            self.log.warning("screenshot do timeout salvo em %s", shot)
        except Exception:
            pass
        raise EprocConsultaError(
            f"Sem resultado/erro em {timeout_s}s. url={self.page.url!r} title={self.page.title()!r}"
        )

    def _processo_carregado(self, numero: str) -> bool:
        """Página de detalhe do processo carregada.

        Sinais confirmados no eproc-MG (ambos presentes na rota /processo_selecionar):
        - URL contém exatamente `acao=processo_selecionar`
        - <title> começa com o número CNJ formatado (ex.: '1003897-27.2026.8.13.0145 ::')
        """
        assert self.page is not None
        try:
            if "acao=processo_selecionar" in self.page.url.lower():
                return True
            title = self.page.title() or ""
            # comparar só dígitos para tolerar formatação CNJ
            if numero in re.sub(r"\D", "", title.split("::", 1)[0]):
                return True
        except Exception:
            pass
        return False

    def _mensagem_erro_consulta(self) -> str | None:
        """Detecta mensagem de erro / 'não encontrado' inline."""
        assert self.page is not None
        for pat in ("não encontrado", "nenhum registro", "inexistente", "inválido"):
            try:
                loc = self.page.get_by_text(re.compile(pat, re.I))
                if loc.count() > 0 and loc.first.is_visible():
                    return loc.first.inner_text().strip()
            except Exception:
                continue
        return None

    # ==========================================================================
    # MOVIMENTAR / PETICIONAR
    # ==========================================================================
    def movimentar_peticionar(
        self,
        evento: str = "PETIÇÃO",
        desmarcar_prazos: bool = True,
    ) -> dict[str, Any]:
        """A partir da página do processo, abre 'Movimentar/Peticionar' e seleciona
        um evento no autocomplete por correspondência exata de texto.

        `evento` é o rótulo visível na lista — para garantir que a opção genérica
        'PETIÇÃO' seja escolhida (e não 'PETIÇÃO - ADITAMENTO À DENÚNCIA', etc.).

        Se `desmarcar_prazos=True` (default) e o fieldset de prazos abertos aparecer
        no form, todos os checkboxes vêm desmarcados para não fechar prazos sem querer.
        """
        if self.page is None:
            raise RuntimeError("Adapter não iniciado.")

        self.log.info("clicando em Movimentar/Peticionar")
        self.page.click(self.SEL_BTN_MOVIMENTAR)
        try:
            self.page.wait_for_selector(self.SEL_EVENTO_INPUT, state="visible", timeout=15_000)
        except Exception:
            # Form não abriu — pode ter caído em página de erro do eproc (ex.: classe
            # do processo não permite peticionar, processo baixado/extinto, etc.)
            self._abortar_se_erro_eproc("movimentar_peticionar")
            raise  # se não era erro reconhecível, propaga o timeout original

        valor = self._tentar_selecionar_evento(evento)

        # Após selecionar o evento o eproc dispara AJAX que adiciona ao DOM:
        # aviso 'Atenção!', fieldset de prazos (#fldPrazo, se houver) e a seção de
        # documentos. Precisamos esperar isso completar antes de manipular prazos.
        self._aguardar_form_movimentacao_estabilizar()

        prazos = self._desmarcar_prazos() if desmarcar_prazos else 0
        return {"evento": valor, "url": self.page.url, "prazos_desmarcados": prazos}

    def _tentar_selecionar_evento(self, evento: str, max_tentativas: int = 3) -> str:
        """Digita o evento no autocomplete e seleciona a opção exata. Faz retry
        interno até `max_tentativas` se o campo ficar vazio (flake conhecido
        do autocomplete do MG, onde uma das 3 estratégias de seleção não pega).

        Retorna o valor confirmado no input. Levanta EprocConsultaError se nem
        após as tentativas o valor ficar correto.
        """
        assert self.page is not None
        ultimo_valor = ""
        for tentativa in range(1, max_tentativas + 1):
            self.log.info("digitando evento '%s' no autocomplete (tentativa %d/%d)",
                          evento, tentativa, max_tentativas)
            self.page.click(self.SEL_EVENTO_INPUT)
            self.page.locator(self.SEL_EVENTO_INPUT).fill("")
            # keyboard.type dispara keydown/keyup → triggers do AJAX do autocomplete
            self.page.keyboard.type(evento, delay=40)

            self._selecionar_opcao_autocomplete(evento)

            # Tab força blur → eproc dispara AJAX que carrega seções dependentes
            self.page.keyboard.press("Tab")

            ultimo_valor = self.page.locator(self.SEL_EVENTO_INPUT).input_value()
            if ultimo_valor.strip().upper() == evento.upper():
                self.log.info("evento confirmado no campo: %r", ultimo_valor)
                return ultimo_valor

            self.log.warning(
                "tentativa %d: campo evento ficou com %r — limpando e tentando de novo",
                tentativa, ultimo_valor,
            )
            self.page.wait_for_timeout(600)

        raise EprocConsultaError(
            f"Após {max_tentativas} tentativas, o campo evento ficou com "
            f"{ultimo_valor!r} (esperava {evento!r})."
        )

    def _aguardar_form_movimentacao_estabilizar(self, timeout_s: int = 10) -> None:
        """Espera o form de Movimentação Processual terminar de hidratar via AJAX.

        Importante: o AJAX que carrega prazos/documentos pode demorar uns ms
        pra iniciar após o blur do campo evento. Sem um delay inicial,
        `wait_for_load_state('networkidle')` pode retornar imediatamente
        (já estava idle), antes do AJAX começar.
        """
        assert self.page is not None
        # delay inicial pra dar tempo do AJAX pós-blur disparar
        self.page.wait_for_timeout(800)
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout_s * 1000)
        except Exception as e:
            self.log.debug("networkidle não atingido em %ds (%s)", timeout_s, e)
        try:
            self.page.get_by_text("Adicionar mais Documentos", exact=False).first.wait_for(
                state="visible", timeout=timeout_s * 1000
            )
            self.log.debug("form de movimentação estabilizou")
        except Exception:
            self.log.warning(
                "'Adicionar mais Documentos' não apareceu em %ds — form pode estar incompleto",
                timeout_s,
            )

    def _desmarcar_prazos(self) -> int:
        """Se o fieldset #fldPrazo ('Selecione o(s) prazo(s) a ser(em) fechado(s)')
        existir, desmarca todos os checkboxes que vierem marcados.

        Retorna a quantidade de checkboxes desmarcados (0 quando o fieldset
        não aparece — caso comum: só existe quando há prazo aberto para o advogado).
        """
        assert self.page is not None
        fld = self.page.locator(self.SEL_FLD_PRAZO)
        if fld.count() == 0:
            # fallback por texto (caso uma versão do eproc mude o id)
            por_texto = self.page.get_by_text("Selecione o(s) prazo", exact=False)
            if por_texto.count() == 0:
                self.log.info("prazos: nenhum fieldset detectado (processo sem prazo aberto)")
                return 0
        if fld.count() > 0 and not fld.first.is_visible():
            self.log.info("prazos: fieldset presente mas oculto — ignorando")
            return 0

        boxes = self.page.locator(self.SEL_CHK_PRAZO)
        total = boxes.count()
        if total == 0:
            self.log.info("prazos: fieldset detectado mas sem checkboxes selPrazo[]")
            return 0

        desmarcados = 0
        for i in range(total):
            cb = boxes.nth(i)
            if cb.is_checked():
                cb.uncheck()
                desmarcados += 1
        self.log.info("prazos: %d/%d checkbox(es) desmarcado(s)", desmarcados, total)
        return desmarcados

    # ==========================================================================
    # ANEXAR DOCUMENTO + CONFIRMAR
    # ==========================================================================
    def anexar_documento(
        self,
        arquivo: str | Path,
        tipo: str,
        documento: int = 1,
    ) -> dict[str, Any]:
        """Anexa um PDF ao 'Documento N' do form de movimentação e preenche o
        autocomplete de Tipo. Não toca em Sigilo (mantém o default 'Sem Sigilo').

        **Idempotente:** se o arquivo (por nome) já estiver na tabela 'Documentos
        selecionados e ainda não utilizados em movimentação' (resíduo de execução
        anterior que confirmou mas não peticionou), pula upload+tipo+confirmar e
        retorna `{"reutilizado": True, ...}` para o caller saber que não deve
        chamar `confirmar_documentos()` de novo.

        Aguarda o upload (fineuploader/qq-uploader) terminar — sinalizado pela
        classe `qq-upload-success` no <li> do arquivo ou pelo hidden `fleArquivo_N`
        receber valor.
        """
        if self.page is None:
            raise RuntimeError("Adapter não iniciado.")

        path = Path(arquivo).expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Arquivo não encontrado: {path}")

        # Idempotência: eproc preserva docs confirmados; não duplica.
        ja_existe = self._contar_doc_na_tabela_selecionados(path.name)
        if ja_existe > 0:
            self.log.warning(
                "documento '%s' já está na tabela 'Documentos selecionados' (%dx) — "
                "pulando upload/tipo/confirmar para não duplicar",
                path.name,
                ja_existe,
            )
            return {
                "arquivo": str(path),
                "tipo_texto": "(reutilizado de execução anterior)",
                "tipo_id": "(n/a)",
                "documento": documento,
                "reutilizado": True,
            }

        sel_input = self.SEL_DOC_FILE_INPUT_TPL.format(n=documento)
        sel_list = self.SEL_DOC_UPLOAD_LIST_TPL.format(n=documento)
        sel_hidden = self.SEL_DOC_FLE_HIDDEN_TPL.format(n=documento)
        sel_tipo = self.SEL_DOC_TIPO_INPUT_TPL.format(n=documento)
        sel_tipo_hidden = self.SEL_DOC_TIPO_HIDDEN_TPL.format(n=documento)

        self.log.info("anexando arquivo '%s' ao Documento %d", path.name, documento)
        self.page.locator(sel_input).set_input_files(str(path))
        self._aguardar_upload_completar(documento, sel_list, sel_hidden)

        self.log.info("digitando tipo '%s' no autocomplete do Documento %d", tipo, documento)
        self.page.click(sel_tipo)
        self.page.locator(sel_tipo).fill("")
        self.page.keyboard.type(tipo, delay=40)
        self._selecionar_opcao_autocomplete(tipo)
        # blur pra disparar onChange do tipo (mesma lógica do evento)
        self.page.keyboard.press("Tab")

        valor_tipo = self.page.locator(sel_tipo).input_value()
        tipo_id = self.page.locator(sel_tipo_hidden).input_value()
        if not tipo_id:
            raise EprocPeticionarError(
                f"Tipo '{tipo}' não foi efetivamente selecionado "
                f"(hidden {sel_tipo_hidden} vazio). Texto no campo: {valor_tipo!r}."
            )
        self.log.info("tipo selecionado: texto=%r id=%r", valor_tipo, tipo_id)

        return {
            "arquivo": str(path),
            "tipo_texto": valor_tipo,
            "tipo_id": tipo_id,
            "documento": documento,
            "reutilizado": False,
        }

    def _contar_doc_na_tabela_selecionados(self, nome_arquivo: str) -> int:
        """Conta linhas na tabela 'Documentos selecionados ...' que mencionam o nome."""
        assert self.page is not None
        try:
            return self.page.locator(f'tr:has-text("{nome_arquivo}")').count()
        except Exception:
            return 0

    def _mensagem_erro_movimentacao(self) -> str | None:
        """Captura mensagem de erro/aviso visível na tela de Movimentação Processual."""
        if not self.page:
            return None
        for sel in [".infraMensagemErro", ".infraMensagemAviso", "[role=alert]"]:
            try:
                loc = self.page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    txt = loc.first.inner_text().strip()
                    if txt:
                        return txt
            except Exception:
                continue
        return None

    def _abortar_se_erro_eproc(self, contexto: str, exc_cls: type = None) -> None:
        """Verifica se o eproc retornou página de erro e levanta com a mensagem
        real do tribunal. `exc_cls` permite usar a exception apropriada à fase
        (`EprocConsultaError`, `EprocPeticionarError`, etc.).
        """
        err = self._detectar_erro_eproc()
        if not err:
            return
        self._dump_screenshot(f"erro_eproc_{contexto}")
        if exc_cls is None:
            exc_cls = EprocPeticionarError
        raise exc_cls(f"eproc bloqueou em '{contexto}': {err}")

    def _detectar_erro_eproc(self) -> str | None:
        """Cobre dois formatos de erro do eproc:
          1. Página inteira de Erro (heading 'Erro' + parágrafos vermelhos).
             Ex.: 'Não é possível peticionar em processos da classe CARTA PRECATÓRIA...'
          2. Mensagem inline em form (`.infraMensagemErro`).
        """
        if not self.page:
            return None
        # Caso 1: página de erro full-screen
        try:
            heading = self.page.get_by_role(
                "heading", name=re.compile(r"^\s*Erro\s*$", re.IGNORECASE)
            )
            if heading.count() > 0 and heading.first.is_visible():
                body_txt = self.page.locator("body").inner_text()
                linhas = [l.strip() for l in body_txt.split("\n") if l.strip()]
                try:
                    idx = next(i for i, l in enumerate(linhas) if l.lower() == "erro")
                except StopIteration:
                    idx = -1
                if idx >= 0:
                    msg: list[str] = []
                    for l in linhas[idx + 1: idx + 6]:
                        if l.lower() in {"voltar", "ajuda"}:
                            break
                        msg.append(l)
                    if msg:
                        return " ".join(msg)
        except Exception:
            pass
        # Caso 2: inline
        return self._mensagem_erro_movimentacao()

    def _aguardar_redirect_processo(self, timeout_s: int = 10) -> bool:
        """True se a URL mudou pra `acao=processo_selecionar` em até `timeout_s`."""
        assert self.page is not None
        try:
            self.page.wait_for_url(
                re.compile(r"acao=processo_selecionar", re.I),
                timeout=timeout_s * 1000,
            )
            return True
        except Exception:
            return False

    def _aguardar_upload_completar(
        self,
        documento: int,
        sel_list: str,
        sel_hidden: str,
        timeout_s: int = 60,
    ) -> None:
        """O qq-uploader marca o item com `.qq-upload-success` quando o upload
        termina (e o servidor responde com o id, que vai pro hidden fleArquivo_N).
        Espera até qualquer uma das condições.
        """
        assert self.page is not None
        # primeiro: <li> aparece (upload iniciado)
        item = self.page.locator(f"{sel_list} li").first
        try:
            item.wait_for(state="visible", timeout=10_000)
        except Exception as e:
            raise EprocPeticionarError(
                f"Upload do Documento {documento} não iniciou em 10s ({e})"
            ) from e

        # depois: classe success no <li> OU hidden recebe valor
        fim = time.time() + timeout_s
        while time.time() < fim:
            try:
                cls = item.get_attribute("class") or ""
                if "qq-upload-success" in cls:
                    self.log.info("upload do Documento %d concluído (success)", documento)
                    return
                if "qq-upload-fail" in cls:
                    raise EprocPeticionarError(
                        f"Upload do Documento {documento} falhou (qq-upload-fail)"
                    )
                val = self.page.locator(sel_hidden).input_value()
                if val:
                    self.log.info("upload do Documento %d concluído (hidden=%s)", documento, val)
                    return
            except EprocPeticionarError:
                raise
            except Exception:
                pass
            self.page.wait_for_timeout(500)

        raise EprocPeticionarError(
            f"Upload do Documento {documento} não terminou em {timeout_s}s"
        )

    def adicionar_mais_documentos(self, novo_n: int) -> None:
        """Clica em 'Adicionar mais Documentos' e espera o fieldset
        `#fldInfDocumento{novo_n}` aparecer no DOM.
        """
        if self.page is None:
            raise RuntimeError("Adapter não iniciado.")
        self.log.info("'+Adicionar mais Documentos' → preparando Documento %d", novo_n)
        self.page.click(self.SEL_BTN_ADICIONAR_DOC)
        sel = self.SEL_DOC_FIELDSET_TPL.format(n=novo_n)
        self.page.locator(sel).wait_for(state="visible", timeout=10_000)

    def confirmar_documentos(
        self,
        aguardar_nomes: list[str] | None = None,
        timeout_evidencia_s: int = 15,
    ) -> dict[str, Any]:
        """Clica em 'Confirmar seleção de documentos' (#btnEnviarArquivos).

        O AJAX que popula a tabela 'Documentos selecionados' é assíncrono.
        Quando `aguardar_nomes` é passado, esperamos cada nome aparecer em uma
        linha (`<tr>`) da página — é o sinal forte de sucesso. Sem isso,
        cai num `wait_for_load_state("networkidle")` que costuma retornar
        imediato (mesmo bug que vimos antes em outros forms do eproc).
        """
        if self.page is None:
            raise RuntimeError("Adapter não iniciado.")
        self.log.info("clicando em 'Confirmar seleção de documentos'")
        self.page.click(self.SEL_BTN_CONFIRMAR_DOCS)
        self.page.wait_for_timeout(1200)
        try:
            self.page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass

        if aguardar_nomes:
            # Antes de esperar evidência por nome, confere se a tela não virou erro
            # (ex.: validação do servidor rejeitou os docs por algum motivo).
            self._abortar_se_erro_eproc("confirmar_documentos")
            for nome in aguardar_nomes:
                try:
                    self.page.locator(f'tr:has-text("{nome}")').first.wait_for(
                        state="visible", timeout=timeout_evidencia_s * 1000
                    )
                    self.log.debug("doc '%s' apareceu na tabela", nome)
                except Exception as e:
                    # Antes de só warn, re-verifica erro (pode ter aparecido no meio do wait)
                    self._abortar_se_erro_eproc("confirmar_documentos")
                    self.log.warning(
                        "doc '%s' não apareceu na tabela em %ds (%s)",
                        nome, timeout_evidencia_s, e,
                    )
        return {"url": self.page.url, "title": self.page.title()}

    def peticionar_e_capturar_recibo(
        self,
        numero: str,
        pasta_destino: Path,
        nome_arquivo_anexo: str | None = None,
        nome_recibo: str | None = None,
    ) -> Path:
        """Aciona o botão Peticionar (#sbmMovimentar) e salva um screenshot
        full-page do estado pós-protocolo como PDF.

        O botão tem `data-disabled="true"` no HTML inicial — é um flag custom
        do eproc, não a propriedade HTML `disabled`. O que realmente importa é
        o documento ter sido aceito no `Documentos selecionados ...`.
        Confirmamos isso (sinal real de sucesso do confirmar_documentos) antes
        de disparar o click.
        """
        if self.page is None:
            raise RuntimeError("Adapter não iniciado.")

        # 1) garante que o documento foi de fato adicionado à tabela
        if nome_arquivo_anexo:
            self.log.info("aguardando documento aparecer na tabela 'Documentos selecionados'")
            try:
                self.page.locator(f'tr:has-text("{nome_arquivo_anexo}")').first.wait_for(
                    state="visible", timeout=15_000
                )
            except Exception as e:
                self._dump_screenshot("peticionar_doc_nao_listado")
                raise EprocPeticionarError(
                    f"Documento '{nome_arquivo_anexo}' não apareceu na tabela 'Documentos "
                    f"selecionados' após confirmar — o eproc pode ter rejeitado. ({e})"
                ) from e

        # 2) Estratégias múltiplas para acionar Peticionar:
        #    a) scroll + hover + click force=True
        #    b) chamada direta à função JS validarMovimentacaoESubmit()
        #    c) limpar data-disabled e clicar normal
        # Em cada uma checamos se a página navegou (URL muda pra processo_selecionar).
        btn = self.page.locator(self.SEL_BTN_PETICIONAR).first
        try:
            btn.scroll_into_view_if_needed()
            btn.hover()
        except Exception:
            pass
        self.page.wait_for_timeout(400)

        navegou = False

        self.log.info("estratégia (a): click force=True em #sbmMovimentar")
        try:
            btn.click(force=True)
        except Exception as e:
            self.log.warning("click(force=True) falhou: %s", e)
        navegou = self._aguardar_redirect_processo(timeout_s=8)

        if not navegou:
            self.log.info("estratégia (b): page.evaluate('validarMovimentacaoESubmit()')")
            try:
                self.page.evaluate(
                    "if (typeof validarMovimentacaoESubmit === 'function') validarMovimentacaoESubmit();"
                )
            except Exception as e:
                self.log.warning("evaluate falhou: %s", e)
            navegou = self._aguardar_redirect_processo(timeout_s=8)

        if not navegou:
            self.log.info("estratégia (c): limpa data-disabled e clica normal")
            try:
                self.page.evaluate(
                    """document.querySelectorAll('button[accesskey="t"][value="Movimentar"]')
                       .forEach(b => b.setAttribute('data-disabled', 'false'));"""
                )
                btn.click()
            except Exception as e:
                self.log.warning("limpar/clicar falhou: %s", e)
            navegou = self._aguardar_redirect_processo(timeout_s=10)

        if not navegou:
            # Pode ter virado uma página inteira de erro do eproc (ex.: regra
            # de negócio bloqueando — 'classe CARTA PRECATÓRIA já baixada').
            err = self._detectar_erro_eproc()
            self._dump_screenshot("peticionar_sem_navegacao")
            if err:
                raise EprocPeticionarError(f"eproc bloqueou: {err}")
            raise EprocPeticionarError(
                "Peticionar acionado mas a página não navegou e nenhuma mensagem "
                "de erro foi reconhecida. Screenshot salvo pra inspeção manual."
            )

        try:
            self.page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass

        # 4) Captura recibo
        pasta_destino = Path(pasta_destino).expanduser().resolve()
        pasta_destino.mkdir(parents=True, exist_ok=True)
        nome = nome_recibo or f"recibo{numero}.pdf"
        recibo_pdf = pasta_destino / nome

        self.log.info("capturando screenshot do recibo em %s", recibo_pdf)
        png_bytes = self.page.screenshot(full_page=True)
        self._png_para_pdf(png_bytes, recibo_pdf)
        return recibo_pdf

    @staticmethod
    def _png_para_pdf(png_bytes: bytes, destino: Path) -> None:
        """Converte um PNG (bytes) num PDF de uma página com Pillow."""
        from io import BytesIO

        from PIL import Image  # import local: dependência opcional

        img = Image.open(BytesIO(png_bytes)).convert("RGB")
        img.save(destino, "PDF", resolution=100.0)

    def _selecionar_opcao_autocomplete(self, texto: str, timeout_ms: int = 8000) -> None:
        """Espera a opção exata aparecer no popup do autocomplete e clica nela.

        Tenta vários padrões pra cobrir as diferentes implementações de
        autocomplete dentro do eproc (algumas usam <li>, outras <a>/<div>).
        Cai pra ArrowDown+Enter em último caso.
        """
        assert self.page is not None
        per = max(1000, timeout_ms // 3)

        # 1) padrão acessível (role=option)
        try:
            opt = self.page.get_by_role("option", name=texto, exact=True)
            opt.first.wait_for(state="visible", timeout=per)
            opt.first.click()
            return
        except Exception:
            pass

        # 2) <li> com texto exato (case-insensitive). É o caso do txtEvento.
        try:
            li = self.page.locator("li").filter(
                has_text=re.compile(rf"^\s*{re.escape(texto)}\s*$", re.IGNORECASE)
            )
            li.first.wait_for(state="visible", timeout=per)
            li.first.click()
            return
        except Exception:
            pass

        # 3) qualquer elemento com texto exato visível, excluindo <input>/<label>.
        # É o caso do txtTipo_N (dropdown não é <li>).
        try:
            cands = self.page.get_by_text(texto, exact=True)
            n = cands.count()
            for i in range(n):
                el = cands.nth(i)
                if not el.is_visible():
                    continue
                tag = (el.evaluate("e => e.tagName") or "").lower()
                if tag in {"input", "label", "legend"}:
                    continue  # pular o próprio campo / rótulo
                el.click()
                return
        except Exception:
            pass

        # 4) último recurso: teclado
        self.log.warning("opção '%s' não encontrada por seletor — usando ArrowDown+Enter", texto)
        self.page.keyboard.press("ArrowDown")
        self.page.keyboard.press("Enter")
