#!/bin/bash
# Polling continu des recettes (lancé par launchd toutes les ~15 min).
# Attache le PDF de la facture dès qu'un virement client arrive sur Qonto.

PROJECT="/Users/naeldarwish/rapprochement-pennylane"
LOG="$PROJECT/reports/recettes/watch.log"
PYTHON="/Users/naeldarwish/Library/Python/3.9/bin/python3"
[ -x "$PYTHON" ] || PYTHON="/usr/bin/python3"

mkdir -p "$PROJECT/reports/recettes"

echo "$(date '+%Y-%m-%d %H:%M:%S') — poll" >> "$LOG"
cd "$PROJECT" && "$PYTHON" scripts/run_recettes.py --since "2026-04-01" >> "$LOG" 2>&1
