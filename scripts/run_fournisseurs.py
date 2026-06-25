#!/usr/bin/env python3
"""Orchestrateur du rapprochement factures fournisseurs — une seule commande.

Enchaîne :
  1. Google Drive  -> télécharge les PDF nouveaux du dossier « Factures fournisseurs »
  2. QontoClient.fetch_all_debits -> qonto_debits.json
  3. reconcile_fournisseurs (match) -> matches.json + reconciliation_*.md
  4. QontoClient.upload_attachment  -> attache chaque PDF (skip si déjà attaché)
  5. processed.json -> marque les Drive file IDs rapprochés (évite le retraitement)

Usage :
    python3 scripts/run_fournisseurs.py [--since YYYY-MM-DD] [--out-dir DIR]
                                        [--auth] [--dry-run]

--auth    : ouvre le navigateur pour la 1re autorisation Google Drive (une fois).
--dry-run : s'arrête après l'étape 3 (rapprochement), n'attache ni ne marque rien.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from recon.config import load_dotenv  # noqa: E402
from recon.qonto_client import QontoClient, load_qonto_credentials  # noqa: E402
from recon.drive_client import DriveClient  # noqa: E402
import reconcile_fournisseurs as recon  # noqa: E402

DEFAULT_SINCE = "2026-04-01"
DEFAULT_FOLDER = "Factures fournisseurs"


def _safe_filename(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in " ._-" else "_" for c in name)
    keep = keep.strip() or "facture"
    return keep if keep.lower().endswith(".pdf") else keep + ".pdf"


def _load_processed(path: Path) -> Dict[str, dict]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


# --- Étape 1 : Drive -------------------------------------------------------
def step1_fetch_drive(client: DriveClient, folder_name: str, input_dir: Path,
                      processed_path: Path) -> List[Dict]:
    """Télécharge les PDF non encore traités. Renvoie [{id, name, local_path}]."""
    processed = _load_processed(processed_path)
    folder_id = client.find_folder_id(folder_name)
    pdfs = client.list_pdfs(folder_id)
    new = [f for f in pdfs if f["id"] not in processed]
    print(f"{len(pdfs)} PDF dans Drive, {len(new)} nouveau(x).", file=sys.stderr)

    input_dir.mkdir(parents=True, exist_ok=True)
    out: List[Dict] = []
    for f in new:
        dest = input_dir / _safe_filename(f["name"])
        try:
            client.download_pdf(f["id"], dest)
            out.append({"id": f["id"], "name": f["name"], "local_path": str(dest)})
            print(f"  ✓ {dest.name}", file=sys.stderr)
        except Exception as exc:
            print(f"  ✗ {f['name']} : {exc}", file=sys.stderr)
    return out


# --- Étape 4 : upload ------------------------------------------------------
def step4_upload(client: QontoClient, matches: List[Dict]) -> set:
    """Attache chaque PDF si la transaction n'a pas déjà de pièce jointe.

    Renvoie l'ensemble des drive_file_id effectivement traités (attaché ou déjà présent).
    """
    done: set = set()
    for m in matches:
        tx_id = m["qonto_transaction_id"]
        pdf = Path(m["invoice_path"])
        drive_id = m.get("drive_file_id", "")
        try:
            existing = client.get_transaction_attachments(tx_id)
            if existing:
                print(f"  → {pdf.name} : transaction déjà documentée, ignorée", file=sys.stderr)
                done.add(drive_id)
                continue
        except Exception as exc:
            print(f"  ⚠ {pdf.name} : vérif PJ impossible ({exc}), tentative d'attache", file=sys.stderr)
        try:
            client.upload_attachment(tx_id, pdf)
            print(f"  ✓ {pdf.name} -> {tx_id}", file=sys.stderr)
            done.add(drive_id)
        except Exception as exc:
            print(f"  ✗ {pdf.name} : {exc}", file=sys.stderr)
    return done


# --- Étape 5 : marquer -----------------------------------------------------
def step5_mark(processed_path: Path, done_ids: set, new_files: List[Dict]) -> None:
    processed = _load_processed(processed_path)
    today = datetime.now().strftime("%Y-%m-%d")
    by_id = {f["id"]: f for f in new_files}
    for drive_id in done_ids:
        if not drive_id:
            continue
        processed[drive_id] = {
            "name": by_id.get(drive_id, {}).get("name", ""),
            "processed_at": today,
        }
    processed_path.parent.mkdir(parents=True, exist_ok=True)
    processed_path.write_text(
        json.dumps(processed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"{len(done_ids)} facture(s) marquée(s) dans {processed_path.name}.", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Rapprochement fournisseurs de bout en bout.")
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--out-dir", default="reports/fournisseurs")
    parser.add_argument("--auth", action="store_true",
                        help="Ouvre le navigateur pour la 1re autorisation Google Drive.")
    parser.add_argument("--dry-run", action="store_true",
                        help="S'arrête après le rapprochement, n'attache ni ne marque rien.")
    args = parser.parse_args()

    load_dotenv()
    out_dir = Path(args.out_dir)
    input_dir = out_dir / "input"
    processed_path = out_dir / "processed.json"
    debits_path = out_dir / "qonto_debits.json"
    folder_name = os.environ.get("DRIVE_FOLDER_NAME", DEFAULT_FOLDER)
    creds_path = Path(os.environ.get("GOOGLE_CREDENTIALS_PATH", ROOT / "credentials.json"))
    token_path = Path(os.environ.get("GOOGLE_TOKEN_PATH", ROOT / "token.json"))

    # --- 1. Drive -----------------------------------------------------------
    print("\n=== 1/5 Téléchargement des factures (Google Drive) ===", file=sys.stderr)
    drive = DriveClient(creds_path, token_path)
    drive.authenticate(headless=not args.auth)
    new_files = step1_fetch_drive(drive, folder_name, input_dir, processed_path)

    if not any(input_dir.glob("*.pdf")):
        print("Aucun PDF à traiter.", file=sys.stderr)
        return 0

    # --- 2. Débits Qonto ----------------------------------------------------
    print("\n=== 2/5 Débits Qonto ===", file=sys.stderr)
    slug, key = load_qonto_credentials()
    client = QontoClient(slug, key)
    debits_raw = client.fetch_all_debits(since=args.since)
    out_dir.mkdir(parents=True, exist_ok=True)
    debits_path.write_text(
        json.dumps(debits_raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"{len(debits_raw)} débit(s) -> {debits_path}", file=sys.stderr)

    # --- 3. Rapprochement (en process pour conserver drive_file_id) ---------
    print("\n=== 3/5 Rapprochement ===", file=sys.stderr)
    id_map = {Path(f["local_path"]).name: f["id"] for f in new_files}
    invoices = recon.load_invoices(input_dir, id_map=id_map)
    invoices = [i for i in invoices if not i.date or i.date >= args.since]
    debits = recon.load_qonto_debits(debits_path)
    report = recon.match(invoices, debits, recon.DEFAULT_WARN_DAYS)
    report.run_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    today = report.run_date[:10]
    recon.write_matches_json(report, out_dir / "matches.json")
    recon.write_reconciliation_report(report, out_dir / f"reconciliation_{today}.md")
    recon.print_summary(report)

    if args.dry_run:
        print("\n[dry-run] Arrêt avant l'attachement Qonto.", file=sys.stderr)
        return 0

    # --- 4. Attachement -----------------------------------------------------
    print("\n=== 4/5 Attachement sur Qonto ===", file=sys.stderr)
    matches = json.loads((out_dir / "matches.json").read_text(encoding="utf-8"))
    if not matches:
        print("Aucun rapprochement — rien à attacher.", file=sys.stderr)
        return 0
    done_ids = step4_upload(client, matches)

    # --- 5. Marquer les traités --------------------------------------------
    print("\n=== 5/5 Marquage des factures traitées ===", file=sys.stderr)
    step5_mark(processed_path, done_ids, new_files)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
