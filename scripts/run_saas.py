#!/usr/bin/env python3
"""Orchestrateur du rapprochement des factures SaaS (email → Qonto) — une commande.

Enchaîne :
  1. Gmail (IMAP)            -> télécharge les PDF des factures fournisseurs SaaS
  2. fetch_all_debits        -> qonto_debits.json
  3. reconcile_saas (match)  -> matches.json + reconciliation_*.md + unreconciled.json
  4. upload_attachment       -> attache les PDF des rapprochements « exact » (skip si déjà attaché)
  5. processed.json          -> marque les Message-ID attachés (retire du manifest)

Idempotence par Message-ID : manifest.json = factures en attente ; processed.json = déjà
attachées. Les factures non rapprochées restent dans le manifest et sont re-tentées (le débit
peut se régler après réception du PDF).

Usage :
    python3 scripts/run_saas.py [--since YYYY-MM-DD] [--skip-fetch] [--dry-run]

--skip-fetch : saute Gmail (étape 1), re-tente reconcile+attach sur le manifest existant.
--dry-run    : s'arrête après le rapprochement (n'attache ni ne marque rien).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from recon.config import load_dotenv  # noqa: E402
from recon.qonto_client import QontoClient, load_qonto_credentials  # noqa: E402
import fetch_saas_gmail as fetch  # noqa: E402
import reconcile_saas as recon  # noqa: E402

DEFAULT_SINCE = "2026-04-01"


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def main() -> int:
    parser = argparse.ArgumentParser(description="Rapprochement factures SaaS de bout en bout.")
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--out-dir", default="reports/saas")
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    out_dir = Path(args.out_dir)
    input_dir = out_dir / "input"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    processed_path = out_dir / "processed.json"
    debits_path = out_dir / "qonto_debits.json"

    processed: Dict[str, dict] = _load_json(processed_path, {})
    manifest: Dict[str, dict] = _load_json(manifest_path, {})

    # --- 1/5 Téléchargement Gmail ---
    if args.skip_fetch:
        print("\n=== 1/5 Téléchargement sauté (--skip-fetch) ===", file=sys.stderr)
    else:
        print("\n=== 1/5 Téléchargement des factures SaaS (Gmail) ===", file=sys.stderr)
        # On ne re-télécharge ni les emails déjà attachés (processed) ni ceux déjà en
        # attente dans le manifest (déjà parsés) -> balayage incrémental.
        already = set(processed.keys()) | set(manifest.keys())
        new_entries = fetch.fetch_invoices(input_dir, args.since, already)
        for e in new_entries:
            manifest[e["msgid"]] = e
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{len(new_entries)} nouvelle(s) facture(s) ; {len(manifest)} en attente.", file=sys.stderr)

    if not manifest:
        print("Aucune facture en attente — sortie.", file=sys.stderr)
        return 0

    # --- 2/5 Débits Qonto ---
    print("\n=== 2/5 Débits Qonto ===", file=sys.stderr)
    slug, key = load_qonto_credentials()
    client = QontoClient(slug, key)
    debits = client.fetch_all_debits(since=args.since)
    debits_path.write_text(json.dumps(debits, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(debits)} débit(s) -> {debits_path.name}", file=sys.stderr)

    # --- 3/5 Rapprochement ---
    print("\n=== 3/5 Rapprochement ===", file=sys.stderr)
    matches, unmatched = recon.match(list(manifest.values()), debits,
                                     recon.DEFAULT_WARN_DAYS, recon.DEFAULT_WINDOW_DAYS)
    recon.write_matches_json(matches, out_dir / "matches.json")
    recon.write_unreconciled_json(unmatched, out_dir / "unreconciled.json")
    recon.write_report(matches, unmatched, out_dir / f"reconciliation_{datetime.now():%Y-%m-%d}.md")
    exact = [m for m in matches if m.confidence == "exact"]
    print(f"{len(matches)} rapprochée(s) ({len(exact)} fiable(s)), {len(unmatched)} non rapprochée(s).",
          file=sys.stderr)

    if args.dry_run:
        print("\n--dry-run : pas d'attache ni de marquage.", file=sys.stderr)
        return 0

    # --- 4/5 Attache sur Qonto (exact seulement) ---
    print("\n=== 4/5 Attachement sur Qonto ===", file=sys.stderr)
    warn = [m for m in matches if m.confidence != "exact"]
    for m in warn:
        print(f"  ⚠ {m.invoice.get('vendor')} {Path(m.invoice.get('primary_pdf','')).name} "
              f"-> {m.debit.get('label')} {m.debit.get('settled_at')} (à confirmer, non attaché)",
              file=sys.stderr)

    attached_msgids: set = set()
    for m in exact:
        tx_id = m.debit.get("id")
        msgids = m.invoice.get("msgids") or [m.invoice.get("msgid")]
        pdfs = [Path(p) for p in m.invoice.get("pdf_paths", [])]
        try:
            if client.get_transaction_attachments(tx_id):
                print(f"  → {m.invoice.get('vendor')} : transaction déjà documentée, ignorée", file=sys.stderr)
                attached_msgids.update(msgids)
                continue
        except Exception as exc:
            print(f"  ⚠ vérif PJ impossible ({exc}), tentative d'attache", file=sys.stderr)
        ok = False
        for pdf in pdfs:
            if not pdf.exists():
                continue
            try:
                client.upload_attachment(tx_id, pdf)
                print(f"  ✓ {pdf.name} -> {tx_id} ({m.debit.get('label')})", file=sys.stderr)
                ok = True
            except Exception as exc:
                print(f"  ✗ {pdf.name} : {exc}", file=sys.stderr)
        if ok:
            attached_msgids.update(msgids)

    # --- 5/5 Marquage ---
    print("\n=== 5/5 Marquage des factures traitées ===", file=sys.stderr)
    today = datetime.now().strftime("%Y-%m-%d")
    for msgid in attached_msgids:
        entry = manifest.get(msgid, {})
        processed[msgid] = {"vendor": entry.get("vendor", ""),
                            "name": Path(entry.get("primary_pdf", "")).name,
                            "processed_at": today}
        manifest.pop(msgid, None)
    processed_path.write_text(json.dumps(processed, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(attached_msgids)} facture(s) attachée(s) et marquée(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
