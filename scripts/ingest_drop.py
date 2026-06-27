#!/usr/bin/env python3
"""Fallback MANUEL : rapproche + attache des PDF déposés à la main.

Pour les portails NON scrapables automatiquement (CAPTCHA Cloudflare type OpenAI/ChatGPT,
Airbnb, Turo… ou tout portail récalcitrant) : on télécharge le reçu/la facture à la main UNE
fois, on le dépose dans un dossier, et ce script fait le reste (parse + match + attache).

Dépôt :  reports/portals/_drop/<handler_key>/<nom>.pdf
  <handler_key> = clé du portail dans portal_vendors.csv (openai, airbnb, turo, hunter, …).
  Le sous-dossier donne le vendeur → croisement fiable nom marchand ↔ libellé Qonto.

Chaque PDF est parsé (montant/devise/date via recon.saas.parse_pdf), rapproché au débit Qonto
sans PJ (montant ±0.01 sur EUR `amount` OU devise `local_amount`, + date dans la fenêtre,
+ alias du vendeur présent dans le libellé) et attaché si « exact ». Idempotent (processed.json).

Usage :
    python3 scripts/ingest_drop.py [--drop-dir reports/portals/_drop] [--since YYYY-MM-DD] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from recon.qonto_client import QontoClient, load_qonto_credentials  # noqa: E402
from recon.portals import detect_unreconciled, load_portal_vendors  # noqa: E402
from recon import saas  # noqa: E402
import reconcile_portals as recon  # noqa: E402

DEFAULT_SINCE = "2026-04-01"


def main() -> int:
    ap = argparse.ArgumentParser(description="Rapproche + attache des PDF déposés à la main.")
    ap.add_argument("--drop-dir", default="reports/portals/_drop")
    ap.add_argument("--since", default=DEFAULT_SINCE)
    ap.add_argument("--out-dir", default="reports/portals")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    drop = Path(args.drop_dir)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    vendors = load_portal_vendors()
    keys = sorted({v["handler_key"] for v in vendors})

    if not drop.exists():
        drop.mkdir(parents=True, exist_ok=True)

    # 1. Collecte des PDF déposés, par sous-dossier = handler_key.
    invoices = []
    for sub in sorted(p for p in drop.iterdir() if p.is_dir()):
        if sub.name not in keys:
            print(f"  ⚠ sous-dossier « {sub.name} » inconnu (pas un portail) — ignoré.",
                  file=sys.stderr)
            continue
        for pdf in sorted(sub.glob("*.pdf")):
            pp = saas.parse_pdf(pdf)
            if pp.amount is None or not pp.date:
                print(f"  ⚠ {sub.name}/{pdf.name} : montant/date illisible "
                      f"(amount={pp.amount}, date={pp.date}) — ignoré.", file=sys.stderr)
                continue
            invoices.append({
                "vendor": sub.name, "invoice_id": pdf.stem,
                "amount": pp.amount, "currency": pp.currency,
                "date": pp.date, "pdf_path": str(pdf),
            })
            print(f"  · [{sub.name}] {pdf.name}  {pp.date}  {pp.amount} {pp.currency}",
                  file=sys.stderr)

    if not invoices:
        print(f"\nAucun PDF exploitable dans {drop}.\n"
              f"Déposez les reçus dans un sous-dossier par portail, ex. {drop}/openai/recu.pdf\n"
              f"Portails connus : {', '.join(keys)}", file=sys.stderr)
        return 0

    # 2. Backlog Qonto (débits sans PJ), hors déjà traités.
    slug, secret = load_qonto_credentials()
    client = QontoClient(slug, secret)
    processed_path = out / "processed.json"
    processed = {}
    if processed_path.exists():
        try:
            processed = json.loads(processed_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    backlog = [tx for tx in detect_unreconciled(client, args.since)
               if tx.get("id") not in processed]

    # 3. Rapprochement (fenêtre large : dépôt manuel = date du reçu parfois éloignée du débit).
    matches, unmatched = recon.reconcile(invoices, backlog, vendors,
                                         window_days=15, warn_days=15)
    exact = [m for m in matches if m.confidence == "exact"]
    print(f"\n{len(matches)} rapprochée(s) ({len(exact)} exacte(s)), "
          f"{len(unmatched)} non rapprochée(s).", file=sys.stderr)
    for m in matches:
        if m.confidence != "exact":
            print(f"  ⚠ {m.invoice['vendor']} {Path(m.invoice['pdf_path']).name} "
                  f"→ {m.debit['label']} (warn, non attaché)", file=sys.stderr)
    for inv in unmatched:
        print(f"  ✗ {inv.get('vendor')} {Path(inv.get('pdf_path','')).name} : "
              f"{inv.get('_reason','non rapprochée')}", file=sys.stderr)

    if args.dry_run:
        print("\n--dry-run : rien attaché.", file=sys.stderr)
        return 0

    # 4. Attache des « exact » (idempotent).
    ok = 0
    today = datetime.now().strftime("%Y-%m-%d")
    for m in exact:
        tx_id = m.debit["id"]
        pdf = Path(m.invoice["pdf_path"])
        try:
            if client.get_transaction_attachments(tx_id):
                processed[tx_id] = {"vendor": m.invoice["vendor"], "pdf": pdf.name,
                                    "attached_at": today}
                continue
        except Exception:
            pass
        try:
            client.upload_attachment(tx_id, pdf)
            print(f"  ✓ {pdf.name} → {m.debit['label']} ({m.debit['settled_at']})",
                  file=sys.stderr)
            processed[tx_id] = {"vendor": m.invoice["vendor"], "pdf": pdf.name,
                                "attached_at": today}
            ok += 1
        except Exception as exc:
            print(f"  ✗ {pdf.name} : {exc}", file=sys.stderr)

    processed_path.write_text(json.dumps(processed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{ok} attachée(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
