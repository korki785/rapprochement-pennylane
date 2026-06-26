#!/usr/bin/env python3
"""Pré-check léger : y a-t-il un nouvel email de facture SaaS ? (exit 0 = oui, 1 = non)

Compare les Message-ID des emails fournisseurs récents à reports/saas/seen_emails.json.
Évite de lancer le run complet pour rien (poller 15 min). `--mark` acte les vus.

Usage :
    python3 scripts/saas_has_new_email.py [--days 14]   # exit 0 si nouveau
    python3 scripts/saas_has_new_email.py --mark         # marque tout comme vu
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_saas_gmail as fetch  # noqa: E402

SEEN_PATH = ROOT / "reports" / "saas" / "seen_emails.json"
DEFAULT_DAYS = 14


def _load_seen() -> set:
    if SEEN_PATH.exists():
        try:
            return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            return set()
    return set()


def _save_seen(seen: set) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Pré-check des nouveaux emails de factures SaaS.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--mark", action="store_true")
    args = parser.parse_args()

    since = (date.today() - timedelta(days=args.days)).isoformat()
    current = set(fetch.list_message_ids(since))
    seen = _load_seen()

    if args.mark:
        seen.update(current)
        _save_seen(seen)
        return 0

    new = current - seen
    if new:
        print(f"{len(new)} nouvel(aux) email(s) facture SaaS.", file=sys.stderr)
        return 0
    print("Aucun nouvel email facture SaaS.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
