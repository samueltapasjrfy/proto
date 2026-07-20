"""Gera o resumo por UF de um ciclo, lendo os logs das fases (novos + migrados).
Uso: python scripts/cycle_report.py <log1> [<log2> ...]
- disponíveis por UF: linhas "Tribunal: eproc_xx — N item(ns)" (itens que chegaram ao browser)
- protocolados por UF: linhas "→ CNJ <20 dígitos>"
Imprime uma linha: "por UF (protocolados/disponíveis): MG 8/12 · SP 3/5 ..."
"""
import sys, re
from collections import Counter

_EPROC = {"eproc_mg": "MG", "eproc_rs": "RS", "eproc_sp": "SP", "eproc_rj": "RJ"}
_COD = {"813": "MG", "821": "RS", "826": "SP", "819": "RJ"}


def uf_cnj(c):
    d = re.sub(r"\D", "", c or "")
    return _COD.get(d[-7:-4], "?")


def main():
    disp, prot = Counter(), Counter()
    for path in sys.argv[1:]:
        try:
            txt = open(path).read()
        except OSError:
            continue
        for m in re.finditer(r"Tribunal:\s*(eproc_\w+)\s*—\s*(\d+)\s*item", txt):
            disp[_EPROC.get(m.group(1), "?")] += int(m.group(2))
        for m in re.finditer(r"→ CNJ (\d{20})", txt):
            prot[uf_cnj(m.group(1))] += 1
    ufs = ["MG", "SP", "RS", "RJ"]
    parts = [f"{u} {prot[u]}/{disp[u]}" for u in ufs if disp[u] or prot[u]]
    print("por UF (protocolados/disponíveis): " + (" · ".join(parts) if parts else "nada"))


if __name__ == "__main__":
    main()
