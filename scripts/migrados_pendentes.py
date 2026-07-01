"""Cruza os JSONs de descoberta (migracao_{mg,sp,rj}_aptos.json) com o banco e
lista os migrados pendentes (status 1/10, não concluídos, fora da blacklist).
Grava /tmp/run_mig.txt e imprime a contagem por tribunal.
"""
import json, re
from collections import Counter

import rpa.config  # carrega .env
from rpa.db import conexao

BLACK = "data/pendencias_manuais.json"


def trib(c):
    return {"813": "MG", "821": "RS", "826": "SP", "819": "RJ"}.get(
        re.sub(r"\D", "", c or "")[-7:-4], "?"
    )


def main():
    bloq = set(json.load(open(BLACK))["todos"])
    todos = set()
    for t in ("mg", "sp", "rj"):
        try:
            for e in json.load(open(f"data/migracao_{t}_aptos.json")):
                todos.add(e["cod_item"])
        except FileNotFoundError:
            pass
    todos -= bloq
    if not todos:
        print("MIGRADOS: 0")
        open("/tmp/run_mig.txt", "w").write("")
        return
    inlist = ",".join(map(str, sorted(todos)))
    with conexao() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT t.CodItem, t.CodStatusCheckin st, t.DtConclusao, "
                "REPLACE(REPLACE(REPLACE(p.NumProcessoCNJ,'.',''),'-',''),' ','') cnj "
                "FROM tbitens t JOIN tbprocessos p ON t.IdProc=p.IdProc "
                f"WHERE t.CodItem IN ({inlist})"
            )
            rows = [dict(r) for r in cur.fetchall()]
    run = [r for r in rows if r["st"] in (1, 10) and not r["DtConclusao"]]
    ids = sorted(r["CodItem"] for r in run)
    print(f"MIGRADOS: {len(ids)}  {dict(Counter(trib(r['cnj']) for r in run))}")
    open("/tmp/run_mig.txt", "w").write(" ".join(map(str, ids)))


if __name__ == "__main__":
    main()
