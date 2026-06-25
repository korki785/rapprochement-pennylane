#!/usr/bin/env python3
"""Pré-check léger : y a-t-il un NOUVEAU mail UberEats non encore traité ?

Connexion IMAP seule (pas de Playwright). Compare les Message-ID des emails
UberEats récents à reports/ubereats/seen_emails.json.

Codes de sortie :
    0 = au moins un mail UberEats non vu  -> le watcher lance le téléchargement.
    1 = rien de nouveau.

Usage :
    python3 scripts/ubereats_has_new_email.py [--days N]
    python3 scripts/ubereats_has_new_email.py --mark   # marque tout comme vu
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from fetch_ubereats_gmail import load_credentials, connect_imap  # noqa: E402

SEEN_PATH = ROOT / "reports" / "ubereats" / "seen_emails.json"
DEFAULT_DAYS = 3


def _load_seen() -> set:
    if SEEN_PATH.exists():
        try:
            return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            return set()
    return set()


def _save_seen(seen: set) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2),
                         encoding="utf-8")


def _message_ids(days: int) -> list:
    """Message-ID des emails UberEats des `days` derniers jours."""
    gmail, password = load_credentials()
    mail = connect_imap(gmail, password)
    since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
    _, data = mail.search(None, f'FROM "uber" SINCE "{since}"')
    ids = data[0].split() if data and data[0] else []
    out = []
    for mid in ids:
        _, d = mail.fetch(mid, "(BODY[HEADER.FIELDS (MESSAGE-ID)])")
        raw = b""
        for part in d:
            if isinstance(part, tuple):
                raw += part[1] or b""
        m = re.search(rb"<[^>]+>", raw)
        out.append(m.group(0).decode("utf-8", "replace") if m else mid.decode())
    mail.logout()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Détecte un nouveau mail UberEats.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--mark", action="store_true",
                        help="Marque tous les mails récents comme vus (après traitement).")
    args = parser.parse_args()

    current = _message_ids(args.days)
    seen = _load_seen()

    if args.mark:
        seen.update(current)
        _save_seen(seen)
        print(f"{len(current)} mail(s) marqué(s) vu(s).", file=sys.stderr)
        return 0

    new = [mid for mid in current if mid not in seen]
    if new:
        print(f"{len(new)} nouveau(x) mail(s) UberEats.", file=sys.stderr)
        return 0
    print("Aucun nouveau mail UberEats.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
