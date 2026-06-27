#!/usr/bin/env python3
"""Orchestrateur du Flux 5 — Portails (détection Qonto → portail → attachement).

Enchaîne :
  1. detect_unreconciled  → backlog.json (débits sans PJ, par vendeur)
  2. fetch_{vendor}.py    → reports/portals/{vendor}/input/*.pdf + manifest.json
  3. reconcile_portals    → matches.json + reconciliation_*.md
  4. upload_attachment    → processed.json (idempotent)

Usage :
    python3 scripts/run_portals.py [--since YYYY-MM-DD] [--dry-run] [--vendor <key>] [--headed]
    python3 scripts/run_portals.py --dry-run          # voir le backlog + matches sans attacher
    python3 scripts/run_portals.py --vendor openai    # un seul vendeur
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from recon.qonto_client import QontoClient, load_qonto_credentials  # noqa: E402
from recon.portals import detect_unreconciled, load_portal_vendors, match_vendor  # noqa: E402
import reconcile_portals as recon  # noqa: E402

DEFAULT_SINCE = "2026-04-01"
SCRIPTS = ROOT / "scripts"


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def _run_fetcher(vendor_key: str, since: str, out_dir: Path,
                 headed: bool, dry_run: bool) -> int:
    fetcher = SCRIPTS / "portals" / f"fetch_{vendor_key}.py"
    if not fetcher.exists():
        print(f"  ✗ fetch_{vendor_key}.py introuvable ({fetcher})", file=sys.stderr)
        return 1
    cmd = [
        sys.executable, str(fetcher),
        "--since", since,
        "--out-dir", str(out_dir / vendor_key / "input"),
    ]
    if headed:
        cmd.append("--headed")
    if dry_run:
        cmd.append("--dry-run")
    print(f"\n=== Portail {vendor_key} ===", file=sys.stderr)
    result = subprocess.run(cmd)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Rapprochement portails Flux 5.")
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--out-dir", default="reports/portals")
    parser.add_argument("--dry-run", action="store_true",
                        help="Arrête après rapprochement, n'attache rien.")
    parser.add_argument("--vendor", default=None,
                        help="Traite un seul vendeur (ex. openai, notion, bolt…).")
    parser.add_argument("--headed", action="store_true",
                        help="Affiche le navigateur (utile pour débogage).")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Saute le scraping (utilise les manifests existants).")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    backlog_path = out_dir / "backlog.json"
    processed_path = out_dir / "processed.json"

    processed: Dict = _load_json(processed_path, {})

    # --- 1/4 Détection ---
    print("\n=== 1/4 Détection transactions sans PJ ===", file=sys.stderr)
    slug, key = load_qonto_credentials()
    client = QontoClient(slug, key)
    vendors = load_portal_vendors()

    all_unreconciled = detect_unreconciled(client, args.since)
    backlog = [
        {**tx, "vendor": v["name"], "handler_key": v["handler_key"]}
        for tx in all_unreconciled
        if tx.get("id") not in processed
        for v in [match_vendor(tx, vendors)]
        if v
    ]
    backlog_path.write_text(json.dumps(backlog, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(backlog)} transaction(s) sans PJ pour portails connus → {backlog_path.name}",
          file=sys.stderr)

    if not backlog:
        print("Rien à traiter.", file=sys.stderr)
        return 0

    # Grouper par handler_key.
    by_vendor: Dict[str, List] = {}
    for tx in backlog:
        hk = tx["handler_key"]
        by_vendor.setdefault(hk, []).append(tx)

    if args.vendor:
        by_vendor = {k: v for k, v in by_vendor.items() if k == args.vendor}
        if not by_vendor:
            print(f"Aucune transaction pour le vendeur « {args.vendor} ».", file=sys.stderr)
            return 0

    # --- 2/4 Fetch portails ---
    if args.skip_fetch:
        print("\n=== 2/4 Scraping sauté (--skip-fetch) ===", file=sys.stderr)
    else:
        print(f"\n=== 2/4 Scraping portails ({len(by_vendor)} vendeur(s)) ===", file=sys.stderr)
        for vendor_key in by_vendor:
            _run_fetcher(vendor_key, args.since, out_dir, args.headed, args.dry_run)

    # --- 3/4 Rapprochement ---
    print("\n=== 3/4 Rapprochement ===", file=sys.stderr)
    invoices = recon._load_all_manifests(out_dir)
    if not invoices:
        print("Aucune facture portail téléchargée — sortie.", file=sys.stderr)
        return 0

    matches, unmatched = recon.reconcile(invoices, backlog, vendors,
                                         window_days=10, warn_days=10)
    recon.write_matches_json(matches, out_dir / "matches.json")
    recon.write_unreconciled_json(unmatched, out_dir / "unreconciled.json")
    recon.write_report(
        matches, unmatched,
        out_dir / f"reconciliation_{datetime.now():%Y-%m-%d}.md"
    )
    exact = [m for m in matches if m.confidence == "exact"]
    warn = [m for m in matches if m.confidence != "exact"]
    print(f"{len(matches)} rapprochée(s) ({len(exact)} exacte(s), {len(warn)} warn), "
          f"{len(unmatched)} non rapprochée(s).", file=sys.stderr)

    for m in warn:
        print(f"  ⚠ {m.invoice.get('vendor')} {Path(m.invoice.get('pdf_path','')).name} "
              f"→ {m.debit.get('label')} {m.debit.get('settled_at')} (warn, non attaché)",
              file=sys.stderr)

    if args.dry_run:
        print("\n--dry-run : pas d'attachement.", file=sys.stderr)
        return 0

    # --- 4/4 Attachement ---
    print("\n=== 4/4 Attachement sur Qonto ===", file=sys.stderr)
    ok_count = 0
    skip_count = 0
    for m in exact:
        tx_id = m.debit.get("id")
        pdf = Path(m.invoice.get("pdf_path", ""))
        if not pdf.exists():
            print(f"  ✗ PDF introuvable : {pdf}", file=sys.stderr)
            continue
        try:
            if client.get_transaction_attachments(tx_id):
                skip_count += 1
                processed[tx_id] = {
                    "vendor": m.invoice.get("vendor"),
                    "pdf": pdf.name,
                    "attached_at": datetime.now().strftime("%Y-%m-%d"),
                }
                continue
        except Exception:
            pass
        try:
            client.upload_attachment(tx_id, pdf)
            print(f"  ✓ {pdf.name} → {m.debit.get('label')} ({m.debit.get('settled_at')})",
                  file=sys.stderr)
            processed[tx_id] = {
                "vendor": m.invoice.get("vendor"),
                "pdf": pdf.name,
                "attached_at": datetime.now().strftime("%Y-%m-%d"),
            }
            ok_count += 1
        except Exception as exc:
            print(f"  ✗ {pdf.name} : {exc}", file=sys.stderr)

    processed_path.write_text(json.dumps(processed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{ok_count} attachée(s), {skip_count} déjà documentée(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
