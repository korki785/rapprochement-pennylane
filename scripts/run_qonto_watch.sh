#!/bin/bash
# Polling rapprochement PILOTÉ PAR LA TRANSACTION (lancé par launchd, ~horaire).
# Pour chaque transaction Qonto sans justificatif : cherche le reçu dans Gmail par
# (nom du libellé + montant), vérifie, attache. Aucun fournisseur pré-enregistré requis.
# Cadence plus lente que les autres pollers : parcourt TOUTES les tx non rapprochées × IMAP
# (pas de pré-check email bon marché possible, le déclencheur est côté transactions).

PROJECT="/Users/naeldarwish/rapprochement-pennylane"
LOG="$PROJECT/reports/qonto/watch.log"
PYTHON="/Users/naeldarwish/Library/Python/3.9/bin/python3"
[ -x "$PYTHON" ] || PYTHON="/usr/bin/python3"

mkdir -p "$PROJECT/reports/qonto"
cd "$PROJECT" || exit 1

SINCE="2026-04-01"

echo "$(date '+%Y-%m-%d %H:%M:%S') — poll" >> "$LOG"
"$PYTHON" scripts/reconcile_qonto.py --since "$SINCE" >> "$LOG" 2>&1
