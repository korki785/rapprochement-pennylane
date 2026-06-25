#!/bin/bash
# Polling continu (lancé par launchd toutes les ~15 min).
# Ne traite que les nouveaux PDF Drive (sortie rapide sinon).

PROJECT="/Users/naeldarwish/rapprochement-pennylane"
LOG="$PROJECT/reports/fournisseurs/watch.log"
PYTHON="/Users/naeldarwish/Library/Python/3.9/bin/python3"
[ -x "$PYTHON" ] || PYTHON="/usr/bin/python3"

mkdir -p "$PROJECT/reports/fournisseurs"

echo "$(date '+%Y-%m-%d %H:%M:%S') — poll" >> "$LOG"
cd "$PROJECT" && "$PYTHON" scripts/run_fournisseurs.py --since "2026-04-01" >> "$LOG" 2>&1
