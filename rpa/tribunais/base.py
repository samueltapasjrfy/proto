"""Contrato base para adaptadores de tribunais.

Cada tribunal (eproc_mg, eproc_rs, projudi, ...) deve subclassar `BaseAdapter`,
expor `LOGIN_URL` / `TRIBUNAL_ID` e implementar `login()`. Métodos das próximas
etapas (`buscar_processo`, `protocolar`, ...) entram como métodos adicionais
nas subclasses ou como mixins nesta camada.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from ..config import Settings
from ..logger import get as get_logger
from ..models import Cliente, CookieJar
from ..storage import CookieStore

CredentialSource = Any  # duck-typed: precisa expor get() e credenciais()


class BaseAdapter(ABC):
    TRIBUNAL_ID: str = ""
    LOGIN_URL: str = ""

    def __init__(
        self,
        cliente: Cliente,
        *,
        settings: Settings,
        cliente_store: CredentialSource,
        cookie_store: CookieStore,
        headless: bool | None = None,
    ):
        if not self.TRIBUNAL_ID or not self.LOGIN_URL:
            raise NotImplementedError("Subclasse deve definir TRIBUNAL_ID e LOGIN_URL.")
        self.cliente = cliente
        self.settings = settings
        self.cliente_store = cliente_store
        self.cookie_store = cookie_store
        self.headless = settings.headless if headless is None else headless
        self.log = get_logger(f"{self.TRIBUNAL_ID}.{cliente.id}")

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    # ---- ciclo de vida ----
    def start(self) -> None:
        self.log.debug("iniciando Playwright (headless=%s)", self.headless)
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self.context = self._browser.new_context()
        self.page = self.context.new_page()

    def stop(self) -> None:
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._playwright:
                self._playwright.stop()
            self._playwright = self._browser = self.context = self.page = None

    def __enter__(self) -> "BaseAdapter":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ---- helpers compartilhados ----
    def persistir_cookies(self) -> CookieJar:
        assert self.context is not None, "Adapter não iniciado — chame start() ou use 'with'."
        cookies = self.context.cookies()
        jar = CookieJar(cliente_id=self.cliente.id, tribunal=self.TRIBUNAL_ID, cookies=cookies)
        self.cookie_store.save(jar)
        self.log.info("cookies persistidos (%d)", len(cookies))
        return jar

    # ---- contrato ----
    @abstractmethod
    def login(self) -> dict[str, Any]:
        """Executa login e retorna metadados (ex.: {'estado': 'painel'|'2fa'|...})."""
        raise NotImplementedError
