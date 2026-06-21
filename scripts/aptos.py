"""Helper de operação diária: lista/roda os aptos "novos", excluindo as
pendências manuais conhecidas (bloqueios reais que reaparecem como aptos).

Encapsula o fluxo que era feito à mão a cada rodada:
  1. consulta `listar_protocolos_aptos` (janela padrão = últimos 7 dias)
  2. tira os CodItems de `data/pendencias_manuais.json` (CNJ ausente, score
     baixo, remetido ao TJ, não encontrado, etc.)
  3. mostra os novos por tribunal — e opcionalmente já dispara o `processar`

Uso:
    python scripts/aptos.py list                  # só lista os novos
    python scripts/aptos.py run --workers 3        # lista E roda os novos
    python scripts/aptos.py run --workers 2 --ignorar-migracao 1607980 1607984
    python scripts/aptos.py run --workers 3 --no-peticionar   # dry-run

Sem CodItems explícitos no `run`, roda os "novos" da janela. Com CodItems,
roda exatamente esses (útil pra migrados/retries).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rpa.config  # noqa: E402,F401 — dispara load_dotenv()
from rpa.db import listar_protocolos_aptos  # noqa: E402

PENDENCIAS_PATH = Path("data") / "pendencias_manuais.json"

_TRIB = {"813": "MG", "821": "RS", "826": "SP", "819": "RJ"}


def _trib(cnj: str | None) -> str:
    d = re.sub(r"\D", "", cnj or "")
    return _TRIB.get(d[-7:-4], "?") if len(d) >= 7 else "?"


def _pendencias() -> set[int]:
    if not PENDENCIAS_PATH.exists():
        return set()
    try:
        data = json.loads(PENDENCIAS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return set(data.get("todos") or [])


def _novos(desde: str | None) -> list[dict]:
    hoje = date.today().isoformat()
    if not desde:
        desde = (date.today() - timedelta(days=7)).isoformat()
    itens = listar_protocolos_aptos(dt_cadastro_minimo=desde, dt_cadastro_maximo=hoje)
    bloq = _pendencias()
    novos = [it for it in itens if it["CodItem"] not in bloq]
    print(f"janela: {desde}..{hoje} | aptos: {len(itens)} | "
          f"pendências manuais no pool: {len(itens) - len(novos)}")
    print(f"NOVOS: {len(novos)}  "
          f"{dict(Counter(_trib(it.get('NumProcessoCNJ')) for it in novos))}")
    if novos:
        print(" ".join(str(it["CodItem"]) for it in novos))
    return novos


def _run(cods: list[int], *, workers: int, peticionar: bool,
         ignorar_migracao: bool) -> int:
    cmd = [sys.executable, "main.py", "processar", *map(str, cods),
           "--workers", str(workers)]
    cmd.append("--peticionar" if peticionar else "--no-peticionar")
    if ignorar_migracao:
        cmd.append("--ignorar-filtro-migracao")
    print("→", " ".join(cmd))
    return subprocess.call(cmd)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aptos", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="lista os aptos novos (sem rodar)")
    pl.add_argument("--desde", default=None, help="data mínima (default: -7d)")

    pr = sub.add_parser("run", help="roda os aptos novos (ou CodItems dados)")
    pr.add_argument("cod_items", nargs="*", type=int,
                    help="CodItems específicos; vazio = os novos da janela")
    pr.add_argument("--desde", default=None, help="data mínima (default: -7d)")
    pr.add_argument("--workers", type=int, default=3)
    pr.add_argument("--peticionar", action=argparse.BooleanOptionalAction, default=True)
    pr.add_argument("--ignorar-migracao", action="store_true",
                    help="passa --ignorar-filtro-migracao (pra migrados)")
    args = p.parse_args(argv)

    if args.cmd == "list":
        _novos(args.desde)
        return 0

    if args.cmd == "run":
        if args.cod_items:
            cods = args.cod_items
            print(f"rodando {len(cods)} CodItem(s) explícito(s)")
        else:
            cods = [it["CodItem"] for it in _novos(args.desde)]
            if not cods:
                print("(nada novo a rodar)")
                return 0
        return _run(cods, workers=args.workers, peticionar=args.peticionar,
                    ignorar_migracao=args.ignorar_migracao)

    return 1


if __name__ == "__main__":
    sys.exit(main())
