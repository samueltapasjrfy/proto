"""Versão async da Fase 2 (eproc) — usada pra paralelizar via asyncio.

Espelha a lógica de `EprocMGAdapter` (sync) mas opera em `playwright.async_api`.
Login fica sync; isso aqui é o que roda dentro de cada "aba" (Page async)
quando vários itens são processados em paralelo dentro do mesmo BrowserContext.

Reusa constantes/seletores do adapter sync pra evitar drift.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from io import BytesIO
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from ...models import ConsultaProcessoResultado
from .adapter import EprocConsultaError, EprocMGAdapter, EprocPeticionarError


class EprocMGAsyncFlow:
    """Encapsula o pipeline async de Fase 2 pra uma única `Page`.

    Cada worker (= cada aba paralela) instancia um objeto destes e chama
    `processar_item` pra rodar o ciclo completo (consultar → movimentar →
    upload → confirmar → peticionar → recibo) no item designado.

    Constantes e seletores são herdados do `EprocMGAdapter` pra não duplicar.
    """

    # Constantes do adapter sync (compartilhadas, não duplicar)
    SEL_MENU_CONSULTA = EprocMGAdapter.SEL_MENU_CONSULTA
    SEL_NUM_PROCESSO = EprocMGAdapter.SEL_NUM_PROCESSO
    SEL_BTN_CONSULTAR = EprocMGAdapter.SEL_BTN_CONSULTAR
    SEL_BTN_MOVIMENTAR = EprocMGAdapter.SEL_BTN_MOVIMENTAR
    SEL_EVENTO_INPUT = EprocMGAdapter.SEL_EVENTO_INPUT
    SEL_FLD_PRAZO = EprocMGAdapter.SEL_FLD_PRAZO
    SEL_CHK_PRAZO = EprocMGAdapter.SEL_CHK_PRAZO
    SEL_DOC_FIELDSET_TPL = EprocMGAdapter.SEL_DOC_FIELDSET_TPL
    SEL_DOC_FILE_INPUT_TPL = EprocMGAdapter.SEL_DOC_FILE_INPUT_TPL
    SEL_DOC_UPLOAD_LIST_TPL = EprocMGAdapter.SEL_DOC_UPLOAD_LIST_TPL
    SEL_DOC_FLE_HIDDEN_TPL = EprocMGAdapter.SEL_DOC_FLE_HIDDEN_TPL
    SEL_DOC_TIPO_INPUT_TPL = EprocMGAdapter.SEL_DOC_TIPO_INPUT_TPL
    SEL_DOC_TIPO_HIDDEN_TPL = EprocMGAdapter.SEL_DOC_TIPO_HIDDEN_TPL
    SEL_BTN_ADICIONAR_DOC = EprocMGAdapter.SEL_BTN_ADICIONAR_DOC
    SEL_BTN_CONFIRMAR_DOCS = EprocMGAdapter.SEL_BTN_CONFIRMAR_DOCS
    SEL_BTN_PETICIONAR = EprocMGAdapter.SEL_BTN_PETICIONAR

    def __init__(self, page: Page, log: logging.Logger, logs_dir: Path, base_url: str):
        self.page = page
        self.log = log
        self.logs_dir = logs_dir
        self.base_url = base_url

    # ---- entrypoint ----
    async def processar_item(self, prep: dict, args) -> dict:
        """Pipeline completo pra 1 item. Retorna dict com chaves:
        `cod_item`, `cnj_digits`, `ok`, `recibo` (Path|None), `erro` (str|None).
        """
        cod = prep["cod_item"]
        cnj = prep["cnj_digits"]
        principal: Path = prep["principal"]
        anexos: list[Path] = prep["anexos"]

        try:
            # Cada worker abre uma Page nova em about:blank — precisa navegar
            # pra base do eproc pra "ativar" os cookies da sessão herdada do
            # login sync e cair no painel logado.
            self.log.info("CodItem=%s: navegando pra %s", cod, self.base_url)
            await self.page.goto(self.base_url, wait_until="domcontentloaded", timeout=30_000)

            cons = await self.consultar_processo(cnj)
            if not cons.encontrado:
                return {"cod_item": cod, "cnj_digits": cnj, "ok": False,
                        "recibo": None, "erro": f"consulta: {cons.erro}"}

            await self.movimentar_peticionar(evento=args.evento)

            # Idempotência: peça já presente?
            ja = await self._contar_doc_na_tabela(principal.name)
            if ja > 0:
                self.log.warning(
                    "CodItem=%s: peça principal já está na tabela (%d) — pulando uploads",
                    cod, ja,
                )
            else:
                await self.anexar_documento(arquivo=principal, tipo=args.tipo, documento=1)
                for i, anex in enumerate(anexos, start=2):
                    await self.adicionar_mais_documentos(novo_n=i)
                    await self.anexar_documento(arquivo=anex, tipo=args.tipo_anexo, documento=i)
                await self.confirmar_documentos(
                    aguardar_nomes=[principal.name] + [a.name for a in anexos],
                )

            recibo = None
            if args.peticionar:
                recibo = await self.peticionar_e_capturar_recibo(
                    numero=cnj,
                    pasta_destino=principal.parent,
                    nome_arquivo_anexo=principal.name,
                )

            return {"cod_item": cod, "cnj_digits": cnj, "ok": True,
                    "recibo": recibo, "erro": None}
        except Exception as e:
            self.log.exception("CodItem=%s: falhou — %s", cod, e)
            return {"cod_item": cod, "cnj_digits": cnj, "ok": False,
                    "recibo": None, "erro": f"{type(e).__name__}: {e}"}

    # ==========================================================================
    # CONSULTA PROCESSUAL
    # ==========================================================================
    async def consultar_processo(self, numero: str) -> ConsultaProcessoResultado:
        numero_limpo = re.sub(r"\D", "", numero)
        if len(numero_limpo) != 20:
            raise ValueError(f"CNJ precisa ter 20 dígitos; recebido {len(numero_limpo)}")
        self.log.info("consultando processo %s", numero_limpo)
        await self._abrir_consulta_processual()
        await self.page.fill(self.SEL_NUM_PROCESSO, numero_limpo)
        await self.page.click(self.SEL_BTN_CONSULTAR)
        return await self._aguardar_resultado_consulta(numero_limpo)

    async def _abrir_consulta_processual(self) -> None:
        # Sempre re-navega via menu — força o eproc a emitir um hash anti-CSRF
        # novo por aba/item. Sem isso, workers em paralelo no mesmo context
        # podem submeter com hash já consumido (SP devolve a tela vazia → timeout).
        await self.page.click(self.SEL_MENU_CONSULTA)
        sub = self.page.get_by_role("link", name="Consultar Processos")
        await sub.first.click()
        await self.page.wait_for_selector(self.SEL_NUM_PROCESSO, state="visible", timeout=15_000)

    async def _aguardar_resultado_consulta(
        self, numero: str, timeout_s: int = 20,
    ) -> ConsultaProcessoResultado:
        fim = time.time() + timeout_s
        while time.time() < fim:
            await self._abortar_se_erro_eproc("consultar_processo", EprocConsultaError)
            erro = await self._mensagem_erro_consulta()
            if erro:
                self.log.info("processo %s não encontrado: %s", numero, erro)
                return ConsultaProcessoResultado(numero=numero, encontrado=False, erro=erro)
            if await self._processo_carregado(numero):
                titulo = await self.page.title() or ""
                m = re.match(r"\s*([\d\-.]+)\s*::", titulo)
                numero_fmt = m.group(1) if m else None
                self.log.info("processo %s encontrado (%s)", numero, numero_fmt or titulo)
                return ConsultaProcessoResultado(
                    numero=numero, encontrado=True,
                    numero_formatado=numero_fmt,
                    url=self.page.url, titulo=titulo,
                )
            await self.page.wait_for_timeout(500)
        await self._dump_screenshot(f"timeout_consulta")
        raise EprocConsultaError(
            f"Sem resultado/erro em {timeout_s}s. url={self.page.url!r} title={await self.page.title()!r}"
        )

    async def _processo_carregado(self, numero: str) -> bool:
        try:
            if "acao=processo_selecionar" in self.page.url.lower():
                return True
            title = await self.page.title() or ""
            if numero in re.sub(r"\D", "", title.split("::", 1)[0]):
                return True
        except Exception:
            pass
        return False

    async def _mensagem_erro_consulta(self) -> str | None:
        for pat in ("não encontrado", "nenhum registro", "inexistente", "inválido"):
            try:
                loc = self.page.get_by_text(re.compile(pat, re.I))
                if await loc.count() > 0 and await loc.first.is_visible():
                    return (await loc.first.inner_text()).strip()
            except Exception:
                continue
        return None

    # ==========================================================================
    # MOVIMENTAR / PETICIONAR
    # ==========================================================================
    async def movimentar_peticionar(
        self, evento: str = "PETIÇÃO", desmarcar_prazos: bool = True,
    ) -> dict[str, Any]:
        self.log.info("clicando em Movimentar/Peticionar")
        await self.page.click(self.SEL_BTN_MOVIMENTAR)
        try:
            await self.page.wait_for_selector(self.SEL_EVENTO_INPUT, state="visible", timeout=15_000)
        except Exception:
            await self._abortar_se_erro_eproc("movimentar_peticionar")
            raise

        valor = await self._tentar_selecionar_evento(evento)
        await self._aguardar_form_movimentacao_estabilizar()
        prazos = await self._desmarcar_prazos() if desmarcar_prazos else 0
        return {"evento": valor, "url": self.page.url, "prazos_desmarcados": prazos}

    async def _tentar_selecionar_evento(self, evento: str, max_tentativas: int = 3) -> str:
        ultimo_valor = ""
        for tentativa in range(1, max_tentativas + 1):
            self.log.info("digitando evento %r (tentativa %d/%d)",
                          evento, tentativa, max_tentativas)
            await self.page.click(self.SEL_EVENTO_INPUT)
            await self.page.locator(self.SEL_EVENTO_INPUT).fill("")
            await self.page.keyboard.type(evento, delay=40)
            await self._selecionar_opcao_autocomplete(evento)
            await self.page.keyboard.press("Tab")
            ultimo_valor = await self.page.locator(self.SEL_EVENTO_INPUT).input_value()
            if ultimo_valor.strip().upper() == evento.upper():
                self.log.info("evento confirmado: %r", ultimo_valor)
                return ultimo_valor
            self.log.warning("tentativa %d: campo ficou %r — retry", tentativa, ultimo_valor)
            await self.page.wait_for_timeout(600)
        raise EprocConsultaError(
            f"Após {max_tentativas} tentativas, campo evento ficou {ultimo_valor!r}."
        )

    async def _selecionar_opcao_autocomplete(self, texto: str, timeout_ms: int = 8000) -> None:
        per = max(1000, timeout_ms // 3)
        # 1) role=option
        try:
            opt = self.page.get_by_role("option", name=texto, exact=True)
            await opt.first.wait_for(state="visible", timeout=per)
            await opt.first.click()
            return
        except Exception:
            pass
        # 2) li exato
        try:
            li = self.page.locator("li").filter(
                has_text=re.compile(rf"^\s*{re.escape(texto)}\s*$", re.IGNORECASE)
            )
            await li.first.wait_for(state="visible", timeout=per)
            await li.first.click()
            return
        except Exception:
            pass
        # 3) get_by_text exato (qualquer elemento), excluindo input/label/legend
        try:
            cands = self.page.get_by_text(texto, exact=True)
            n = await cands.count()
            for i in range(n):
                el = cands.nth(i)
                if not await el.is_visible():
                    continue
                tag = ((await el.evaluate("e => e.tagName")) or "").lower()
                if tag in {"input", "label", "legend"}:
                    continue
                await el.click()
                return
        except Exception:
            pass
        # 4) teclado
        self.log.warning("opção %r não encontrada — usando ArrowDown+Enter", texto)
        await self.page.keyboard.press("ArrowDown")
        await self.page.keyboard.press("Enter")

    async def _aguardar_form_movimentacao_estabilizar(self, timeout_s: int = 10) -> None:
        await self.page.wait_for_timeout(800)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout_s * 1000)
        except Exception:
            pass
        try:
            await self.page.get_by_text("Adicionar mais Documentos", exact=False).first.wait_for(
                state="visible", timeout=timeout_s * 1000
            )
        except Exception:
            self.log.warning("'Adicionar mais Documentos' não apareceu — form pode estar incompleto")

    async def _desmarcar_prazos(self) -> int:
        fld = self.page.locator(self.SEL_FLD_PRAZO)
        if await fld.count() == 0:
            por_texto = self.page.get_by_text("Selecione o(s) prazo", exact=False)
            if await por_texto.count() == 0:
                self.log.info("prazos: nenhum fieldset detectado")
                return 0
        if await fld.count() > 0 and not await fld.first.is_visible():
            self.log.info("prazos: fieldset oculto — ignorando")
            return 0
        boxes = self.page.locator(self.SEL_CHK_PRAZO)
        total = await boxes.count()
        if total == 0:
            return 0
        desmarcados = 0
        for i in range(total):
            cb = boxes.nth(i)
            if await cb.is_checked():
                await cb.uncheck()
                desmarcados += 1
        self.log.info("prazos: %d/%d desmarcado(s)", desmarcados, total)
        return desmarcados

    # ==========================================================================
    # ANEXAR DOCUMENTO
    # ==========================================================================
    async def anexar_documento(
        self, arquivo: Path, tipo: str, documento: int = 1,
    ) -> dict[str, Any]:
        path = Path(arquivo).expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Arquivo não encontrado: {path}")

        ja_existe = await self._contar_doc_na_tabela(path.name)
        if ja_existe > 0:
            self.log.warning("doc %r já na tabela (%dx) — skip", path.name, ja_existe)
            return {"arquivo": str(path), "reutilizado": True, "documento": documento}

        sel_input = self.SEL_DOC_FILE_INPUT_TPL.format(n=documento)
        sel_list = self.SEL_DOC_UPLOAD_LIST_TPL.format(n=documento)
        sel_hidden = self.SEL_DOC_FLE_HIDDEN_TPL.format(n=documento)
        sel_tipo = self.SEL_DOC_TIPO_INPUT_TPL.format(n=documento)
        sel_tipo_hidden = self.SEL_DOC_TIPO_HIDDEN_TPL.format(n=documento)

        self.log.info("anexando %r ao Doc %d", path.name, documento)
        await self.page.locator(sel_input).set_input_files(str(path))
        await self._aguardar_upload_completar(documento, sel_list, sel_hidden)

        self.log.info("digitando tipo %r no Doc %d", tipo, documento)
        await self.page.click(sel_tipo)
        await self.page.locator(sel_tipo).fill("")
        await self.page.keyboard.type(tipo, delay=40)
        await self._selecionar_opcao_autocomplete(tipo)
        await self.page.keyboard.press("Tab")

        valor_tipo = await self.page.locator(sel_tipo).input_value()
        tipo_id = await self.page.locator(sel_tipo_hidden).input_value()
        if not tipo_id:
            raise EprocPeticionarError(
                f"Tipo {tipo!r} não foi selecionado (hidden vazio). Texto: {valor_tipo!r}"
            )
        self.log.info("tipo: texto=%r id=%r", valor_tipo, tipo_id)
        return {"arquivo": str(path), "tipo_texto": valor_tipo, "tipo_id": tipo_id,
                "documento": documento, "reutilizado": False}

    async def _aguardar_upload_completar(
        self, documento: int, sel_list: str, sel_hidden: str, timeout_s: int = 60,
    ) -> None:
        item = self.page.locator(f"{sel_list} li").first
        try:
            await item.wait_for(state="visible", timeout=10_000)
        except Exception as e:
            raise EprocPeticionarError(f"Upload Doc {documento} não iniciou em 10s ({e})") from e
        fim = time.time() + timeout_s
        while time.time() < fim:
            try:
                cls = (await item.get_attribute("class")) or ""
                if "qq-upload-success" in cls:
                    self.log.info("upload Doc %d concluído (success)", documento)
                    return
                if "qq-upload-fail" in cls:
                    raise EprocPeticionarError(f"Upload Doc {documento} falhou (qq-upload-fail)")
                val = await self.page.locator(sel_hidden).input_value()
                if val:
                    self.log.info("upload Doc %d concluído (hidden=%s)", documento, val)
                    return
            except EprocPeticionarError:
                raise
            except Exception:
                pass
            await self.page.wait_for_timeout(500)
        raise EprocPeticionarError(f"Upload Doc {documento} não terminou em {timeout_s}s")

    async def adicionar_mais_documentos(self, novo_n: int) -> None:
        self.log.info("'+Adicionar mais Documentos' → Doc %d", novo_n)
        await self.page.click(self.SEL_BTN_ADICIONAR_DOC)
        sel = self.SEL_DOC_FIELDSET_TPL.format(n=novo_n)
        await self.page.locator(sel).wait_for(state="visible", timeout=10_000)

    # ==========================================================================
    # CONFIRMAR DOCUMENTOS
    # ==========================================================================
    async def confirmar_documentos(
        self, aguardar_nomes: list[str] | None = None, timeout_evidencia_s: int = 15,
    ) -> dict[str, Any]:
        self.log.info("clicando em 'Confirmar seleção de documentos'")
        await self.page.click(self.SEL_BTN_CONFIRMAR_DOCS)
        await self.page.wait_for_timeout(1200)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        if aguardar_nomes:
            await self._abortar_se_erro_eproc("confirmar_documentos")
            for nome in aguardar_nomes:
                try:
                    await self.page.locator(f'tr:has-text("{nome}")').first.wait_for(
                        state="visible", timeout=timeout_evidencia_s * 1000
                    )
                except Exception:
                    await self._abortar_se_erro_eproc("confirmar_documentos")
                    self.log.warning("doc %r não apareceu em %ds", nome, timeout_evidencia_s)
        return {"url": self.page.url, "title": await self.page.title()}

    async def _contar_doc_na_tabela(self, nome_arquivo: str) -> int:
        try:
            return await self.page.locator(f'tr:has-text("{nome_arquivo}")').count()
        except Exception:
            return 0

    # ==========================================================================
    # PETICIONAR + CAPTURAR RECIBO
    # ==========================================================================
    async def peticionar_e_capturar_recibo(
        self, numero: str, pasta_destino: Path, nome_arquivo_anexo: str | None = None,
    ) -> Path:
        if nome_arquivo_anexo:
            self.log.info("aguardando %r na tabela de selecionados", nome_arquivo_anexo)
            try:
                await self.page.locator(f'tr:has-text("{nome_arquivo_anexo}")').first.wait_for(
                    state="visible", timeout=15_000
                )
            except Exception as e:
                await self._dump_screenshot("peticionar_doc_nao_listado")
                raise EprocPeticionarError(
                    f"Doc {nome_arquivo_anexo!r} não listado após confirmar ({e})"
                ) from e

        btn = self.page.locator(self.SEL_BTN_PETICIONAR).first
        try:
            await btn.scroll_into_view_if_needed()
            await btn.hover()
        except Exception:
            pass
        await self.page.wait_for_timeout(400)

        navegou = False

        self.log.info("estratégia (a): click force=True")
        try:
            await btn.click(force=True)
        except Exception as e:
            self.log.warning("click force falhou: %s", e)
        navegou = await self._aguardar_redirect_processo(8)

        if not navegou:
            self.log.info("estratégia (b): evaluate validarMovimentacaoESubmit()")
            try:
                await self.page.evaluate(
                    "if (typeof validarMovimentacaoESubmit === 'function') validarMovimentacaoESubmit();"
                )
            except Exception as e:
                self.log.warning("evaluate falhou: %s", e)
            navegou = await self._aguardar_redirect_processo(8)

        if not navegou:
            self.log.info("estratégia (c): limpa data-disabled + click")
            try:
                await self.page.evaluate(
                    """document.querySelectorAll('button[accesskey="t"][value="Movimentar"]')
                       .forEach(b => b.setAttribute('data-disabled', 'false'));"""
                )
                await btn.click()
            except Exception as e:
                self.log.warning("limpar/clicar falhou: %s", e)
            navegou = await self._aguardar_redirect_processo(10)

        if not navegou:
            err = await self._detectar_erro_eproc()
            await self._dump_screenshot("peticionar_sem_navegacao")
            if err:
                raise EprocPeticionarError(f"eproc bloqueou: {err}")
            raise EprocPeticionarError(
                "Peticionar acionado mas página não navegou nem mostrou erro."
            )

        try:
            await self.page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass

        pasta_destino = Path(pasta_destino).expanduser().resolve()
        pasta_destino.mkdir(parents=True, exist_ok=True)
        recibo_pdf = pasta_destino / f"recibo{numero}.pdf"
        self.log.info("capturando recibo em %s", recibo_pdf)
        png_bytes = await self.page.screenshot(full_page=True)
        self._png_para_pdf(png_bytes, recibo_pdf)
        return recibo_pdf

    async def _aguardar_redirect_processo(self, timeout_s: int = 10) -> bool:
        try:
            await self.page.wait_for_url(
                re.compile(r"acao=processo_selecionar", re.I),
                timeout=timeout_s * 1000,
            )
            return True
        except Exception:
            return False

    @staticmethod
    def _png_para_pdf(png_bytes: bytes, destino: Path) -> None:
        from PIL import Image
        img = Image.open(BytesIO(png_bytes)).convert("RGB")
        img.save(destino, "PDF", resolution=100.0)

    # ==========================================================================
    # DETECÇÃO DE ERRO + UTILITIES
    # ==========================================================================
    async def _detectar_erro_eproc(self) -> str | None:
        # Página inteira de Erro (heading "Erro")
        try:
            heading = self.page.get_by_role(
                "heading", name=re.compile(r"^\s*Erro\s*$", re.IGNORECASE)
            )
            if await heading.count() > 0 and await heading.first.is_visible():
                body_txt = await self.page.locator("body").inner_text()
                linhas = [l.strip() for l in body_txt.split("\n") if l.strip()]
                try:
                    idx = next(i for i, l in enumerate(linhas) if l.lower() == "erro")
                except StopIteration:
                    idx = -1
                if idx >= 0:
                    msg = []
                    for l in linhas[idx + 1: idx + 6]:
                        if l.lower() in {"voltar", "ajuda"}:
                            break
                        msg.append(l)
                    if msg:
                        return " ".join(msg)
        except Exception:
            pass
        # Inline
        for sel in [".infraMensagemErro", ".infraMensagemAviso", "[role=alert]"]:
            try:
                loc = self.page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    txt = (await loc.first.inner_text()).strip()
                    if txt:
                        return txt
            except Exception:
                continue
        return None

    async def _abortar_se_erro_eproc(
        self, contexto: str, exc_cls: type = None,
    ) -> None:
        err = await self._detectar_erro_eproc()
        if not err:
            return
        await self._dump_screenshot(f"erro_eproc_{contexto}")
        if exc_cls is None:
            exc_cls = EprocPeticionarError
        raise exc_cls(f"eproc bloqueou em '{contexto}': {err}")

    async def _dump_screenshot(self, prefix: str) -> None:
        try:
            shot = self.logs_dir / f"{prefix}_{int(time.time())}.png"
            await self.page.screenshot(path=str(shot), full_page=True)
            self.log.warning("screenshot salvo em %s", shot)
        except Exception:
            pass
