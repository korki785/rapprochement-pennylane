#!/usr/bin/env python3
"""Email hebdomadaire des justificatifs NON rapprochés (semaine écoulée).

Lit reports/fournisseurs/unreconciled.json (écrit par run_fournisseurs.py),
filtre les factures uploadées dans Drive durant les 7 derniers jours, et envoie
un digest texte à EMAIL_TO via Gmail SMTP (src/recon/mailer.py).

Usage :
    python3 scripts/email_unreconciled.py [--all] [--dry-run] [--days N] [--skip-if-empty]

--all          : ignore le filtre 7 jours (toutes les factures non rapprochées).
--days N        : fenêtre en jours (défaut 7).
--dry-run       : imprime le digest sans envoyer.
--skip-if-empty : n'envoie aucun email s'il n'y a rien à signaler.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon.mailer import send_email  # noqa: E402

DEFAULT_DAYS = 7
UNRECONCILED_PATH = ROOT / "reports" / "fournisseurs" / "unreconciled.json"


def _created_date(iso: str) -> str:
    """Renvoie la date ISO (YYYY-MM-DD) d'un createdTime Drive, sinon ""."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return iso[:10]


def _fmt_amount(amount: float, currency: str) -> str:
    if not amount:
        return "—"
    sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get((currency or "EUR").upper(), currency)
    return f"{amount:.2f} {sym}"


def load_items(window_days: int, take_all: bool) -> list:
    if not UNRECONCILED_PATH.exists():
        return []
    items = json.loads(UNRECONCILED_PATH.read_text(encoding="utf-8"))
    if take_all:
        return items
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    kept = []
    for it in items:
        created = _created_date(it.get("drive_created", ""))
        # Pas de date Drive connue -> on garde (mieux vaut signaler que rater).
        if not created or created >= cutoff:
            kept.append(it)
    return kept


def build_digest(items: list, window_days: int, take_all: bool) -> tuple[str, str]:
    monday = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    n = len(items)
    if take_all:
        subject = f"[Rapprochement] {n} justificatif(s) non rapproché(s) — total"
    else:
        subject = f"[Rapprochement] {n} justificatif(s) non rapproché(s) — semaine du {monday}"

    if n == 0:
        body = ("Bonjour,\n\n"
                "Aucun justificatif en attente de rapprochement cette semaine. "
                "Tout est à jour.\n\n— Rapprochement fournisseurs")
        return subject, body

    lines = ["Bonjour,", ""]
    scope = "au total" if take_all else f"uploadé(s) ces {window_days} derniers jours"
    lines.append(f"{n} justificatif(s) non rapproché(s) ({scope}) :")
    lines.append("")
    for it in sorted(items, key=lambda x: (x.get("date") or "", x.get("name", ""))):
        amt = _fmt_amount(it.get("amount", 0.0), it.get("currency", "EUR"))
        d = it.get("date") or "date ?"
        lines.append(f"• {it.get('name', '?')} — {amt} — {d} — {it.get('reason', '')}")
    lines.append("")
    lines.append("Ces justificatifs n'ont pas trouvé de transaction Qonto correspondante "
                 "(montant/date introuvable, payé hors Qonto, ou OCR illisible). "
                 "À traiter manuellement dans Qonto.")
    lines.append("")
    lines.append("— Rapprochement fournisseurs")
    return subject, "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Email des justificatifs non rapprochés.")
    parser.add_argument("--all", action="store_true",
                        help="Toutes les factures non rapprochées (ignore le filtre 7 j).")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-if-empty", action="store_true")
    args = parser.parse_args()

    items = load_items(args.days, args.all)
    subject, body = build_digest(items, args.days, args.all)

    if not items and args.skip_if_empty:
        print("Rien à signaler — email non envoyé (--skip-if-empty).", file=sys.stderr)
        return 0

    if args.dry_run:
        print(f"Sujet : {subject}\n", file=sys.stderr)
        print(body, file=sys.stderr)
        return 0

    report_md = None
    today = date.today().isoformat()
    md = UNRECONCILED_PATH.parent / f"reconciliation_{today}.md"
    if md.exists():
        report_md = [md]
    send_email(subject, body, attachments=report_md)
    print(f"Email envoyé : {subject}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
