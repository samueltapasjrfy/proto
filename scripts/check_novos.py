"""Lista aptos novos (status 1/10, janela 7 dias, fora da blacklist) por tribunal.
Uso: python scripts/check_novos.py   -> imprime contagem + ids e grava /tmp/run_novos.txt
"""
import json, re
from datetime import date, timedelta
from collections import Counter

import rpa.config  # carrega .env
from rpa.db import listar_protocolos_aptos

BLACK = "data/pendencias_manuais.json"


def trib(c):
    return {"813": "MG", "821": "RS", "826": "SP", "819": "RJ"}.get(
        re.sub(r"\D", "", c or "")[-7:-4], "?"
    )


def main():
    bloq = set(json.load(open(BLACK))["todos"])
    desde = (date.today() - timedelta(days=7)).isoformat()
    hoje = date.today().isoformat()
    itens = listar_protocolos_aptos(dt_cadastro_minimo=desde, dt_cadastro_maximo=hoje)
    novos = [it for it in itens if it["CodItem"] not in bloq]
    cont = dict(Counter(trib(it.get("NumProcessoCNJ")) for it in novos))
    ids = sorted(it["CodItem"] for it in novos)
    print(f"NOVOS: {len(novos)}  {cont}")
    print(" ".join(map(str, ids)))
    open("/tmp/run_novos.txt", "w").write(" ".join(map(str, ids)))


if __name__ == "__main__":
    main()
