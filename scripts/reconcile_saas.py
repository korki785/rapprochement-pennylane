#!/usr/bin/env python3
"""Rapproche les factures SaaS (manifest.json) avec les débits Qonto.

Pour chaque facture (montant, devise, date, alias du fournisseur) :
  - candidat = débit dont le montant colle à `amount` (EUR) OU à `local_amount`
    (devise d'origine, ex. USD Anthropic), à ±0.01 ;
  - ET dont le libellé normalisé contient un alias du fournisseur (croisement
    nom marchand ↔ libellé — évite les faux positifs sur montants récurrents) ;
  - ET dont la date de règlement est dans la fenêtre (prélèvements/CB postérieurs
    à la facture). Le candidat le plus proche en date gagne.

confidence="exact" si alias trouvé + montant exact + écart ≤ WARN_DAYS → attaché auto.
Sinon "warn" (ex. fournisseur inconnu rapproché au montant seul) → revue manuelle.

Usage :
    python3 scripts/reconcile_saas.py \\
        --manifest reports/saas/manifest.json \\
        --transactions reports/saas/qonto_debits.json \\
        --out-dir reports/saas [--warn-days 10] [--window-days 15]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon.normalize import normalize_label  # noqa: E402

AMOUNT_TOLERANCE = 0.01
DEFAULT_WARN_DAYS = 10      # écart facture↔règlement encore considéré « fiable »
DEFAULT_WINDOW_DAYS = 15    # fenêtre max de recherche d'un candidat
MERGE_WINDOW_DAYS = 12      # facture + reçu de paiement du MÊME débit (Aircall) -> fusion


def _days_between(d1: str, d2: str) -> Optional[int]:
    try:
        a = date.fromisoformat(d1[:10])
        b = date.fromisoformat(d2[:10])
    except (ValueError, TypeError):
        return None
    return abs((a - b).days)


def _amount_close(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return False
    return abs(round(a, 2) - round(b, 2)) <= AMOUNT_TOLERANCE


def _alias_in_label(aliases: List[str], label: str) -> bool:
    norm = normalize_label(label)
    raw = (label or "").upper()
    for alias in aliases:
        a = alias.upper()
        if a in norm or a in raw:
            return True
    return False


@dataclass
class Match:
    invoice: Dict
    debit: Dict
    confidence: str
    date_gap: int
    matched_on: str        # "amount" | "local_amount"
    alias_ok: bool


def _is_invoice_doc(name: str) -> bool:
    n = (name or "").lower()
    return "invoice" in n or "facture" in n


def merge_duplicates(invoices: List[Dict]) -> List[Dict]:
    """Fusionne facture + reçu de paiement d'un même débit (même fournisseur, même
    montant/devise, dates rapprochées). Évite qu'un des deux reste orphelin éternel.

    L'entrée fusionnée porte tous les `pdf_paths` (facture d'abord) et tous les `msgids`.
    """
    groups: Dict[tuple, List[Dict]] = {}
    for inv in invoices:
        amt = inv.get("amount")
        vendor = inv.get("vendor")
        # Ne fusionne QUE des factures d'un même fournisseur CONNU (facture + reçu).
        # Fournisseur « inconnu » -> jamais fusionné (clé unique par message).
        if not vendor or vendor == "inconnu" or amt is None:
            key = ("__solo__", inv.get("msgid") or id(inv))
        else:
            key = (vendor, round(amt, 2), (inv.get("currency") or "").upper())
        groups.setdefault(key, []).append(inv)

    out: List[Dict] = []
    for items in groups.values():
        items.sort(key=lambda x: x.get("date") or x.get("email_date") or "")
        clusters: List[List[Dict]] = []
        for it in items:
            d = it.get("date") or it.get("email_date") or ""
            for cl in clusters:
                ref = cl[0].get("date") or cl[0].get("email_date") or ""
                g = _days_between(d, ref)
                if g is not None and g <= MERGE_WINDOW_DAYS:
                    cl.append(it)
                    break
            else:
                clusters.append([it])
        for cl in clusters:
            if len(cl) == 1:
                base = dict(cl[0])
                base["msgids"] = [base.get("msgid")]
                out.append(base)
                continue
            # facture en premier comme PDF principal/comptable.
            cl.sort(key=lambda x: not _is_invoice_doc(x.get("primary_pdf", "")))
            primary = cl[0]
            pdf_paths: List[str] = []
            for it in cl:
                for p in it.get("pdf_paths", []):
                    if p not in pdf_paths:
                        pdf_paths.append(p)
            merged = dict(primary)
            merged["pdf_paths"] = pdf_paths
            merged["msgids"] = [it.get("msgid") for it in cl]
            # garde la date de facture (la plus ancienne du cluster).
            merged["date"] = min((it.get("date") or it.get("email_date") or "") for it in cl)
            out.append(merged)
    return out


def match(invoices: List[Dict], debits: List[Dict],
          warn_days: int, window_days: int) -> tuple[List[Match], List[Dict]]:
    """Rapproche les factures aux débits. Renvoie (matches, factures_non_rapprochées)."""
    invoices = merge_duplicates(invoices)
    used: set = set()
    matches: List[Match] = []
    unmatched: List[Dict] = []

    # Factures avec un montant en premier ; date connue ensuite (greedy, plus sûr d'abord).
    ordered = sorted(invoices, key=lambda inv: (inv.get("amount") is None, inv.get("date") or ""))
    for inv in ordered:
        amount = inv.get("amount")
        inv_date = inv.get("date") or inv.get("email_date") or ""
        aliases = inv.get("aliases") or []
        if amount is None or not inv_date:
            unmatched.append({**inv, "_reason": "montant ou date illisible dans le PDF"})
            continue

        candidates = []
        for t in debits:
            if t.get("id") in used:
                continue
            gap = _days_between(inv_date, t.get("settled_at") or "")
            if gap is None or gap > window_days:
                continue
            if _amount_close(t.get("amount"), amount):
                matched_on = "amount"
            elif _amount_close(t.get("local_amount"), amount):
                matched_on = "local_amount"
            else:
                continue
            alias_ok = _alias_in_label(aliases, t.get("label", ""))
            candidates.append((t, gap, matched_on, alias_ok))

        if not candidates:
            unmatched.append({**inv, "_reason": "aucun débit Qonto au bon montant dans la fenêtre"})
            continue

        named = [c for c in candidates if c[3]]
        if named:
            # Lien nom marchand ↔ libellé : rapprochement de confiance.
            named.sort(key=lambda c: c[1])           # date la plus proche
            t, gap, matched_on, alias_ok = named[0]
            confidence = "exact" if gap <= warn_days else "warn"
        elif inv.get("company_id"):
            # Vraie facture « Maison Darwish » mais aucun libellé ne matche le nom :
            # bon montant possible sur le mauvais marchand -> à confirmer (jamais auto-attaché).
            candidates.sort(key=lambda c: c[1])
            t, gap, matched_on, alias_ok = candidates[0]
            confidence = "warn"
        else:
            # Ni lien de nom, ni identité société -> on ne devine pas.
            unmatched.append({**inv, "_reason": "montant/date trouvés mais ni nom ni identité société ne correspondent"})
            continue

        used.add(t.get("id"))
        matches.append(Match(inv, t, confidence, gap, matched_on, alias_ok))

    return matches, unmatched


def write_matches_json(matches: List[Match], path: Path) -> None:
    out = []
    for m in matches:
        out.append({
            "msgid": m.invoice.get("msgid"),
            "msgids": m.invoice.get("msgids", [m.invoice.get("msgid")]),
            "vendor": m.invoice.get("vendor"),
            "sender": m.invoice.get("sender"),
            "company_id": m.invoice.get("company_id", False),
            "invoice_name": Path(m.invoice.get("primary_pdf", "")).name,
            "primary_pdf": m.invoice.get("primary_pdf"),
            "pdf_paths": m.invoice.get("pdf_paths", []),
            "invoice_amount": m.invoice.get("amount"),
            "invoice_currency": m.invoice.get("currency"),
            "invoice_date": m.invoice.get("date"),
            "qonto_transaction_id": m.debit.get("id"),
            "qonto_label": m.debit.get("label"),
            "qonto_date": m.debit.get("settled_at"),
            "qonto_amount": m.debit.get("amount"),
            "qonto_local_amount": m.debit.get("local_amount"),
            "qonto_local_currency": m.debit.get("local_currency"),
            "matched_on": m.matched_on,
            "alias_ok": m.alias_ok,
            "date_gap_days": m.date_gap,
            "confidence": m.confidence,
        })
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def write_unreconciled_json(unmatched: List[Dict], path: Path) -> None:
    items = []
    for inv in unmatched:
        items.append({
            "vendor": inv.get("vendor"),
            "name": Path(inv.get("primary_pdf", "")).name,
            "amount": inv.get("amount"),
            "currency": inv.get("currency"),
            "date": inv.get("date"),
            "email_date": inv.get("email_date"),
            "reason": inv.get("_reason", "non rapprochée"),
        })
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def write_report(matches: List[Match], unmatched: List[Dict], path: Path) -> None:
    exact = [m for m in matches if m.confidence == "exact"]
    warn = [m for m in matches if m.confidence != "exact"]
    lines = [
        "# Rapprochement SaaS",
        "",
        f"{len(matches)} facture(s) rapprochée(s) ({len(exact)} fiable(s), {len(warn)} à vérifier), "
        f"{len(unmatched)} non rapprochée(s).",
        "",
        "## Rapprochées",
        "",
        "| Fournisseur | PDF | Montant | Devise | Date facture | Débit Qonto | Date débit | Écart | Sur | Alias | Confiance |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for m in matches:
        flag = "✅ exact" if m.confidence == "exact" else "⚠️ warn"
        lines.append(
            f"| {m.invoice.get('vendor','')} | {Path(m.invoice.get('primary_pdf','')).name} "
            f"| {m.invoice.get('amount')} | {m.invoice.get('currency')} | {m.invoice.get('date')} "
            f"| {m.debit.get('label','')} | {m.debit.get('settled_at','')} | {m.date_gap} j "
            f"| {m.matched_on} | {'oui' if m.alias_ok else 'non'} | {flag} |"
        )
    lines += ["", "## Non rapprochées", "",
              "| Fournisseur | PDF | Montant | Devise | Date | Raison |", "|---|---|---|---|---|---|"]
    for inv in unmatched:
        lines.append(
            f"| {inv.get('vendor','')} | {Path(inv.get('primary_pdf','')).name} "
            f"| {inv.get('amount')} | {inv.get('currency')} | {inv.get('date')} "
            f"| {inv.get('_reason','')} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rapprochement factures SaaS ↔ débits Qonto.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--transactions", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--warn-days", type=int, default=DEFAULT_WARN_DAYS)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    invoices: List[Dict] = []
    if manifest_path.exists():
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        invoices = list(data.values()) if isinstance(data, dict) else list(data)
    debits = json.loads(Path(args.transactions).read_text(encoding="utf-8"))

    matches, unmatched = match(invoices, debits, args.warn_days, args.window_days)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_matches_json(matches, out_dir / "matches.json")
    write_unreconciled_json(unmatched, out_dir / "unreconciled.json")
    write_report(matches, unmatched, out_dir / f"reconciliation_{date.today().isoformat()}.md")

    exact = sum(1 for m in matches if m.confidence == "exact")
    print(f"{len(matches)} rapprochée(s) ({exact} fiable(s)), {len(unmatched)} non rapprochée(s).",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
