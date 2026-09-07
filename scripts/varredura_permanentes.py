"""Varre os logs do cron e identifica CodItems que falham SEMPRE pelo mesmo
motivo permanente (dado ruim / bloqueio do tribunal), pra tirá-los do loop de
repesca infinita.

Permanente = motivo que vai se repetir identicamente em toda tentativa:
  - score_baixo / cnj_ausente_no_pdf / download_falhou  -> pré-check no PDF, determinístico
  - remetido_ao_tj / peticionar_bloqueado               -> o eproc recusa explicitamente
  - nao_encontrado                                      -> processo não existe no eproc

Exige MIN_OCORRENCIAS falhas em execuções distintas antes de blacklistar (evita
matar item por causa de um azar pontual). Uso:
    python scripts/varredura_permanentes.py            # dry-run (só mostra)
    python scripts/varredura_permanentes.py --apply    # blacklista + status 1
"""
import glob, json, re, sys
from collections import defaultdict

import rpa.config
from rpa.db import conexao

BLACK = "data/pendencias_manuais.json"
MIN_OCORRENCIAS = 2          # nº de execuções distintas com o mesmo motivo
MIN_NAO_ENCONTRADO = 3       # "não encontrado" pode ser processo ainda não migrado

PADROES = [
    ("score_baixo",        re.compile(r"CodItem=(\d+): ABORT — score do melhor candidato")),
    ("cnj_ausente_no_pdf", re.compile(r"CodItem=(\d+): ABORT — CNJ \d+ não aparece no PDF principal")),
    ("download_falhou",    re.compile(r"CodItem=(\d+): nenhum PDF utilizável após download")),
    ("remetido_ao_tj",     re.compile(r"CodItem=(\d+): falhou — eproc bloqueou.*REMETIDO AO TJ")),
    ("peticionar_bloqueado", re.compile(r"CodItem=(\d+): falhou — eproc bloqueou.*Não é possível peticionar")),
    ("nao_encontrado",     re.compile(r"✗ (\d+) — consulta: Processo não encontrado")),
]


def main():
    aplicar = "--apply" in sys.argv
    p = json.load(open(BLACK))
    ja_bloq = set(p.get("todos", []))

    # motivo -> cod -> set(arquivos em que falhou)
    ocorr = defaultdict(lambda: defaultdict(set))
    for log in glob.glob("data/auto_logs/*.log"):
        try:
            txt = open(log, errors="replace").read()
        except OSError:
            continue
        for motivo, rx in PADROES:
            for m in rx.finditer(txt):
                ocorr[motivo][int(m.group(1))].add(log)

    # candidatos = atingiram o mínimo de ocorrências e ainda não estão na blacklist
    cand = {}
    for motivo, mapa in ocorr.items():
        minimo = MIN_NAO_ENCONTRADO if motivo == "nao_encontrado" else MIN_OCORRENCIAS
        for cod, arquivos in mapa.items():
            if cod in ja_bloq or len(arquivos) < minimo:
                continue
            # se cair em mais de um motivo, fica com o de maior contagem
            if cod not in cand or len(arquivos) > cand[cod][1]:
                cand[cod] = (motivo, len(arquivos))

    if not cand:
        print("nenhum candidato novo — nada a blacklistar")
        return

    # descarta os que já foram concluídos (status 9) — não fazem mal a ninguém
    inlist = ",".join(map(str, sorted(cand)))
    with conexao() as c:
        with c.cursor() as cur:
            cur.execute(
                f"SELECT CodItem, CodStatusCheckin st, DtConclusao FROM tbitens WHERE CodItem IN ({inlist})"
            )
            estado = {r["CodItem"]: dict(r) for r in cur.fetchall()}

    alvos, concluidos, em_execucao = {}, [], []
    for cod, (motivo, n) in cand.items():
        e = estado.get(cod)
        if e is None:
            continue
        if e["st"] == 9 or e["DtConclusao"]:
            concluidos.append(cod)
        elif e["st"] == 11:
            em_execucao.append(cod)        # em voo agora: não mexer
        else:
            alvos[cod] = (motivo, n, e["st"])

    por_motivo = defaultdict(list)
    for cod, (motivo, n, st) in sorted(alvos.items()):
        por_motivo[motivo].append((cod, n, st))

    print(f"{'APLICANDO' if aplicar else 'DRY-RUN'} — candidatos a blacklist: {len(alvos)}")
    for motivo, itens in sorted(por_motivo.items()):
        print(f"\n  {motivo} ({len(itens)}):")
        for cod, n, st in itens:
            print(f"    {cod}  (falhou em {n} execuções, status atual {st})")
    if concluidos:
        print(f"\n  ignorados (já concluídos): {len(concluidos)} -> {sorted(concluidos)}")
    if em_execucao:
        print(f"  ignorados (em execução agora, status 11): {sorted(em_execucao)}")

    if not aplicar:
        print("\n(dry-run — rode com --apply pra gravar)")
        return

    for cod, (motivo, _n, _st) in alvos.items():
        p.setdefault(motivo, [])
        if cod not in p[motivo]:
            p[motivo].append(cod)
    p["todos"] = sorted(set(p.get("todos", [])) | set(alvos))
    json.dump(p, open(BLACK, "w"), ensure_ascii=False, indent=2)
    print(f"\nblacklist gravada — total agora: {len(p['todos'])}")

    # Volta pra status 1 (pendente) SOMENTE quem está no pool de aptos (1/10).
    # Itens em 3/5/8 estão fora do pool — o robô já não os pega, e movê-los pra 1
    # os INJETARIA no pool, o contrário do que queremos. Esses só entram na blacklist.
    no_pool = [c for c, (_m, _n, st) in alvos.items() if st in (1, 10)]
    fora_pool = {c: st for c, (_m, _n, st) in alvos.items() if st not in (1, 10)}
    if no_pool:
        with conexao() as c:
            with c.cursor() as cur:
                cur.execute(
                    f"UPDATE tbitens SET CodStatusCheckin=1, UserAtualizacao='admin.jurify' "
                    f"WHERE CodItem IN ({','.join(map(str, sorted(no_pool)))})"
                )
                n = cur.rowcount
            c.commit()
        print(f"status -> 1 (pendente) em {n} item(ns) que estavam no pool (1/10)")
    if fora_pool:
        print(f"status PRESERVADO (fora do pool, so blacklist): {fora_pool}")


if __name__ == "__main__":
    main()
