#!/usr/bin/env python3
"""Email récap hebdo FIABLE des transactions sans justificatif (transaction-centric).

Lit reports/recap/confirmed.json (écrit par audit_unreconciled.py, après vérification
adversariale + éventuelle escalade Claude). N'envoie QUE des items CONFIRMÉS « vraiment
sans justificatif ». Joint le rapport d'audit du jour.

Si reports/recap/needs_review.json (items que Claude n'a pas pu trancher) est non vide,
on N'ENVOIE PAS le récap : on envoie une alerte courte « à vérifier » à la place.

Usage :
    python3 scripts/email_recap.py [--dry-run] [--skip-if-empty]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon.mailer import send_email  # noqa: E402

RECAP_DIR = ROOT / "reports" / "recap"
CONFIRMED = RECAP_DIR / "confirmed.json"
NEEDS_REVIEW = RECAP_DIR / "needs_review.json"


def _load(path: Path) -> list:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []


def _fmt(amount, currency: str) -> str:
    if not amount:
        return "—"
    sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get((currency or "EUR").upper(), currency)
    return f"{float(amount):.2f} {sym}"


def build_recap(items: list) -> tuple:
    monday = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    n = len(items)
    subject = f"[Récap] {n} transaction(s) sans justificatif — semaine du {monday}"
    if n == 0:
        body = ("Bonjour,\n\nToutes les dépenses de la semaine ont un justificatif. "
                "Rien à signaler (vérifié sur le statut live Qonto + recherche des justificatifs).\n\n"
                "— Récap rapprochement")
        return subject, body

    lines = ["Bonjour,", "",
             f"{n} transaction(s) sans justificatif cette semaine "
             "(vérifié live Qonto + recherche approfondie d'un justificatif avant envoi) :", ""]
    for it in sorted(items, key=lambda x: (x.get("date") or "", x.get("label", ""))):
        amt = _fmt(it.get("amount"), it.get("currency", "EUR"))
        d = it.get("date") or "date ?"
        op = it.get("operation_type", "")
        lines.append(f"• {it.get('label', '?')} — {amt} — {d} — {op}")
    lines += ["",
              "Ces transactions n'ont AUCUN justificatif trouvable (ni portail, ni email, ni Drive). "
              "À traiter manuellement dans Qonto.", "", "— Récap rapprochement"]
    return subject, "\n".join(lines)


def build_alert(needs: list) -> tuple:
    n = len(needs)
    subject = f"[Récap] EN ATTENTE — {n} item(s) à vérifier avant envoi"
    lines = ["Bonjour,", "",
             f"Le récap hebdo est EN ATTENTE : {n} transaction(s) pour lesquelles un justificatif "
             "existe peut-être mais n'a pas pu être rattaché ni tranché automatiquement :", ""]
    for it in needs:
        amt = _fmt(it.get("amount"), it.get("currency", "EUR"))
        lines.append(f"• {it.get('label', '?')} — {amt} — {it.get('date', '?')} "
                     f"— {it.get('found_in', it.get('reason', ''))}")
    lines += ["",
              "Le récap fiable ne sera envoyé qu'une fois ces points levés (pour ne jamais "
              "t'envoyer un récap potentiellement faux).", "", "— Récap rapprochement"]
    return subject, "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Email récap fiable des tx sans justificatif.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-if-empty", action="store_true")
    args = ap.parse_args()

    needs = _load(NEEDS_REVIEW)
    if needs:
        subject, body = build_alert(needs)
    else:
        items = _load(CONFIRMED)
        if not items and args.skip_if_empty:
            print("Rien à signaler — email non envoyé (--skip-if-empty).", file=sys.stderr)
            return 0
        subject, body = build_recap(items)

    if args.dry_run:
        print(f"Sujet : {subject}\n\n{body}", file=sys.stderr)
        return 0

    today = date.today().isoformat()
    audit_md = RECAP_DIR / f"audit_{today}.md"
    attachments = [audit_md] if (audit_md.exists() and not needs) else None
    send_email(subject, body, attachments=attachments)
    print(f"Email envoyé : {subject}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
