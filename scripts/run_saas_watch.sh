#!/bin/bash
# Polling factures SaaS (lancé par launchd toutes les ~15 min).
# - Nouvel email fournisseur détecté -> télécharge le PDF + reconcile + attach.
# - Sinon -> retry léger reconcile+attach (rattrape les débits réglés depuis).

PROJECT="/Users/naeldarwish/rapprochement-pennylane"
LOG="$PROJECT/reports/saas/watch.log"
PYTHON="/Users/naeldarwish/Library/Python/3.9/bin/python3"
[ -x "$PYTHON" ] || PYTHON="/usr/bin/python3"

mkdir -p "$PROJECT/reports/saas"
cd "$PROJECT" || exit 1

SINCE="2026-04-01"                       # reconcile large (rattrape débits tardifs)

echo "$(date '+%Y-%m-%d %H:%M:%S') — poll" >> "$LOG"

if "$PYTHON" scripts/saas_has_new_email.py >> "$LOG" 2>&1; then
    echo "  nouvel email -> run complet" >> "$LOG"
    "$PYTHON" scripts/run_saas.py --since "$SINCE" >> "$LOG" 2>&1
    "$PYTHON" scripts/saas_has_new_email.py --mark >> "$LOG" 2>&1
else
    echo "  rien de neuf -> retry reconcile+attach" >> "$LOG"
    "$PYTHON" scripts/run_saas.py --since "$SINCE" --skip-fetch >> "$LOG" 2>&1
fi
