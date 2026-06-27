#!/usr/bin/env python3
"""Rapproche les factures portail (manifest.json par vendeur) avec les débits Qonto du backlog.

Pour chaque facture :
  - candidat = débit dont montant colle à `amount` (EUR) OU `local_amount` (devise), ±0.01
  - ET alias du vendeur présent dans le libellé Qonto
  - ET settled_at dans la fenêtre date ± window_days

confidence="exact" → attaché automatiquement par run_portals.py.
confidence="warn"  → listé, jamais auto-attaché (révision manuelle).

Usage :
    python3 scripts/reconcile_portals.py \\
        --backlog reports/portals/backlog.json \\
        --manifests-dir reports/portals \\
        --out-dir reports/portals [--window-days 10]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon.normalize import normalize_label  # noqa: E402
from recon.portals import load_portal_vendors  # noqa: E402

AMOUNT_TOL = 0.01
DEFAULT_WINDOW = 10
DEFAULT_WARN = 10


def _days(d1: str, d2: str) -> Optional[int]:
    try:
        return abs((date.fromisoformat(d1[:10]) - date.fromisoformat(d2[:10])).days)
    except (ValueError, TypeError):
        return None


def _amount_close(a, b) -> bool:
    if a is None or b is None:
        return False
    return abs(round(float(a), 2) - round(float(b), 2)) <= AMOUNT_TOL


def _alias_ok(aliases: List[str], label: str) -> bool:
    norm = normalize_label(label)
    raw = (label or "").upper()
    return any(a.upper() in norm or a.upper() in raw for a in aliases)


@dataclass
class Match:
    invoice: Dict
    debit: Dict
    confidence: str
    date_gap: int
    matched_on: str


def _vendor_aliases(vendor_key: str, vendors: List[Dict]) -> List[str]:
    for v in vendors:
        if v.get("handler_key") == vendor_key:
            return [p.strip() for p in v.get("qonto_label_patterns", "").split("|") if p.strip()]
    return [vendor_key.upper()]


def reconcile(invoices: List[Dict], backlog: List[Dict],
              vendors: List[Dict], window_days: int, warn_days: int) -> tuple:
    used: set = set()
    matches: List[Match] = []
    unmatched: List[Dict] = []

    for inv in invoices:
        amount = inv.get("amount")
        inv_date = inv.get("date") or ""
        vendor_key = inv.get("vendor") or ""
        aliases = _vendor_aliases(vendor_key, vendors)

        if amount is None or not inv_date:
            unmatched.append({**inv, "_reason": "montant ou date manquant"})
            continue

        candidates = []
        for tx in backlog:
            if tx.get("id") in used:
                continue
            gap = _days(inv_date, tx.get("settled_at") or "")
            if gap is None or gap > window_days:
                continue
            if _amount_close(tx.get("amount"), amount):
                matched_on = "amount"
            elif _amount_close(tx.get("local_amount"), amount):
                matched_on = "local_amount"
            else:
                continue
            alias = _alias_ok(aliases, tx.get("label", ""))
            candidates.append((tx, gap, matched_on, alias))

        if not candidates:
            unmatched.append({**inv, "_reason": "aucun débit Qonto au bon montant dans la fenêtre"})
            continue

        named = [c for c in candidates if c[3]]
        if named:
            named.sort(key=lambda c: c[1])
            tx, gap, matched_on, _ = named[0]
            confidence = "exact" if gap <= warn_days else "warn"
        else:
            # Bon montant mais alias absent du libellé → warn.
            candidates.sort(key=lambda c: c[1])
            tx, gap, matched_on, _ = candidates[0]
            confidence = "warn"

        used.add(tx.get("id"))
        matches.append(Match(inv, tx, confidence, gap, matched_on))

    return matches, unmatched


def write_matches_json(matches: List[Match], path: Path) -> None:
    out = []
    for m in matches:
        out.append({
            "vendor": m.invoice.get("vendor"),
            "invoice_id": m.invoice.get("invoice_id"),
            "pdf_path": m.invoice.get("pdf_path"),
            "invoice_amount": m.invoice.get("amount"),
            "invoice_date": m.invoice.get("date"),
            "qonto_transaction_id": m.debit.get("id"),
            "qonto_label": m.debit.get("label"),
            "qonto_date": m.debit.get("settled_at"),
            "qonto_amount": m.debit.get("amount"),
            "qonto_local_amount": m.debit.get("local_amount"),
            "qonto_local_currency": m.debit.get("local_currency"),
            "matched_on": m.matched_on,
            "date_gap_days": m.date_gap,
            "confidence": m.confidence,
        })
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def write_unreconciled_json(unmatched: List[Dict], path: Path) -> None:
    items = [{"vendor": i.get("vendor"), "invoice_id": i.get("invoice_id"),
              "amount": i.get("amount"), "date": i.get("date"),
              "reason": i.get("_reason", "non rapprochée")} for i in unmatched]
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def write_report(matches: List[Match], unmatched: List[Dict], path: Path) -> None:
    exact = [m for m in matches if m.confidence == "exact"]
    warn = [m for m in matches if m.confidence != "exact"]
    lines = [
        "# Rapprochement Portails",
        "",
        f"{len(matches)} rapprochée(s) ({len(exact)} exacte(s), {len(warn)} warn), "
        f"{len(unmatched)} non rapprochée(s).",
        "",
        "## Rapprochées",
        "",
        "| Vendeur | Facture | Montant | Date | Qonto | Date Qonto | Écart | Sur | Confiance |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for m in matches:
        flag = "✅ exact" if m.confidence == "exact" else "⚠️ warn"
        pdf = Path(m.invoice.get("pdf_path", "")).name
        lines.append(
            f"| {m.invoice.get('vendor')} | {pdf} | {m.invoice.get('amount')} "
            f"| {m.invoice.get('date')} | {m.debit.get('label')} "
            f"| {m.debit.get('settled_at')} | {m.date_gap} j | {m.matched_on} | {flag} |"
        )
    lines += ["", "## Non rapprochées", "", "| Vendeur | Facture | Montant | Date | Raison |",
              "|---|---|---|---|---|"]
    for inv in unmatched:
        lines.append(
            f"| {inv.get('vendor')} | {inv.get('invoice_id')} | {inv.get('amount')} "
            f"| {inv.get('date')} | {inv.get('_reason', '')} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_all_manifests(manifests_dir: Path) -> List[Dict]:
    invoices = []
    for manifest_path in manifests_dir.glob("*/manifest.json"):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = data if isinstance(data, list) else list(data.values())
            invoices.extend(entries)
        except Exception as exc:
            print(f"  ⚠ {manifest_path}: {exc}", file=sys.stderr)
    return invoices


def main() -> int:
    parser = argparse.ArgumentParser(description="Rapprochement factures portails ↔ débits Qonto.")
    parser.add_argument("--backlog", required=True)
    parser.add_argument("--manifests-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW)
    parser.add_argument("--warn-days", type=int, default=DEFAULT_WARN)
    args = parser.parse_args()

    backlog = json.loads(Path(args.backlog).read_text(encoding="utf-8"))
    invoices = _load_all_manifests(Path(args.manifests_dir))
    vendors = load_portal_vendors()

    matches, unmatched = reconcile(invoices, backlog, vendors, args.window_days, args.warn_days)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_matches_json(matches, out_dir / "matches.json")
    write_unreconciled_json(unmatched, out_dir / "unreconciled.json")
    write_report(matches, unmatched, out_dir / f"reconciliation_{date.today().isoformat()}.md")

    exact = sum(1 for m in matches if m.confidence == "exact")
    print(
        f"{len(matches)} rapprochée(s) ({exact} exacte(s)), {len(unmatched)} non rapprochée(s).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
