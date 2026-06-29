#!/bin/bash
# Loop autônomo: a cada 30 min, lista aptos novos e roda com 3 workers.
# (bash, não zsh: zsh não faz word-splitting de $IDS sem aspas e quebra o processar)
# - Roda SOMENTE hoje (guard de data) — teste de 1 dia.
# - Lock por mkdir (atômico, portável no macOS) impede duas execuções sobrepostas.
# - check_novos só pega status 1/10 fora da blacklist; itens em execução (11) ou
#   concluídos (9) ficam de fora -> sem risco de duplo protocolo.
# - NÃO mexe em status 11 (não reseta falhas) pra nunca colidir com run em voo.

set -u
DIA_TESTE="2026-06-29"
REPO="/Users/samuelferreira/Documents/rpa-protocolo"
LOGDIR="$REPO/data/auto_logs"
LOCKDIR="/tmp/rpa_auto.lockdir"

export PATH="/Library/Frameworks/Python.framework/Versions/3.13/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

HOJE=$(date +%F)
mkdir -p "$LOGDIR"
AUDIT="$LOGDIR/auto.log"

# guard: só roda no dia do teste
if [ "$HOJE" != "$DIA_TESTE" ]; then
  echo "$(date '+%F %T') skip — fora do dia de teste ($DIA_TESTE)" >> "$AUDIT"
  exit 0
fi

# lock atômico — se já tem instância rodando, pula este tick
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  echo "$(date '+%F %T') skip — já há execução em andamento" >> "$AUDIT"
  exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT

# trava extra: se QUALQUER 'main.py processar' já estiver rodando (manual ou outro),
# pula este tick — blindagem contra duplo protocolo de um mesmo CodItem.
if pgrep -f "main.py processar" >/dev/null 2>&1; then
  echo "$(date '+%F %T') skip — já há 'main.py processar' em execução" >> "$AUDIT"
  exit 0
fi

cd "$REPO" || { echo "$(date '+%F %T') ERRO cd repo" >> "$AUDIT"; exit 1; }
TS=$(date +%H%M%S)
RUNLOG="$LOGDIR/run_${HOJE}_${TS}.log"

# 1) lista novos (grava /tmp/run_novos.txt). Limpa antes pra NUNCA reusar lista
#    antiga caso o check falhe (evita reprocessar itens errados).
rm -f /tmp/run_novos.txt
PYTHONPATH=. python3 scripts/check_novos.py > "$LOGDIR/novos_${TS}.txt" 2>&1
if [ $? -ne 0 ]; then
  echo "$(date '+%F %T') ERRO no check_novos — abortando este tick (ver novos_${TS}.txt)" >> "$AUDIT"
  exit 1
fi
IDS=$(cat /tmp/run_novos.txt 2>/dev/null)

if [ -z "${IDS// }" ]; then
  echo "$(date '+%F %T') sem novos" >> "$AUDIT"
  exit 0
fi

N=$(echo $IDS | wc -w | tr -d ' ')
echo "$(date '+%F %T') rodando $N novo(s): $IDS" >> "$AUDIT"

# 2) processa com 3 workers, paralelo por tribunal
python3 main.py processar $IDS --ignorar-filtro-migracao --peticionar \
  --workers 3 --parallel-tribunais >> "$RUNLOG" 2>&1

OKS=$(grep -c 'marcado como CONCLUÍDO' "$RUNLOG" 2>/dev/null || echo 0)
echo "$(date '+%F %T') fim — $N enviados, $OKS finalizados (log: $RUNLOG)" >> "$AUDIT"
