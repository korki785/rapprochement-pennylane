#!/bin/bash
# Lancé par launchd chaque semaine.
# Rapprochement factures fournisseurs : Drive -> Qonto.
# --since fixe au 01/04/2026 : une facture ajoutée tardivement au Drive doit
# quand même être rapprochée ; processed.json évite le retraitement.

PROJECT="/Users/naeldarwish/rapprochement-pennylane"
LOG="$PROJECT/reports/fournisseurs/weekly_run.log"
PYTHON="/Users/naeldarwish/Library/Python/3.9/bin/python3"
[ -x "$PYTHON" ] || PYTHON="/usr/bin/python3"

mkdir -p "$PROJECT/reports/fournisseurs"

SINCE="2026-04-01"

echo "==============================" >> "$LOG"
echo "$(date '+%Y-%m-%d %H:%M:%S') — Lancement rapprochement fournisseurs (since $SINCE)" >> "$LOG"

cd "$PROJECT" && "$PYTHON" scripts/run_fournisseurs.py --since "$SINCE" >> "$LOG" 2>&1

echo "$(date '+%Y-%m-%d %H:%M:%S') — Terminé (code: $?)" >> "$LOG"
