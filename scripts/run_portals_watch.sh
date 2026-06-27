#!/bin/bash
# Flux 5 — Watcher portails : déclenché toutes les 15 min par launchd.
# Lance run_portals.py uniquement si de nouvelles transactions sans PJ sont détectées.
set -euo pipefail

cd /Users/naeldarwish/rapprochement-pennylane
PYTHON=/Users/naeldarwish/Library/Python/3.9/bin/python3

if ! $PYTHON scripts/portals_has_new.py --since 2026-04-01; then
    exit 0
fi

$PYTHON scripts/run_portals.py --since 2026-04-01
