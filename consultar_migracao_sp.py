"""Descoberta de processos SP migrados pro eproc — via HTTP direto.

Pega os candidatos SP que passam nos filtros normais de status/documento
mas estão fora do filtro "óbvio" de migração (CNJ começa com 4 e ano >= 2025),
consulta cada um no eproc-SP e salva o resultado num JSON local.

Estratégia (HTTP com reciclagem de login):
- O eproc-SP exige um captcha a cada ~9 consultas POR SESSÃO. Re-logar (sessão
  nova) zera esse contador — verificado empiricamente.
- Então: faz batches de `--por-sessao` (default 8, abaixo do gatilho de 9).
  Cada batch: 1 login Playwright (captura cookies + hash) → fecha o browser →
  roda as ~8 consultas via `requests` HTTP direto no endpoint AJAX
  (`processos_consulta_por_numprocesso`), cada uma ~50-300ms.
- Entre batches espera a próxima janela TOTP de 30s (o eproc rejeita o mesmo
  código TOTP consecutivo).
- Se o captcha disparar antes do fim do batch, os itens restantes voltam pra
  fila do próximo login (não se perdem).
- Salva tudo em `data/migracao_sp.json` (cumulativo, idempotente).

Por que mudou de Playwright multi-sessão pra HTTP: a navegação Playwright
carrega páginas inteiras (lento, ~20s/consulta quando o eproc trava) e a
abordagem antiga de N lanes concorrentes do mesmo IP estourava rápido. O HTTP
direto (1 request leve por consulta) é ~100x mais rápido por consulta; só
precisa reciclar a sessão a cada ~8 pra driblar o captcha. Resposta XML = não
encontrado; JSON com linkProcessoAssinado = encontrado; JSON com captcha = muro.

Uso:
    python consultar_migracao_sp.py
    python consultar_migracao_sp.py --desde 2025-01-01 --limit 50
    python consultar_migracao_sp.py --skip-ja-consultados
    python consultar_migracao_sp.py --por-sessao 8
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
from rpa.db import listar_arquivos_do_item, listar_candidatos_sp_migracao
from rpa.logger import get as get_logger, setup as setup_logger
from rpa.storage import ENV_CLIENTE_ID, CookieStore, EnvClienteStore
from rpa.tribunais.eproc_sp import EprocSPAdapter


DEFAULT_OUT = Path("data") / "migracao_sp.json"
DEFAULT_OUT_APTOS = Path("data") / "migracao_sp_aptos.json"
BASE = "https://eproc1g.tjsp.jus.br/eproc"


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


def _preparar_sessao_http(adapter: EprocSPAdapter) -> tuple[requests.Session, str, str]:
    """Após login, navega pra tela de consulta, captura hash do form e cookies
    (inclusive cf_clearance do Cloudflare), e devolve uma `requests.Session`
    pronta + (hash_form, referer).
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
    - HTML / outros → sessão expirou ou Cloudflare bloqueou
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

    # Caso degenerado: HTML / corpo inesperado → sessão expirou ou Cloudflare.
    snippet = body[:200].replace("\n", " ")
    cf = "cloudflare" in body.lower() or "just a moment" in body.lower() or resp.status_code in (403, 429, 503)
    motivo = "Cloudflare bloqueou" if cf else "sessão expirou?"
    out["erro"] = f"resposta inesperada ({motivo}): {snippet!r}"
    out["_cloudflare"] = cf
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="consultar-migracao-sp",
        description="Consulta candidatos SP no eproc pra descobrir migrados (via HTTP).",
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
        default=0.15,
        help="segundos entre consultas (default: 0.15).",
    )
    parser.add_argument(
        "--por-sessao",
        type=int,
        default=8,
        help="quantas consultas por login antes de reciclar a sessão (default: 8 "
             "— abaixo do gatilho do captcha que é ~9).",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="headless on/off para o login (default: True)",
    )
    args = parser.parse_args(argv)

    settings = Settings.load()
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

    por_sessao = max(1, args.por_sessao)
    n_batches = (len(candidatos) + por_sessao - 1) // por_sessao
    log.info("=" * 64)
    log.info("vai consultar %d processo(s) SP via HTTP — %d batch(es) de até %d "
             "(re-login a cada batch pra zerar o captcha)",
             len(candidatos), n_batches, por_sessao)
    log.info("=" * 64)

    cliente_store = EnvClienteStore.from_env_eproc_sp()
    cliente = cliente_store.get(ENV_CLIENTE_ID)
    cookie_store = CookieStore(settings.cookies_dir)

    def _abrir_sessao_http():
        """1 login Playwright → cookies + hash → fecha browser. Devolve
        (session, hash_form, referer)."""
        with EprocSPAdapter(
            cliente,
            settings=settings,
            cliente_store=cliente_store,
            cookie_store=cookie_store,
            headless=args.headless,
        ) as adapter:
            adapter.login()
            return _preparar_sessao_http(adapter)

    encontrados = 0
    nao_encontrados = 0
    erros = 0
    processados = 0

    restantes = list(candidatos)
    primeiro = True
    batch_idx = 0
    while restantes:
        batch_idx += 1
        # Entre logins, espera a próxima janela TOTP (eproc rejeita código repetido).
        if not primeiro:
            espera = 30 - (time.time() % 30) + 1
            log.info("aguardando %.1fs próxima janela TOTP antes do batch %d",
                     espera, batch_idx)
            time.sleep(espera)
        primeiro = False

        log.info("-" * 64)
        log.info("BATCH %d/%d — login Playwright pra sessão HTTP nova", batch_idx, n_batches)
        try:
            session, hash_form, referer = _abrir_sessao_http()
        except Exception as e:
            log.exception("falha preparando sessão HTTP (batch %d): %s", batch_idx, e)
            return 2
        log.info("sessão HTTP pronta — hash_form=%s, cookies=%d", hash_form, len(session.cookies))

        # Consome até `por_sessao` itens nesta sessão. Se o captcha disparar antes,
        # paramos o batch e os restantes seguem pra próxima sessão.
        feitos_no_batch = 0
        captcha = False
        while restantes and feitos_no_batch < por_sessao:
            cand = restantes[0]
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
                    if res.get("_cloudflare"):
                        # Captcha/bloqueio: NÃO consome o item — recicla sessão.
                        log.warning("CodItem=%s: captcha disparou no item %d do batch — "
                                    "reciclando sessão (%s)",
                                    cod, feitos_no_batch + 1, res["erro"])
                        captcha = True
                        break
                    registro["encontrado"] = res["encontrado"]
                    registro["numero_formatado"] = res["numero_formatado"]
                    registro["url"] = res["url"]
                    registro["titulo"] = res["titulo"]
                    registro["erro"] = res["erro"]
                    if res["encontrado"]:
                        encontrados += 1
                        log.info("[%d/%d] CodItem=%s ENCONTRADO — %s (%dms)",
                                 processados + 1, len(candidatos), cod,
                                 res["numero_formatado"] or cnj, res["_tempo_ms"])
                    else:
                        nao_encontrados += 1
                        log.info("[%d/%d] CodItem=%s não encontrado (%dms)",
                                 processados + 1, len(candidatos), cod, res["_tempo_ms"])
                except Exception as e:
                    erros += 1
                    registro["erro"] = f"{type(e).__name__}: {e}"
                    log.warning("CodItem=%s: erro na consulta — %s", cod, e)

            # Item consumido (sucesso, não-encontrado, CNJ inválido ou erro de rede).
            registros[int(cod)] = registro
            restantes.pop(0)
            feitos_no_batch += 1
            processados += 1
            if processados % 25 == 0:
                _salvar(out_path, registros)

            if args.delay > 0 and restantes and feitos_no_batch < por_sessao:
                time.sleep(args.delay)

        _salvar(out_path, registros)
        if captcha and feitos_no_batch == 0:
            # Sessão nova já veio captcha'd logo no 1º item: provável bloqueio por
            # IP, não por sessão. Aborta pra não entrar em loop de re-login.
            log.error("captcha logo no 1º item de uma sessão nova — provável bloqueio "
                      "por IP. Abortando; tente de novo mais tarde com --skip-ja-consultados.")
            break

    aptos = sorted(
        (r for r in registros.values() if r.get("encontrado") and r.get("tem_arquivos")),
        key=lambda r: r["cod_item"],
    )
    aptos_path = Path(args.out_aptos).expanduser() if args.out_aptos else DEFAULT_OUT_APTOS
    aptos_path.parent.mkdir(parents=True, exist_ok=True)
    aptos_path.write_text(json.dumps(aptos, ensure_ascii=False, indent=2), encoding="utf-8")
    stack = [r["cod_item"] for r in aptos]

    print("\n" + "=" * 64)
    print("RESUMO — consultar migração SP (HTTP)")
    print("=" * 64)
    print(f"consultados nesta rodada: {processados}/{len(candidatos)}  ({batch_idx} batch/login)")
    print(f"  ✓ encontrados:     {encontrados}")
    print(f"  ✗ não encontrados: {nao_encontrados}")
    print(f"  ! erros:           {erros}")
    if restantes:
        print(f"  ⚠ {len(restantes)} não consultado(s) (captcha/IP) — rode de novo com --skip-ja-consultados")
    print(f"JSON cumulativo: {out_path}")
    print(f"JSON só aptos:   {aptos_path}")
    print(f"\naptos (encontrado + tem arquivos): {len(stack)} CodItem(s)")
    if stack:
        print(" ".join(str(c) for c in stack))
    return 0


if __name__ == "__main__":
    sys.exit(main())
