#!/usr/bin/env python3
"""Orchestrateur complet du rapprochement UberEats — une seule commande.

Enchaîne :
  1. fetch_ubereats_invoices.py  -> télécharge les PDF du portail UberEats
  2. QontoClient.fetch_reimbursement_transfers -> qonto_transfers.json
  3. reconcile_ubereats.py       -> matches.json + reconciliation_*.md
  4. QontoClient.upload_attachment -> attache chaque PDF à sa transaction Qonto

Usage :
    python3 scripts/run_ubereats.py [--since YYYY-MM-DD] [--out-dir DIR]
                                    [--headed] [--dry-run]

--dry-run : s'arrête après l'étape 3 (rapprochement), n'attache rien.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon.qonto_client import QontoClient, load_qonto_credentials  # noqa: E402

DEFAULT_SINCE = "2026-04-01"
SCRIPTS = ROOT / "scripts"


def _run(cmd: list[str], step: str) -> None:
    print(f"\n=== {step} ===", file=sys.stderr)
    result = subprocess.run([sys.executable, *cmd])
    if result.returncode != 0:
        raise SystemExit(f"Étape « {step} » a échoué (code {result.returncode}).")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rapprochement UberEats de bout en bout.")
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--out-dir", default="reports/ubereats")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="S'arrête après le rapprochement, n'attache rien sur Qonto.")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    input_dir = out_dir / "input"
    transfers_path = out_dir / "qonto_transfers.json"
    matches_path = out_dir / "matches.json"

    # --- 1. Télécharger les factures du portail UberEats ----------------------
    fetch_cmd = [
        str(SCRIPTS / "fetch_ubereats_invoices.py"),
        "--out-dir", str(input_dir),
        "--since", args.since,
    ]
    if args.headed:
        fetch_cmd.append("--headed")
    if args.dry_run:
        fetch_cmd.append("--dry-run")
    _run(fetch_cmd, "1/4 Téléchargement des factures UberEats")

    # --- 2. Récupérer les virements de remboursement Qonto --------------------
    print("\n=== 2/4 Virements Qonto ===", file=sys.stderr)
    slug, key = load_qonto_credentials()
    client = QontoClient(slug, key)
    transfers = client.fetch_reimbursement_transfers(since=args.since)
    out_dir.mkdir(parents=True, exist_ok=True)
    transfers_path.write_text(
        json.dumps(transfers, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"{len(transfers)} virement(s) -> {transfers_path}", file=sys.stderr)

    # --- 3. Rapprocher --------------------------------------------------------
    recon_cmd = [
        str(SCRIPTS / "reconcile_ubereats.py"),
        "--input", str(input_dir),
        "--transfers", str(transfers_path),
        "--out-dir", str(out_dir),
        "--since", args.since,
    ]
    _run(recon_cmd, "3/4 Rapprochement")

    if args.dry_run:
        print("\n[dry-run] Arrêt avant l'attachement Qonto.", file=sys.stderr)
        return 0

    # --- 4. Attacher les PDF aux transactions Qonto ---------------------------
    print("\n=== 4/4 Attachement sur Qonto ===", file=sys.stderr)
    if not matches_path.exists():
        print("Aucun matches.json — rien à attacher.", file=sys.stderr)
        return 0
    matches = json.loads(matches_path.read_text(encoding="utf-8"))
    ok = 0
    for m in matches:
        tx_id = m["qonto_transaction_id"]
        pdf = Path(m["invoice_path"])
        try:
            client.upload_attachment(tx_id, pdf)
            ok += 1
            print(f"  ✓ {pdf.name} -> {tx_id}", file=sys.stderr)
        except Exception as exc:
            print(f"  ✗ {pdf.name}: {exc}", file=sys.stderr)
    print(f"\n{ok}/{len(matches)} pièce(s) jointe(s) attachée(s) sur Qonto.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
