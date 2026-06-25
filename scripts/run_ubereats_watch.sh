#!/bin/bash
# Polling UberEats (lancé par launchd toutes les ~15 min).
# - Nouveau mail UberEats détecté -> télécharge le reçu + reconcile + attach.
# - Sinon -> retry léger reconcile+attach (rattrape les virements arrivés depuis).

PROJECT="/Users/naeldarwish/rapprochement-pennylane"
LOG="$PROJECT/reports/ubereats/watch.log"
PYTHON="/Users/naeldarwish/Library/Python/3.9/bin/python3"
[ -x "$PYTHON" ] || PYTHON="/usr/bin/python3"

mkdir -p "$PROJECT/reports/ubereats"
cd "$PROJECT" || exit 1

SINCE="2026-04-01"                       # reconcile large (rattrape virements tardifs)
FETCH_SINCE=$(date -v-10d +%Y-%m-%d)     # téléchargement : 10 derniers jours

echo "$(date '+%Y-%m-%d %H:%M:%S') — poll" >> "$LOG"

if "$PYTHON" scripts/ubereats_has_new_email.py >> "$LOG" 2>&1; then
    echo "  nouveau mail -> run complet" >> "$LOG"
    "$PYTHON" scripts/run_ubereats.py --since "$SINCE" --fetch-since "$FETCH_SINCE" >> "$LOG" 2>&1
    "$PYTHON" scripts/ubereats_has_new_email.py --mark >> "$LOG" 2>&1
else
    echo "  rien de neuf -> retry reconcile+attach" >> "$LOG"
    "$PYTHON" scripts/run_ubereats.py --since "$SINCE" --skip-fetch >> "$LOG" 2>&1
fi
