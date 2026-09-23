#!/bin/bash
# Um ciclo autônomo: FASE NOVOS -> FASE MIGRADOS (descoberta + execução).
# Chamado em loop pelo driver. Travas anti-duplo-protocolo:
#  - guard de data (só no dia de teste)
#  - lock por diretório (não sobrepõe dois ciclos)
#  - pula se já houver 'main.py processar' rodando (manual ou outro)
#  - listas em /tmp são limpas antes de cada check (nunca reusa lista velha)
#  - check_novos/migrados_pendentes só pegam status 1/10 (em execução/concluído ficam fora)

set -u
DIA_TESTE="2026-09-23"
REPO="/Users/samuelferreira/Documents/rpa-protocolo"
LOGDIR="$REPO/data/auto_logs"
LOCKDIR="/tmp/rpa_auto.lockdir"
export PATH="/Library/Frameworks/Python.framework/Versions/3.13/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

HOJE=$(date +%F)
mkdir -p "$LOGDIR"
AUDIT="$LOGDIR/auto.log"

if [ "$HOJE" != "$DIA_TESTE" ]; then
  echo "$(date '+%F %T') skip — fora do dia de teste ($DIA_TESTE)" >> "$AUDIT"; exit 0
fi
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  echo "$(date '+%F %T') skip — ciclo já em andamento" >> "$AUDIT"; exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT
if pgrep -f "main.py processar" >/dev/null 2>&1; then
  echo "$(date '+%F %T') skip — já há 'main.py processar' em execução" >> "$AUDIT"; exit 0
fi
cd "$REPO" || { echo "$(date '+%F %T') ERRO cd repo" >> "$AUDIT"; exit 1; }

# Recolhe Chromes NOSSOS que ficaram orfaos de um ciclo anterior morto na marra.
# NUNCA usar `pkill -f ms-playwright` aqui: o robo-pje-mg roda nesta maquina com
# os browsers dele e seria morto junto (aconteceu em 11/set/2026).
python3 scripts/chrome_gc.py orphans >> "$AUDIT" 2>&1
TS=$(date +%H%M%S)

CIC_DISP=0   # total disponíveis p/ protocolar no ciclo (novos + migrados)
CIC_PROT=0   # total realmente protocolados no eproc

# Teto de itens por ciclo. Lote grande satura a maquina (cada item abre
# browser; com ~150+ o load passa de 15, o Chromium nao renderiza a tempo e o
# login estoura o timeout de 25s -> "nenhum login funcionou" em massa). Isso
# vira bola de neve: nada protocola, a fila cresce, o lote seguinte e maior.
# Aconteceu em 10-11/set/2026: 12 ciclos seguidos com 0 protocolados e fila
# subindo de 183 para 237. Com teto, a fila drena em varios ciclos menores.
MAX_LOTE="${RPA_MAX_LOTE:-40}"

# Workers adaptativos: esta maquina roda outro robo Playwright em paralelo.
# Com o load ja alto, abrir 3 browsers por tribunal so aumenta a fila de CPU e
# faz o login estourar timeout. Acima de 75% dos cores, cai pra 2.
CORES=$(/usr/sbin/sysctl -n hw.ncpu 2>/dev/null || echo 8)
LOAD1=$(uptime | sed 's/.*averages*: //' | awk '{print int($1)}')
if [ "$LOAD1" -ge $((CORES * 3 / 4)) ]; then WK=2; else WK=3; fi
echo "$(date '+%F %T') load=$LOAD1/$CORES -> workers=$WK" >> "$AUDIT"

run_lote() {  # $1=arquivo-de-ids  $2=workers  $3=label
  local todos; todos=$(cat "$1" 2>/dev/null)
  if [ -z "${todos// }" ]; then echo "$(date '+%F %T') $3: nada a rodar" >> "$AUDIT"; return; fi
  local total; total=$(echo $todos | wc -w | tr -d ' ')
  # corta no teto; o resto fica pro proximo ciclo (nunca some silenciosamente)
  local ids; ids=$(echo $todos | tr ' ' '\n' | head -n "$MAX_LOTE" | tr '\n' ' ')
  local n; n=$(echo $ids | wc -w | tr -d ' ')
  if [ "$total" -gt "$n" ]; then
    echo "$(date '+%F %T') $3: fila tem $total, rodando $n (teto $MAX_LOTE) — $((total - n)) ficam pro proximo ciclo" >> "$AUDIT"
  fi
  local log="$LOGDIR/${3}_${HOJE}_${TS}.log"
  echo "$(date '+%F %T') $3: rodando $n -> $ids" >> "$AUDIT"
  python3 main.py processar $ids --ignorar-filtro-migracao --peticionar \
    --workers "$2" --parallel-tribunais >> "$log" 2>&1
  local prot; prot=$(grep -c 'CNJ ' "$log" 2>/dev/null | grep -oE '^[0-9]+' || echo 0)
  prot=$(grep -cE '→ CNJ' "$log" 2>/dev/null || echo 0)
  local ok; ok=$(grep -c 'marcado como CONCLUÍDO' "$log" 2>/dev/null || echo 0)
  CIC_DISP=$((CIC_DISP + n)); CIC_PROT=$((CIC_PROT + prot))
  echo "$(date '+%F %T') $3: fim — $n disponíveis, $prot protocolados, $ok finalizados (log: $log)" >> "$AUDIT"
}

# ================= FASE NOVOS =================
rm -f /tmp/run_novos.txt
if PYTHONPATH=. python3 scripts/check_novos.py > "$LOGDIR/novos_check_${TS}.txt" 2>&1; then
  run_lote /tmp/run_novos.txt "$WK" novos
else
  echo "$(date '+%F %T') novos: ERRO no check — pulando fase" >> "$AUDIT"
fi

# ================= FASE MIGRADOS =================
DESDE=$(date -v-3d +%F 2>/dev/null || echo "2026-06-28")
python3 consultar_migracao_mg.py --desde "$DESDE"               > "$LOGDIR/disc_mg_${TS}.txt" 2>&1 &
python3 consultar_migracao_sp.py --desde "$DESDE" --por-sessao 8 > "$LOGDIR/disc_sp_${TS}.txt" 2>&1 &
python3 consultar_migracao_rj.py --desde "$DESDE"               > "$LOGDIR/disc_rj_${TS}.txt" 2>&1 &
wait
rm -f /tmp/run_mig.txt
if PYTHONPATH=. python3 scripts/migrados_pendentes.py > "$LOGDIR/mig_check_${TS}.txt" 2>&1; then
  run_lote /tmp/run_mig.txt 2 migrados
else
  echo "$(date '+%F %T') migrados: ERRO no cross-ref — pulando fase" >> "$AUDIT"
fi

UFREP=$(PYTHONPATH=. python3 scripts/cycle_report.py "$LOGDIR/novos_${HOJE}_${TS}.log" "$LOGDIR/migrados_${HOJE}_${TS}.log" 2>/dev/null)
echo "$(date '+%F %T') 📊 CICLO RESUMO — disponíveis p/ protocolar: $CIC_DISP | realmente protocolados: $CIC_PROT | $UFREP" >> "$AUDIT"
python3 scripts/chrome_gc.py orphans >> "$AUDIT" 2>&1
echo "$(date '+%F %T') ciclo completo" >> "$AUDIT"
