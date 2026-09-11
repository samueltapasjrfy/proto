"""Recolhe Chromes órfãos do NOSSO robô, sem tocar nos do outro robô.

Esta máquina roda dois robôs Playwright ao mesmo tempo. Um `pkill -f
ms-playwright` mata os dois — foi o que aconteceu em 11/set/2026, quando a
limpeza derrubou os browsers do robo-pje-mg no meio da execução dele.

Como distinguir, sem depender de PID guardado em arquivo:
  - robo-pje-mg usa perfil PERSISTENTE dentro do repo dele:
      --user-data-dir=/Users/.../Documents/robo-pje-mg/data/profile_ba_wN
  - nós usamos `chromium.launch()` sem user_data_dir, então o Playwright cria
    um perfil TEMPORÁRIO em $TMPDIR:
      --user-data-dir=/var/folders/xx/.../T/playwright_chromiumdev_profile-XXXX
Todo processo do browser (renderer, gpu, crashpad) herda essa flag, então ela
identifica a árvore inteira. Só matamos o que casa com o padrão temporário —
é allowlist, não denylist: um robô novo no futuro não entra por engano.

Uso:
  python3 scripts/chrome_gc.py list      # só relata, não mata
  python3 scripts/chrome_gc.py orphans   # mata os NOSSOS já órfãos (default)
  python3 scripts/chrome_gc.py all       # mata todos os nossos (recusa se há run ativo)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

# Perfil temporário do Playwright = nosso. Exige /var/folders (TMPDIR do macOS)
# E o prefixo que o Playwright usa, pra não pegar temp de terceiros.
NOSSO = re.compile(r"--user-data-dir=(/var/folders/\S*?playwright_\S*)")
RUN_ATIVO = re.compile(r"Python\.app.*main\.py processar")


def _ps() -> list[tuple[int, int, str]]:
    out = subprocess.run(
        ["ps", "-eww", "-o", "pid=,ppid=,command="], capture_output=True, text=True
    ).stdout
    linhas = []
    for ln in out.splitlines():
        parte = ln.strip().split(None, 2)
        if len(parte) == 3 and parte[0].isdigit() and parte[1].isdigit():
            linhas.append((int(parte[0]), int(parte[1]), parte[2]))
    return linhas


def classificar(procs):
    nossos, outros = {}, 0
    for pid, ppid, cmd in procs:
        if "ms-playwright" not in cmd and "Chrome for Testing" not in cmd:
            continue
        if NOSSO.search(cmd):
            nossos[pid] = ppid
        else:
            outros += 1
    return nossos, outros


def orfaos(nossos: dict[int, int], vivos: set[int]) -> list[int]:
    """Nosso pid é órfão se subindo a cadeia de pais não existe mais um dono
    vivo fora da árvore do browser (ou seja, o python que o lançou morreu)."""
    alvo = []
    for pid, ppid in nossos.items():
        atual = ppid
        visto = set()
        while atual > 1 and atual not in visto:
            visto.add(atual)
            if atual not in nossos:
                break  # chegou num dono fora da árvore
            atual = nossos[atual]
        if atual <= 1 or atual not in vivos:
            alvo.append(pid)
    return alvo


def main() -> int:
    modo = sys.argv[1] if len(sys.argv) > 1 else "orphans"
    procs = _ps()
    vivos = {p for p, _, _ in procs}
    nossos, outros = classificar(procs)
    run_ativo = any(RUN_ATIVO.search(c) for _, _, c in procs)

    if modo == "list":
        print(f"nossos={len(nossos)} outro_robo={outros} "
              f"orfaos_nossos={len(orfaos(nossos, vivos))} run_ativo={run_ativo}")
        return 0

    if modo == "all":
        if run_ativo and os.getenv("RPA_GC_FORCE") != "1":
            print(f"gc: recusado — há run ativo (use RPA_GC_FORCE=1). nossos={len(nossos)}")
            return 1
        alvo = list(nossos)
    else:
        alvo = orfaos(nossos, vivos)

    for pid in alvo:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        except PermissionError:
            print(f"gc: sem permissão pra matar {pid}")
    print(f"gc[{modo}]: matou {len(alvo)} nossos | preservados do outro robô: {outros}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
