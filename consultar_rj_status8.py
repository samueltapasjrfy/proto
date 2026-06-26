"""One-off: consulta os CodItems RJ em status 8 (fora do candidato normal 1/10)
pra descobrir quais estão migrados no eproc. Reusa os helpers de
consultar_migracao_rj.py. Imprime/salva só os encontrados+com-arquivos.
"""
import json, re, time
from pathlib import Path

from rpa.config import Settings
from rpa.db import conexao, listar_arquivos_do_item
from rpa.logger import get as get_logger, setup as setup_logger
from rpa.storage import ENV_CLIENTE_ID, CookieStore, EnvClienteStore
from rpa.tribunais.eproc_rj import EprocRJAdapter
from consultar_migracao_rj import _preparar_sessao_http, _consultar_http

IDS = [int(x) for x in open("/tmp/rj_status8.txt").read().split()]

settings = Settings.load()
setup_logger(settings.logs_dir, suffix="consultar_rj_status8")
log = get_logger("cli")

inlist = ",".join(map(str, IDS))
with conexao() as c:
    with c.cursor() as cur:
        cur.execute(f"SELECT t.CodItem,t.IdProc,p.NumProcesso,p.NumProcessoCNJ "
                    f"FROM tbitens t JOIN tbprocessos p ON t.IdProc=p.IdProc "
                    f"WHERE t.CodItem IN ({inlist})")
        cand = [dict(r) for r in cur.fetchall()]
log.info("candidatos status-8 RJ: %d", len(cand))

cliente_store = EnvClienteStore.from_env_eproc_rj()
cliente = cliente_store.get(ENV_CLIENTE_ID)
cookie_store = CookieStore(settings.cookies_dir)

with EprocRJAdapter(cliente, settings=settings, cliente_store=cliente_store,
                    cookie_store=cookie_store, headless=True) as adapter:
    adapter.login()
    session, hash_form, referer = _preparar_sessao_http(adapter)
log.info("sessão HTTP pronta — hash=%s cookies=%d", hash_form, len(session.cookies))

migrados, nao, erros = [], 0, 0
for i, cd in enumerate(cand, 1):
    cod = cd["CodItem"]
    cnj = re.sub(r"\D", "", str(cd.get("NumProcessoCNJ") or ""))
    arqs = listar_arquivos_do_item(cod)
    if len(cnj) != 20:
        erros += 1; log.warning("CodItem=%s CNJ inválido", cod); continue
    try:
        res = _consultar_http(session, cnj, hash_form, referer)
    except Exception as e:
        erros += 1; log.warning("CodItem=%s erro %s", cod, e); continue
    if res["encontrado"] and arqs:
        migrados.append(cod)
        log.info("[%d/%d] CodItem=%s MIGRADO+arq (%d arq)", i, len(cand), cod, len(arqs))
    elif res["encontrado"]:
        log.info("[%d/%d] CodItem=%s migrado SEM arquivos", i, len(cand), cod)
    elif res["erro"] and "expirou" in (res["erro"] or ""):
        log.error("sessão expirou em CodItem=%s — abortando", cod); break
    else:
        nao += 1
    time.sleep(0.4)

print(f"\nMIGRADOS (encontrado+arquivos): {len(migrados)}")
print(" ".join(map(str, sorted(migrados))))
print(f"nao_encontrados: {nao} | erros: {erros}")
open("/tmp/rj_status8_migrados.txt", "w").write(" ".join(map(str, sorted(migrados))))
