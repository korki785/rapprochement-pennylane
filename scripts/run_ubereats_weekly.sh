#!/bin/bash
# Lancé par launchd chaque lundi matin.
# Calcule la date du lundi courant et lance le rapprochement UberEats.

PROJECT="/Users/naeldarwish/rapprochement-pennylane"
LOG="$PROJECT/reports/ubereats/weekly_run.log"
PYTHON="/Users/naeldarwish/Library/Python/3.9/bin/python3"
[ -x "$PYTHON" ] || PYTHON="/usr/bin/python3"

mkdir -p "$PROJECT/reports/ubereats"

# Date du lundi de cette semaine (ou il y a 7 jours pour attraper la semaine passée).
SINCE=$(date -v-7d +%Y-%m-%d)

echo "==============================" >> "$LOG"
echo "$(date '+%Y-%m-%d %H:%M:%S') — Lancement rapprochement UberEats (since $SINCE)" >> "$LOG"

cd "$PROJECT" && "$PYTHON" scripts/run_ubereats.py --since "$SINCE" >> "$LOG" 2>&1

echo "$(date '+%Y-%m-%d %H:%M:%S') — Terminé (code: $?)" >> "$LOG"
