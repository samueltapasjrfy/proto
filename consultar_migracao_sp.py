"""Descoberta de processos SP migrados pro eproc.

Pega os candidatos SP que passam nos filtros normais de status/documento
mas estão fora do filtro "óbvio" de migração (CNJ começa com 4 e ano >= 2025),
consulta cada um no eproc-SP e salva o resultado num JSON local.

Estratégia (mesma do `processar` pra SP no main.py):
- N logins sync seriais (com pausa pra próxima janela TOTP de 30s entre cada).
- N BrowserContexts isolados (cookies de cada login).
- N "lanes" async paralelas, cada uma consumindo de uma fila compartilhada
  de CNJs e fazendo `consultar_processo` via Playwright.
- Delay configurável entre consultas DENTRO de cada lane — espalha o ritmo
  pra evitar o rate-limit do captcha Cloudflare do eproc-SP que dispara em
  ~9 consultas/sessão numa janela curta.

Saídas:
- `data/migracao_sp.json` (cumulativo, idempotente)
- `data/migracao_sp_aptos.json` (só os encontrados + com arquivos)

Uso:
    python consultar_migracao_sp.py
    python consultar_migracao_sp.py --workers 4 --delay 30
    python consultar_migracao_sp.py --skip-ja-consultados
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from rpa.config import Settings
from rpa.db import listar_arquivos_do_item, listar_candidatos_sp_migracao
from rpa.logger import get as get_logger, setup as setup_logger
from rpa.storage import ENV_CLIENTE_ID, CookieStore, EnvClienteStore
from rpa.tribunais.eproc_sp import EprocSPAdapter, EprocSPAsyncFlow


DEFAULT_OUT = Path("data") / "migracao_sp.json"
DEFAULT_OUT_APTOS = Path("data") / "migracao_sp_aptos.json"
COOKIES_CACHE = Path("data") / "cookies" / "eproc_sp_sessoes.json"
COOKIES_TTL_HOURS = 6  # cookies eproc-SP típicos duram ~8h; corte conservador


def _default_desde(dias: int = 7) -> str:
    return (date.today() - timedelta(days=dias)).isoformat()


def _hoje() -> str:
    return date.today().isoformat()


def _carregar_existente(path: Path) -> dict[int, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {int(r["cod_item"]): r for r in data if "cod_item" in r}


def _salvar(path: Path, registros: dict[int, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(registros.values(), key=lambda r: r.get("cod_item", 0))
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _carregar_cookies_cache(n: int, log) -> list[list] | None:
    """Tenta carregar cookies salvos da execução anterior. Devolve a lista
    de sessões se válida (dentro do TTL e com pelo menos `n` sessões),
    senão None."""
    if not COOKIES_CACHE.exists():
        return None
    try:
        data = json.loads(COOKIES_CACHE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    ts = data.get("salvo_em")
    if not ts:
        return None
    try:
        salvo = datetime.fromisoformat(ts)
    except ValueError:
        return None
    horas = (datetime.now() - salvo).total_seconds() / 3600
    if horas > COOKIES_TTL_HOURS:
        log.info("cache de cookies expirou (%.1fh > %dh)", horas, COOKIES_TTL_HOURS)
        return None
    sessoes = data.get("sessoes", [])
    if len(sessoes) < n:
        log.info("cache tem %d sessões mas precisamos %d — refazendo login",
                 len(sessoes), n)
        return None
    log.info("cache de cookies válido (%.1fh, %d sessões) — pulando login",
             horas, len(sessoes))
    return sessoes[:n]


def _salvar_cookies_cache(cookies_por_sessao: list[list], log) -> None:
    try:
        COOKIES_CACHE.parent.mkdir(parents=True, exist_ok=True)
        COOKIES_CACHE.write_text(
            json.dumps({
                "salvo_em": datetime.now().isoformat(timespec="seconds"),
                "sessoes": cookies_por_sessao,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("cache de cookies salvo em %s (%d sessões)",
                 COOKIES_CACHE, len(cookies_por_sessao))
    except Exception as e:
        log.warning("falha salvando cache de cookies: %s", e)


def _invalidar_cookies_cache(log) -> None:
    if COOKIES_CACHE.exists():
        try:
            COOKIES_CACHE.unlink()
            log.info("cache de cookies invalidado")
        except Exception as e:
            log.warning("falha removendo cache: %s", e)


def _logins_seriais(cliente, settings, cliente_store, cookie_store, n: int, headless: bool, log) -> list[list]:
    """N logins serializados com TOTP timing. Devolve lista de cookies por sessão."""
    cookies_por_sessao: list[list] = []
    for sid in range(n):
        if sid > 0:
            # Próxima janela TOTP (eproc-SP rejeita o mesmo código consecutivo)
            espera = 30 - (time.time() % 30) + 1
            log.info(
                "aguardando %.1fs próxima janela TOTP antes da sessão %d",
                espera, sid + 1,
            )
            time.sleep(espera)
        try:
            with EprocSPAdapter(
                cliente,
                settings=settings,
                cliente_store=cliente_store,
                cookie_store=cookie_store,
                headless=headless,
            ) as login_adapter:
                login_adapter.login()
                cookies = login_adapter.context.cookies() if login_adapter.context else []
                cookies_por_sessao.append(cookies)
                log.info(
                    "login sessão %d/%d OK — %d cookies",
                    sid + 1, n, len(cookies),
                )
        except Exception as e:
            log.exception("login sessão %d falhou: %s", sid + 1, e)
    return cookies_por_sessao


async def _rodar_lanes_async(
    cookies_por_sessao,
    candidatos: list[dict],
    registros: dict[int, dict],
    out_path: Path,
    log,
    settings,
    base_url: str,
    delay_s: float,
    salvar_a_cada: int,
) -> dict:
    """Lanes async paralelas. Cada lane tem seu BrowserContext isolado e
    consume itens da mesma fila. Entre consultas faz `asyncio.sleep(delay_s)`.
    """
    from playwright.async_api import async_playwright

    metricas = {"encontrados": 0, "nao_encontrados": 0, "erros": 0}
    save_lock = asyncio.Lock()
    counter = {"n": 0}
    total = len(candidatos)

    headless = settings.headless

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        contexts = []
        for cookies in cookies_por_sessao:
            ctx = await browser.new_context()
            await ctx.add_cookies(cookies)
            contexts.append(ctx)

        fila: asyncio.Queue = asyncio.Queue()
        for cand in candidatos:
            fila.put_nowait(cand)

        log.info(
            "%d lanes paralelas, delay=%.1fs entre consultas, %d itens na fila",
            len(contexts), delay_s, total,
        )

        async def _processar_um(flow, cand: dict, lane_log) -> None:
            cod = cand["CodItem"]
            cnj = re.sub(r"\D", "", str(cand.get("NumProcessoCNJ") or ""))
            num_proc = cand.get("NumProcesso") or ""

            registro: dict = {
                "cod_item": int(cod),
                "id_proc": int(cand.get("IdProc") or 0),
                "num_processo": str(num_proc),
                "cnj": cnj,
                "consultado_em": time.strftime("%Y-%m-%d %H:%M:%S"),
                "encontrado": False,
                "numero_formatado": None,
                "url": None,
                "titulo": None,
                "erro": None,
                "tem_arquivos": False,
                "qtd_arquivos": 0,
            }
            try:
                arquivos = listar_arquivos_do_item(cod)
                registro["qtd_arquivos"] = len(arquivos)
                registro["tem_arquivos"] = bool(arquivos)
            except Exception as e:
                lane_log.warning("CodItem=%s: erro listando arquivos: %s", cod, e)

            counter["n"] += 1
            idx = counter["n"]

            if len(cnj) != 20:
                registro["erro"] = f"CNJ inválido ({len(cnj)} dígitos)"
                metricas["erros"] += 1
                lane_log.warning("[%d/%d] CodItem=%s: CNJ inválido", idx, total, cod)
            else:
                t0 = time.time()
                try:
                    res = await flow.consultar_processo(cnj)
                    dt_ms = int((time.time() - t0) * 1000)
                    registro["encontrado"] = bool(res.encontrado)
                    registro["numero_formatado"] = res.numero_formatado
                    registro["url"] = res.url
                    registro["titulo"] = res.titulo
                    registro["erro"] = res.erro
                    if res.encontrado:
                        metricas["encontrados"] += 1
                        lane_log.info(
                            "[%d/%d] CodItem=%s ENCONTRADO — %s (%dms)",
                            idx, total, cod,
                            res.numero_formatado or cnj, dt_ms,
                        )
                    else:
                        metricas["nao_encontrados"] += 1
                        lane_log.info(
                            "[%d/%d] CodItem=%s não encontrado (%dms)",
                            idx, total, cod, dt_ms,
                        )
                except Exception as e:
                    metricas["erros"] += 1
                    registro["erro"] = f"{type(e).__name__}: {e}"
                    lane_log.warning(
                        "[%d/%d] CodItem=%s: erro na consulta — %s",
                        idx, total, cod, e,
                    )

            async with save_lock:
                registros[int(cod)] = registro
                if counter["n"] % salvar_a_cada == 0 or counter["n"] == total:
                    _salvar(out_path, registros)

        async def _lane(lid: int, ctx) -> None:
            # Jitter pra escalonar arrancada das lanes
            await asyncio.sleep(random.uniform(0.0, 2.0) + lid * 0.5)
            lane_log = get_logger(f"eproc_sp.lane{lid}")
            page = await ctx.new_page()
            try:
                # Página começa em about:blank — precisa carregar o eproc
                # uma vez antes de clicar no menu lateral.
                try:
                    await page.goto(base_url, wait_until="domcontentloaded", timeout=30_000)
                    lane_log.info("lane %d: pagina inicial carregada (%s)", lid, page.url)
                except Exception as e:
                    lane_log.warning("lane %d: erro carregando pagina inicial: %s", lid, e)
                flow = EprocSPAsyncFlow(page, lane_log, settings.logs_dir, base_url)
                while True:
                    try:
                        cand = fila.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    await _processar_um(flow, cand, lane_log)
                    if delay_s > 0 and not fila.empty():
                        await asyncio.sleep(delay_s)
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

        await asyncio.gather(*[_lane(i, contexts[i]) for i in range(len(contexts))])

        for ctx in contexts:
            await ctx.close()
        await browser.close()

    return metricas


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="consultar-migracao-sp",
        description="Consulta candidatos SP no eproc pra descobrir migrados "
                    "(Playwright multi-sessão paralela).",
    )
    parser.add_argument(
        "--desde",
        default=_default_desde(),
        help=f"data mínima de cadastro (default: últimos 7 dias = {_default_desde()})",
    )
    parser.add_argument("--limit", type=int, default=None, help="limita N candidatos.")
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help=f"path do JSON cumulativo (default: {DEFAULT_OUT}).",
    )
    parser.add_argument(
        "--out-aptos",
        default=str(DEFAULT_OUT_APTOS),
        help=f"path do JSON só dos aptos — encontrados e com arquivos (default: {DEFAULT_OUT_APTOS}).",
    )
    parser.add_argument(
        "--skip-ja-consultados",
        action="store_true",
        help="pula CodItems que já existem no JSON (apenas adiciona novos).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="N sessões isoladas paralelas (default: 4). Cada uma faz seu login "
             "TOTP serial. Mais workers = mais throughput, mas mais tempo de login.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="segundos entre consultas DENTRO de cada lane (default: 2).",
    )
    parser.add_argument(
        "--por-sessao",
        type=int,
        default=8,
        help="quantas consultas cada sessão faz antes de morrer pra um novo "
             "batch de logins (default: 8 — abaixo do threshold do captcha "
             "que é ~9 por sessão). Total por batch = workers × por_sessao.",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="headless on/off (default: lê do .env)",
    )
    parser.add_argument(
        "--no-cache-cookies",
        action="store_true",
        help="ignora cookies salvos da execução anterior e força logins frescos. "
             f"Cookies válidos são reaproveitados por até {COOKIES_TTL_HOURS}h em "
             f"{COOKIES_CACHE}.",
    )
    args = parser.parse_args(argv)

    settings = Settings.load()
    if args.headless is not None:
        # passa pelo settings.headless usado dentro da função async
        settings.headless = args.headless
    setup_logger(settings.logs_dir, suffix="consultar_migracao_sp")
    log = get_logger("cli")

    out_path = Path(args.out).expanduser()
    registros = _carregar_existente(out_path)
    log.info("JSON inicial: %s — %d registro(s) existente(s)", out_path, len(registros))

    try:
        candidatos = listar_candidatos_sp_migracao(
            dt_cadastro_minimo=args.desde,
            dt_cadastro_maximo=_hoje(),
            limit=args.limit,
        )
    except Exception as e:
        log.exception("erro consultando candidatos no MySQL: %s", e)
        return 1

    if args.skip_ja_consultados:
        pulados = sum(1 for c in candidatos if c["CodItem"] in registros)
        candidatos = [c for c in candidatos if c["CodItem"] not in registros]
        log.info("--skip-ja-consultados: pulando %d já consultado(s)", pulados)

    if not candidatos:
        log.info("nenhum candidato a consultar")
        print("(nada a consultar)")
        return 0

    batch_size = args.workers * args.por_sessao
    n_batches = (len(candidatos) + batch_size - 1) // batch_size
    log.info("=" * 64)
    log.info(
        "vai consultar %d processo(s) SP — %d batch(es) de %d "
        "(%d sessões × %d por sessão), delay %.1fs entre consultas",
        len(candidatos), n_batches, batch_size,
        args.workers, args.por_sessao, args.delay,
    )
    log.info("=" * 64)

    cliente_store = EnvClienteStore.from_env_eproc_sp()
    cliente = cliente_store.get(ENV_CLIENTE_ID)
    cookie_store = CookieStore(settings.cookies_dir)

    metricas_total = {"encontrados": 0, "nao_encontrados": 0, "erros": 0}
    restantes = list(candidatos)

    # Cookies do batch 1: tenta cache, senão login
    cookies_atuais: list[list] | None = None
    if not args.no_cache_cookies:
        cookies_atuais = _carregar_cookies_cache(args.workers, log)
    if cookies_atuais is None:
        log.info("login serial pra batch 1 (%d sessões)", args.workers)
        cookies_atuais = _logins_seriais(
            cliente, settings, cliente_store, cookie_store,
            n=args.workers, headless=settings.headless, log=log,
        )
        if cookies_atuais:
            _salvar_cookies_cache(cookies_atuais, log)

    if not cookies_atuais:
        log.error("nenhum login pra batch 1 — abortando")
        return 2

    # Overlap: enquanto rodamos batch N, login do batch N+1 em paralelo (sync,
    # thread separada — pra não bloquear o asyncio das consultas)
    next_holder: dict = {"cookies": None, "thread": None}

    def _start_next_login() -> None:
        def _alvo() -> None:
            try:
                next_holder["cookies"] = _logins_seriais(
                    cliente, settings, cliente_store, cookie_store,
                    n=args.workers, headless=settings.headless, log=log,
                )
            except Exception as e:
                log.exception("login overlap falhou: %s", e)
                next_holder["cookies"] = []
        t = threading.Thread(target=_alvo, daemon=True)
        t.start()
        next_holder["thread"] = t

    batch_idx = 0
    while restantes:
        batch_idx += 1
        batch = restantes[:batch_size]
        restantes = restantes[batch_size:]
        log.info("=" * 64)
        log.info("BATCH %d/%d — %d itens (%d restantes após)",
                 batch_idx, n_batches, len(batch), len(restantes))
        log.info("=" * 64)

        # Dispara login do próximo batch em background, se ainda houver
        if restantes:
            log.info("disparando login do próximo batch em background (overlap)")
            _start_next_login()

        try:
            metricas_batch = asyncio.run(_rodar_lanes_async(
                cookies_por_sessao=cookies_atuais,
                candidatos=batch,
                registros=registros,
                out_path=out_path,
                log=log,
                settings=settings,
                base_url=EprocSPAdapter.LOGIN_URL,
                delay_s=args.delay,
                salvar_a_cada=10,
            ))
            for k in metricas_total:
                metricas_total[k] += metricas_batch.get(k, 0)
        except Exception as e:
            log.exception("erro fatal nas lanes async (batch %d): %s", batch_idx, e)

        # Pega cookies do próximo batch (espera thread terminar se ainda rodando)
        if restantes:
            t = next_holder["thread"]
            if t is not None:
                if t.is_alive():
                    log.info("aguardando login do próximo batch terminar")
                t.join()
            cookies_atuais = next_holder["cookies"] or []
            next_holder["cookies"] = None
            next_holder["thread"] = None
            if cookies_atuais:
                _salvar_cookies_cache(cookies_atuais, log)
            else:
                log.error("login overlap falhou — abortando")
                break

    metricas = metricas_total

    _salvar(out_path, registros)

    aptos = sorted(
        (r for r in registros.values() if r.get("encontrado") and r.get("tem_arquivos")),
        key=lambda r: r["cod_item"],
    )
    aptos_path = Path(args.out_aptos).expanduser() if args.out_aptos else DEFAULT_OUT_APTOS
    aptos_path.parent.mkdir(parents=True, exist_ok=True)
    aptos_path.write_text(json.dumps(aptos, ensure_ascii=False, indent=2), encoding="utf-8")
    stack = [r["cod_item"] for r in aptos]

    print("\n" + "=" * 64)
    print("RESUMO — consultar migração SP (Playwright multi-sessão)")
    print("=" * 64)
    print(f"consultados nesta rodada: {len(candidatos)}")
    print(f"  ✓ encontrados:     {metricas['encontrados']}")
    print(f"  ✗ não encontrados: {metricas['nao_encontrados']}")
    print(f"  ! erros:           {metricas['erros']}")
    print(f"JSON cumulativo: {out_path}")
    print(f"JSON só aptos:   {aptos_path}")
    print(f"\naptos (encontrado + tem arquivos): {len(stack)} CodItem(s)")
    if stack:
        print(" ".join(str(c) for c in stack))
    return 0


if __name__ == "__main__":
    sys.exit(main())
