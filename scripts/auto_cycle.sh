#!/bin/bash
# Um ciclo autônomo: FASE NOVOS -> FASE MIGRADOS (descoberta + execução).
# Chamado em loop pelo driver. Travas anti-duplo-protocolo:
#  - guard de data (só no dia de teste)
#  - lock por diretório (não sobrepõe dois ciclos)
#  - pula se já houver 'main.py processar' rodando (manual ou outro)
#  - listas em /tmp são limpas antes de cada check (nunca reusa lista velha)
#  - check_novos/migrados_pendentes só pegam status 1/10 (em execução/concluído ficam fora)

set -u
DIA_TESTE="2026-09-01"
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
TS=$(date +%H%M%S)

CIC_DISP=0   # total disponíveis p/ protocolar no ciclo (novos + migrados)
CIC_PROT=0   # total realmente protocolados no eproc

run_lote() {  # $1=arquivo-de-ids  $2=workers  $3=label
  local ids; ids=$(cat "$1" 2>/dev/null)
  if [ -z "${ids// }" ]; then echo "$(date '+%F %T') $3: nada a rodar" >> "$AUDIT"; return; fi
  local n; n=$(echo $ids | wc -w | tr -d ' ')
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
  run_lote /tmp/run_novos.txt 3 novos
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
echo "$(date '+%F %T') ciclo completo" >> "$AUDIT"
