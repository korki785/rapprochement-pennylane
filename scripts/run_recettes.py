#!/usr/bin/env python3
"""Orchestrateur du rapprochement RECETTES — factures clients ↔ virements Qonto.

Sens inverse des dépenses : quand un client paie une facture par virement, on
attache le PDF de la facture à la transaction créditrice Qonto (justificatif de
la recette). Enchaîne :
  1. QontoClient.fetch_all_credits    -> qonto_credits.json (virements entrants)
  2. QontoClient.list_client_invoices -> client_invoices.json (factures émises)
  3. reconcile_recettes (match)       -> matches.json + reconciliation_*.md
  4. Pour chaque match auto (exact/swift) : télécharge le PDF de la facture
     (get_attachment_url -> download_url) et l'attache à la transaction
     (upload_attachment) — sauté si la transaction a déjà une PJ (idempotent).
  5. processed.json -> marque les transactions rapprochées (clé = tx_id).

Usage :
    python3 scripts/run_recettes.py [--since YYYY-MM-DD] [--out-dir DIR] [--dry-run]

--dry-run : s'arrête après l'étape 3, n'attache ni ne marque rien.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from recon.config import load_dotenv  # noqa: E402
from recon.qonto_client import QontoClient, load_qonto_credentials  # noqa: E402
import reconcile_recettes as recon  # noqa: E402

DEFAULT_SINCE = "2026-04-01"


def _load_processed(path: Path) -> Dict[str, dict]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def step4_attach(client: QontoClient, matches: List[Dict]) -> set:
    """Attache le PDF de la facture à chaque transaction créditrice rapprochée.

    N'auto-attache QUE les rapprochements sûrs (confidence ∈ AUTO_ATTACH =
    {exact, swift}). Les « warn » (écart de montant non expliqué, accouplement
    ambigu) sont seulement listés → confirmation humaine (voir rapport markdown).
    Idempotent : sauté si la transaction a déjà une pièce jointe.

    Renvoie l'ensemble des tx_id effectivement traités (attaché ou déjà présent).
    """
    done: set = set()
    warn = [m for m in matches if m.get("confidence") not in recon.AUTO_ATTACH]
    if warn:
        print(f"  {len(warn)} rapprochement(s) à confirmer (non attaché auto) :", file=sys.stderr)
        for m in warn:
            print(f"    ⚠ {m.get('invoice_number', '?')} {m.get('invoice_client', '')} "
                  f"[{m.get('match_strategy', '?')}] -> {m['qonto_date']} (voir rapport)",
                  file=sys.stderr)

    for m in matches:
        if m.get("confidence") not in recon.AUTO_ATTACH:
            continue
        tx_id = m["qonto_transaction_id"]
        att_id = m.get("invoice_attachment_id", "")
        number = m.get("invoice_number", "facture")
        if not att_id:
            print(f"  ✗ {number} : pas d'attachment_id, ignorée", file=sys.stderr)
            continue
        try:
            existing = client.get_transaction_attachments(tx_id)
            if existing:
                print(f"  → {number} : transaction déjà documentée, ignorée", file=sys.stderr)
                done.add(tx_id)
                continue
        except Exception as exc:
            print(f"  ⚠ {number} : vérif PJ impossible ({exc}), tentative d'attache", file=sys.stderr)

        # URL présignée (~30 min) : télécharger + attacher dans la foulée.
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / f"{number}.pdf"
            try:
                url = client.get_attachment_url(att_id)
                if not url:
                    raise RuntimeError("URL de la facture vide")
                client.download_url(url, dest)
                client.upload_attachment(tx_id, dest)
                print(f"  ✓ {number} -> {tx_id}", file=sys.stderr)
                done.add(tx_id)
            except Exception as exc:
                print(f"  ✗ {number} : {exc}", file=sys.stderr)
    return done


def step5_mark(processed_path: Path, done_ids: set, matches: List[Dict]) -> None:
    processed = _load_processed(processed_path)
    today = datetime.now().strftime("%Y-%m-%d")
    by_tx = {m["qonto_transaction_id"]: m for m in matches}
    for tx_id in done_ids:
        m = by_tx.get(tx_id, {})
        processed[tx_id] = {
            "invoice_number": m.get("invoice_number", ""),
            "client": m.get("invoice_client", ""),
            "processed_at": today,
        }
    processed_path.parent.mkdir(parents=True, exist_ok=True)
    processed_path.write_text(
        json.dumps(processed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"{len(done_ids)} virement(s) marqué(s) dans {processed_path.name}.", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Rapprochement recettes de bout en bout.")
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--out-dir", default="reports/recettes")
    parser.add_argument("--dry-run", action="store_true",
                        help="S'arrête après le rapprochement, n'attache ni ne marque rien.")
    args = parser.parse_args()

    load_dotenv()
    out_dir = Path(args.out_dir)
    processed_path = out_dir / "processed.json"
    credits_path = out_dir / "qonto_credits.json"
    invoices_path = out_dir / "client_invoices.json"
    out_dir.mkdir(parents=True, exist_ok=True)

    slug, key = load_qonto_credentials()
    client = QontoClient(slug, key)

    # --- 1. Virements créditeurs -------------------------------------------
    print("\n=== 1/5 Virements créditeurs Qonto ===", file=sys.stderr)
    credits_raw = client.fetch_all_credits(since=args.since)
    credits_path.write_text(
        json.dumps(credits_raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"{len(credits_raw)} crédit(s) depuis {args.since} -> {credits_path}", file=sys.stderr)

    # --- 2. Factures clients ------------------------------------------------
    print("\n=== 2/5 Factures clients Qonto ===", file=sys.stderr)
    invoices_raw = client.list_client_invoices()
    invoices_path.write_text(
        json.dumps(invoices_raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"{len(invoices_raw)} facture(s) client -> {invoices_path}", file=sys.stderr)

    # --- 3. Rapprochement ---------------------------------------------------
    print("\n=== 3/5 Rapprochement ===", file=sys.stderr)
    invoices = recon.load_client_invoices(invoices_path)
    credits = [t for t in recon.load_qonto_credits(credits_path)
               if not t.settled_at or t.settled_at >= args.since]
    report = recon.match(invoices, credits)
    report.run_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    today = report.run_date[:10]
    recon.write_matches_json(report, out_dir / "matches.json")
    recon.write_reconciliation_report(report, out_dir / f"reconciliation_{today}.md")
    recon.print_summary(report)

    if args.dry_run:
        print("\n[dry-run] Arrêt avant l'attachement Qonto.", file=sys.stderr)
        return 0

    # --- 4. Attachement -----------------------------------------------------
    print("\n=== 4/5 Attachement des factures sur Qonto ===", file=sys.stderr)
    matches = json.loads((out_dir / "matches.json").read_text(encoding="utf-8"))
    if not matches:
        print("Aucun rapprochement — rien à attacher.", file=sys.stderr)
        return 0
    done_ids = step4_attach(client, matches)

    # --- 5. Marquer les traités --------------------------------------------
    print("\n=== 5/5 Marquage des virements traités ===", file=sys.stderr)
    step5_mark(processed_path, done_ids, matches)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
