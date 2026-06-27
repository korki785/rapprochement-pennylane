#!/usr/bin/env python3
"""Pré-vérification légère : y a-t-il de nouvelles transactions Qonto sans PJ ?

Utilisé par run_portals_watch.sh pour éviter de lancer Playwright à vide.
Exit 0 = au moins une nouvelle transaction, exit 1 = rien de nouveau.

Usage :
    python3 scripts/portals_has_new.py [--since YYYY-MM-DD] [--processed reports/portals/processed.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon.qonto_client import QontoClient, load_qonto_credentials  # noqa: E402
from recon.portals import detect_unreconciled, load_portal_vendors, match_vendor  # noqa: E402

DEFAULT_SINCE = "2026-04-01"


def main() -> int:
    parser = argparse.ArgumentParser(description="Détecte les nouvelles transactions sans PJ.")
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--processed", default="reports/portals/processed.json")
    args = parser.parse_args()

    processed_path = Path(args.processed)
    processed: dict = {}
    if processed_path.exists():
        try:
            processed = json.loads(processed_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    slug, key = load_qonto_credentials()
    client = QontoClient(slug, key)
    vendors = load_portal_vendors()

    unreconciled = detect_unreconciled(client, args.since)

    new_count = 0
    for tx in unreconciled:
        if tx.get("id") in processed:
            continue
        vendor = match_vendor(tx, vendors)
        if vendor:
            new_count += 1

    if new_count > 0:
        print(f"{new_count} nouvelle(s) transaction(s) sans PJ pour portails connus.", file=sys.stderr)
        return 0

    print("Aucune nouvelle transaction portail à traiter.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
