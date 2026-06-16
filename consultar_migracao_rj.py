"""Descoberta de processos RJ migrados pro eproc.

Pega os candidatos RJ que passam nos filtros normais de status/documento
mas estão fora do filtro "óbvio" de migração (CNJ começa com 3 e ano >= 2025),
consulta cada um no eproc-MG e salva o resultado num JSON local.

Estratégia:
- Login com Playwright UMA VEZ → captura cookies + hash do form de consulta.
- Loop de consultas via `requests` HTTP direto no endpoint AJAX
  (`processos_consulta_por_numprocesso`), ~10x mais rápido que navegar
  pelo browser. Resposta XML = não encontrado; JSON = encontrado.
- Salva tudo em `data/migracao_rj.json` (cumulativo, idempotente).

Uso:
    python consultar_migracao_rj.py
    python consultar_migracao_rj.py --desde 2025-01-01 --limit 50
    python consultar_migracao_rj.py --skip-ja-consultados
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from rpa.config import Settings
from rpa.db import listar_arquivos_do_item, listar_candidatos_rj_migracao
from rpa.logger import get as get_logger, setup as setup_logger
from rpa.storage import ENV_CLIENTE_ID, CookieStore, EnvClienteStore
from rpa.tribunais.eproc_rj import EprocRJAdapter


DEFAULT_OUT = Path("data") / "migracao_rj.json"
DEFAULT_OUT_APTOS = Path("data") / "migracao_rj_aptos.json"
BASE = "https://eproc1g.tjrj.jus.br/eproc"


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


def _preparar_sessao_http(adapter: EprocRJAdapter) -> tuple[requests.Session, str, str]:
    """Após login, navega pra tela de consulta, captura hash do form e cookies,
    e devolve uma `requests.Session` pronta + (hash_form, referer).
    """
    adapter._abrir_consulta_processual()
    referer = adapter.page.url
    hash_form = adapter.page.evaluate(
        """() => {
            const div = document.querySelector('#divNumNrProcesso');
            if (!div) return null;
            const v = div.getAttribute('data-acaoassinada') || '';
            const m = v.match(/hash=([a-f0-9]+)/);
            return m ? m[1] : null;
        }"""
    )
    if not hash_form:
        raise RuntimeError("não foi possível extrair hash do form de consulta")

    s = requests.Session()
    for c in adapter.context.cookies():
        s.cookies.set(c["name"], c["value"], domain=c.get("domain"))
    return s, hash_form, referer


def _consultar_http(
    session: requests.Session,
    cnj: str,
    hash_form: str,
    referer: str,
    timeout_s: float = 15,
) -> dict:
    """Faz POST AJAX e devolve dict tipado com o resultado.

    Resposta do eproc:
    - XML com <erros> ... <erro descricao="...">  → não encontrado
    - JSON com 'resultadoUnico' e 'linkProcessoAssinado' → encontrado
    - HTML / outros → sessão expirou
    """
    url = f"{BASE}/controlador_ajax.php?acao_ajax=processos_consulta_por_numprocesso&hash={hash_form}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": BASE.rsplit("/", 1)[0],
        "Referer": referer,
    }
    payload = {
        "hdnInfraTipoPagina": "1",
        "strIdForm": "frmProcessoListaAjax",
        "fnValidacao[]": ["gerenciadorTelaConsulta", "executarValidacoes"],
        "acao_origem": "consultar",
        "acao_retorno": "",
        "acao": "processo_consultar",
        "tipoPesquisa": "NU",
        "numNrProcesso": cnj,
        "selIdClasseSelecionados": "",
        "strChave": "",
    }
    resp = session.post(url, headers=headers, data=payload, timeout=timeout_s)
    body_bytes = resp.content or b""
    body = body_bytes.decode("iso-8859-1", errors="replace")

    out: dict = {
        "encontrado": False,
        "erro": None,
        "numero_formatado": None,
        "url": None,
        "titulo": None,
        "_status_http": resp.status_code,
        "_tempo_ms": int(resp.elapsed.total_seconds() * 1000),
    }

    if "<erros>" in body or "<erro " in body:
        m = re.search(r'descricao="([^"]+)"', body)
        out["erro"] = m.group(1) if m else "não encontrado"
        return out

    if body.lstrip().startswith("{") and "linkProcessoAssinado" in body:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}
        registros = data.get("registros") or []
        link = None
        if registros:
            link = registros[0].get("linkProcessoAssinado")
            num_fmt = registros[0].get("nr_processo")
            out["numero_formatado"] = num_fmt
        if link and not link.startswith("http"):
            link = f"{BASE}/{link.lstrip('/')}"
        out["encontrado"] = True
        out["url"] = link
        out["titulo"] = out["numero_formatado"]
        return out

    # Caso degenerado: HTML / corpo inesperado → sessão expirou ou algo mudou
    snippet = body[:200].replace("\n", " ")
    out["erro"] = f"resposta inesperada (sessão expirou?): {snippet!r}"
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="consultar-migracao-rj",
        description="Consulta candidatos RJ no eproc pra descobrir migrados (via HTTP).",
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
        "--delay",
        type=float,
        default=0.1,
        help="segundos entre consultas (default: 0.1).",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="headless on/off para o login (default: True)",
    )
    args = parser.parse_args(argv)

    settings = Settings.load()
    setup_logger(settings.logs_dir, suffix="consultar_migracao_rj")
    log = get_logger("cli")

    out_path = Path(args.out).expanduser()
    registros = _carregar_existente(out_path)
    log.info("JSON inicial: %s — %d registro(s) existente(s)", out_path, len(registros))

    try:
        candidatos = listar_candidatos_rj_migracao(
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

    log.info("=" * 64)
    log.info("vai consultar %d processo(s) RJ via HTTP", len(candidatos))
    log.info("=" * 64)

    cliente_store = EnvClienteStore.from_env_eproc_mg()
    cliente = cliente_store.get(ENV_CLIENTE_ID)
    cookie_store = CookieStore(settings.cookies_dir)

    # Login com playwright só pra capturar cookies + hash, depois fecha o browser.
    log.info("login Playwright (1x) — capturando cookies + hash do form")
    with EprocRJAdapter(
        cliente,
        settings=settings,
        cliente_store=cliente_store,
        cookie_store=cookie_store,
        headless=args.headless,
    ) as adapter:
        try:
            adapter.login()
            session, hash_form, referer = _preparar_sessao_http(adapter)
        except Exception as e:
            log.exception("falha preparando sessão HTTP: %s", e)
            return 2
    log.info("sessão HTTP pronta — hash_form=%s, cookies=%d",
             hash_form, len(session.cookies))

    encontrados = 0
    nao_encontrados = 0
    erros = 0
    sessao_expirada = False

    for i, cand in enumerate(candidatos, start=1):
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
            log.warning("CodItem=%s: erro listando arquivos: %s", cod, e)

        if len(cnj) != 20:
            registro["erro"] = f"CNJ inválido ({len(cnj)} dígitos)"
            erros += 1
            log.warning("CodItem=%s: CNJ inválido %r", cod, cnj)
        else:
            try:
                res = _consultar_http(session, cnj, hash_form, referer)
                registro["encontrado"] = res["encontrado"]
                registro["numero_formatado"] = res["numero_formatado"]
                registro["url"] = res["url"]
                registro["titulo"] = res["titulo"]
                registro["erro"] = res["erro"]
                if res["encontrado"]:
                    encontrados += 1
                    log.info(
                        "[%d/%d] CodItem=%s ENCONTRADO — %s (%dms)",
                        i, len(candidatos), cod,
                        res["numero_formatado"] or cnj, res["_tempo_ms"],
                    )
                elif res["erro"] and "expirou" in (res["erro"] or ""):
                    sessao_expirada = True
                    log.error("CodItem=%s: sessão HTTP expirou — abortando loop", cod)
                    erros += 1
                    break
                else:
                    nao_encontrados += 1
                    log.info(
                        "[%d/%d] CodItem=%s não encontrado (%dms)",
                        i, len(candidatos), cod, res["_tempo_ms"],
                    )
            except Exception as e:
                erros += 1
                registro["erro"] = f"{type(e).__name__}: {e}"
                log.warning("CodItem=%s: erro na consulta — %s", cod, e)

        registros[int(cod)] = registro
        # Persiste a cada 25 ou no último, pra não inundar IO
        if i % 25 == 0 or i == len(candidatos):
            _salvar(out_path, registros)

        if args.delay > 0 and i < len(candidatos):
            time.sleep(args.delay)

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
    print("RESUMO — consultar migração RJ (HTTP)")
    print("=" * 64)
    print(f"consultados nesta rodada: {min(len(candidatos), i if 'i' in dir() else 0)}")
    print(f"  ✓ encontrados:     {encontrados}")
    print(f"  ✗ não encontrados: {nao_encontrados}")
    print(f"  ! erros:           {erros}")
    if sessao_expirada:
        print("  ⚠ sessão expirou no meio — rode de novo com --skip-ja-consultados")
    print(f"JSON cumulativo: {out_path}")
    print(f"JSON só aptos:   {aptos_path}")
    print(f"\naptos (encontrado + tem arquivos): {len(stack)} CodItem(s)")
    if stack:
        print(" ".join(str(c) for c in stack))
    return 0


if __name__ == "__main__":
    sys.exit(main())
