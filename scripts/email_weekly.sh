#!/bin/bash
# Email lundi matin (lancé par launchd). Dédup hebdo type orfeo :
# n'envoie qu'une fois par semaine même si le Mac redémarre.

PROJECT="/Users/naeldarwish/rapprochement-pennylane"
LOG="$PROJECT/reports/fournisseurs/email.log"
PYTHON="/Users/naeldarwish/Library/Python/3.9/bin/python3"
[ -x "$PYTHON" ] || PYTHON="/usr/bin/python3"

mkdir -p "$PROJECT/reports/fournisseurs"

SEMAINE=$(date +%Y-%W)
F="$PROJECT/.derniere_semaine_email"
if [ -f "$F" ] && [ "$(cat "$F")" = "$SEMAINE" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') — déjà envoyé semaine $SEMAINE, skip" >> "$LOG"
    exit 0
fi

echo "==============================" >> "$LOG"
echo "$(date '+%Y-%m-%d %H:%M:%S') — email hebdo (semaine $SEMAINE)" >> "$LOG"

cd "$PROJECT" || exit 1
# 1. Rafraîchit l'état (rapproche ce qui peut l'être, réécrit unreconciled.json).
"$PYTHON" scripts/run_fournisseurs.py --since "2026-04-01" --force >> "$LOG" 2>&1
# 2. Envoie le digest des non-rapprochés de la semaine.
"$PYTHON" scripts/email_unreconciled.py >> "$LOG" 2>&1
RC=$?

if [ $RC -eq 0 ]; then
    echo "$SEMAINE" > "$F"
fi
echo "$(date '+%Y-%m-%d %H:%M:%S') — terminé (code: $RC)" >> "$LOG"
