#!/usr/bin/env python3
"""Rapprochement des RECETTES : factures clients ↔ virements créditeurs Qonto.

Sens inverse des flux dépenses. Pour chaque virement ENTRANT (crédit) reçu d'un
client, on retrouve la facture client émise correspondante et on produit
`matches.json` (consommé par run_recettes.py pour attacher le PDF de la facture à
la transaction créditrice) + un rapport markdown.

Fait métier clé : les clients étrangers (ex. Allegria, US) paient par virement
SWIFT → le montant crédité ≠ total de la facture (frais bancaires ~40€ déduits,
ou petit écart d'arrondi FX). Le match ne peut donc PAS être exact au centime sur
tous les virements. Règles :
  - Montant : reçu comparé à `amount` (EUR) ET `local_amount` (devise). Candidat si
    reçu ∈ [total − FEE_MAX, total + ROUND_EPS] (couvre les frais SWIFT et l'arrondi).
  - Nom client EXIGÉ : un jeton du nom de la facture doit être dans le libellé Qonto
    (garde anti-faux-positif « bon montant, mauvais client »).
  - Confiance :
      exact  = nom + |reçu − total| ≤ 0,02 (sur amount ou local_amount).
      swift  = nom + bande + operation_type == swift_income + candidat UNIQUE.
      warn   = nom + bande mais non exact et non (swift unique) → listé, jamais auto.
    Auto-attache = {exact, swift} uniquement (comme le flux saas).

Usage :
    python3 scripts/reconcile_recettes.py \\
        --invoices     reports/recettes/client_invoices.json \\
        --transactions reports/recettes/qonto_credits.json \\
        --out-dir      reports/recettes [--since YYYY-MM-DD] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import List, Optional

# Permet `python3 scripts/reconcile_recettes.py` sans installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recon.normalize import normalize_label, ratio  # noqa: E402

DEFAULT_SINCE = "2026-04-01"
EXACT_TOLERANCE = 0.02      # écart considéré « exact » (arrondi négligeable)
FEE_MAX = 45.0             # frais SWIFT max déduits d'un virement (reçu < total)
ROUND_EPS = 2.0            # petit dépassement toléré (arrondi FX : reçu > total)
GRACE_DAYS = 5             # le paiement ne peut pas précéder l'émission (marge)
NAME_RATIO_MIN = 80.0      # repli si le nom du client est tronqué dans le libellé
SWIFT_OPS = {"swift_income"}
AUTO_ATTACH = {"exact", "swift"}


# --------------------------------------------------------------------------- #
# Structures de données
# --------------------------------------------------------------------------- #
@dataclass
class ClientInvoice:
    id: str
    number: str
    client_name: str
    total: float
    currency: str
    status: str
    attachment_id: str
    issue_date: str        # ISO YYYY-MM-DD

    @property
    def is_valid(self) -> bool:
        return bool(self.attachment_id) and self.total > 0 and bool(self.client_name)


@dataclass
class CreditTx:
    id: str
    label: str
    amount: float          # EUR crédité (net reçu)
    currency: str
    local_amount: float    # montant en devise d'origine (0.0 si EUR)
    local_currency: str
    settled_at: str        # ISO YYYY-MM-DD
    operation_type: str    # "income" | "swift_income" | ...


@dataclass
class MatchResult:
    invoice: ClientInvoice
    transaction: CreditTx
    date_gap_days: int
    match_strategy: str    # "exact" | "swift" | "band"
    confidence: str        # "exact" | "swift" | "warn"


@dataclass
class ReconciliationReport:
    matched: List[MatchResult] = field(default_factory=list)
    unmatched_invoices: List[ClientInvoice] = field(default_factory=list)
    unmatched_transactions: List[CreditTx] = field(default_factory=list)
    run_date: str = ""


# --------------------------------------------------------------------------- #
# Chargement
# --------------------------------------------------------------------------- #
def _to_float(v) -> float:
    try:
        return abs(float(str(v).replace(",", ".")))
    except (TypeError, ValueError):
        return 0.0


def load_client_invoices(json_path: Path) -> List[ClientInvoice]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    out: List[ClientInvoice] = []
    for i in data:
        out.append(ClientInvoice(
            id=str(i.get("id", "")),
            number=str(i.get("number", "")),
            client_name=str(i.get("client_name", "")),
            total=_to_float(i.get("total_amount")),
            currency=str(i.get("currency") or "EUR"),
            status=str(i.get("status", "")),
            attachment_id=str(i.get("attachment_id", "")),
            issue_date=str(i.get("issue_date") or "")[:10],
        ))
    return out


def load_qonto_credits(json_path: Path) -> List[CreditTx]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    out: List[CreditTx] = []
    for t in data:
        out.append(CreditTx(
            id=str(t.get("id", "")),
            label=str(t.get("label", "")),
            amount=_to_float(t.get("amount")),
            currency=str(t.get("currency") or "EUR"),
            local_amount=_to_float(t.get("local_amount")),
            local_currency=str(t.get("local_currency") or "").upper(),
            settled_at=str(t.get("settled_at") or t.get("date") or "")[:10],
            operation_type=str(t.get("operation_type") or ""),
        ))
    return out


# --------------------------------------------------------------------------- #
# Rapprochement
# --------------------------------------------------------------------------- #
def _received_amounts(t: CreditTx) -> List[float]:
    """Montants crédités à comparer : EUR (net) et devise d'origine si présente."""
    vals = [t.amount]
    if t.local_amount and t.local_amount != t.amount:
        vals.append(t.local_amount)
    return [v for v in vals if v > 0]


def name_matches(client_name: str, label: str) -> bool:
    """Vrai si le nom du client apparaît dans le libellé Qonto.

    Exige que TOUS les jetons significatifs du nom (normalisé, suffixe juridique
    « INC/LLC/… » retiré) soient présents dans le libellé normalisé. Repli : ratio
    global ≥ NAME_RATIO_MIN pour les libellés tronqués. « ALLEGRIA INC » → jeton
    ALLEGRIA ∈ « ALLEGRIA, INC ». « SEE YOU SOON » → SEE/YOU/SOON ∈ « SEE YOU SOON ».
    """
    ct = normalize_label(client_name).split()
    if not ct:
        return False
    norm_label = normalize_label(label)
    lt = set(norm_label.split())
    if all(tok in lt for tok in ct):
        return True
    return ratio(" ".join(ct), norm_label) >= NAME_RATIO_MIN


def _best_amount_gap(inv: ClientInvoice, t: CreditTx) -> Optional[float]:
    """Plus petit |reçu − total| si un montant reçu tombe dans la bande, sinon None."""
    lo, hi = inv.total - FEE_MAX, inv.total + ROUND_EPS
    gaps = [abs(v - inv.total) for v in _received_amounts(t) if lo <= v <= hi]
    return min(gaps) if gaps else None


def _days_signed(d_from: str, d_to: str) -> int:
    """Jours de d_from à d_to (positif si d_to après d_from). Grand si illisible."""
    try:
        return (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days
    except (ValueError, TypeError):
        return 10 ** 6


def _is_candidate(inv: ClientInvoice, t: CreditTx) -> Optional[float]:
    """Écart de montant si (inv, t) est un candidat plausible, sinon None.

    Candidat = nom du client dans le libellé + montant dans la bande + le virement
    ne précède pas l'émission (marge GRACE_DAYS).
    """
    if not inv.is_valid:
        return None
    if not name_matches(inv.client_name, t.label):
        return None
    gap = _best_amount_gap(inv, t)
    if gap is None:
        return None
    if t.settled_at and inv.issue_date and _days_signed(inv.issue_date, t.settled_at) < -GRACE_DAYS:
        return None  # un paiement ne peut pas précéder la facture
    return gap


def _confidence(inv: ClientInvoice, t: CreditTx, amount_gap: float,
                credits_for_inv: List[CreditTx], invoices_for_tx: List[ClientInvoice]) -> tuple:
    """(match_strategy, confidence). Voir en-tête du module."""
    if amount_gap <= EXACT_TOLERANCE:
        return "exact", "exact"
    # Écart non nul (frais SWIFT / arrondi) : sûr seulement si SWIFT + accouplement unique.
    unique = len(credits_for_inv) == 1 and len(invoices_for_tx) == 1
    if t.operation_type in SWIFT_OPS and unique:
        return "swift", "swift"
    return "band", "warn"


def match(invoices: List[ClientInvoice], credits: List[CreditTx]) -> ReconciliationReport:
    """Glouton 1:1. Chaque facture prend le crédit candidat le plus proche (montant,
    puis date). La confiance dépend de l'exactitude du montant et de l'unicité.
    """
    report = ReconciliationReport()
    report.unmatched_invoices.extend(i for i in invoices if not i.is_valid)

    valid = [i for i in invoices if i.is_valid]
    # Pré-calcul des candidats (facture, crédit) → écart de montant.
    cand = {}  # (inv_id, tx_id) -> gap
    for inv in valid:
        for t in credits:
            g = _is_candidate(inv, t)
            if g is not None:
                cand[(inv.id, t.id)] = g

    used_tx: set = set()
    # Traite d'abord les factures les plus récentes (paiement le plus probable).
    for inv in sorted(valid, key=lambda i: i.issue_date or "", reverse=True):
        cands = [t for t in credits
                 if t.id not in used_tx and (inv.id, t.id) in cand]
        if not cands:
            report.unmatched_invoices.append(inv)
            continue
        best = min(cands, key=lambda t: (cand[(inv.id, t.id)],
                                         abs(_days_signed(inv.issue_date, t.settled_at))))
        # Unicité : combien de crédits/factures pouvaient s'apparier à ce couple ?
        credits_for_inv = [t for t in credits if (inv.id, t.id) in cand]
        invoices_for_tx = [i2 for i2 in valid if (i2.id, best.id) in cand]
        strat, conf = _confidence(inv, best, cand[(inv.id, best.id)],
                                  credits_for_inv, invoices_for_tx)
        gap_days = abs(_days_signed(inv.issue_date, best.settled_at))
        report.matched.append(MatchResult(inv, best, gap_days, strat, conf))
        used_tx.add(best.id)

    report.unmatched_transactions = [t for t in credits if t.id not in used_tx]
    return report


# --------------------------------------------------------------------------- #
# Sorties
# --------------------------------------------------------------------------- #
def write_matches_json(report: ReconciliationReport, path: Path) -> None:
    payload = [
        {
            "invoice_id": m.invoice.id,
            "invoice_number": m.invoice.number,
            "invoice_attachment_id": m.invoice.attachment_id,
            "invoice_client": m.invoice.client_name,
            "invoice_total": round(m.invoice.total, 2),
            "invoice_currency": m.invoice.currency,
            "invoice_issue_date": m.invoice.issue_date,
            "qonto_transaction_id": m.transaction.id,
            "qonto_date": m.transaction.settled_at,
            "qonto_amount": round(m.transaction.amount, 2),
            "qonto_label": m.transaction.label,
            "match_strategy": m.match_strategy,
            "confidence": m.confidence,
        }
        for m in report.matched
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _fmt_amount(amount: float, currency: str) -> str:
    sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency.upper(), currency)
    return f"{amount:.2f} {sym}"


_CONF_LABEL = {"exact": "✅ exact", "swift": "✅ SWIFT (unique)", "warn": "⚠️ à vérifier"}


def write_reconciliation_report(report: ReconciliationReport, path: Path) -> None:
    n_ok = len(report.matched)
    n_auto = sum(1 for m in report.matched if m.confidence in AUTO_ATTACH)
    n_warn = n_ok - n_auto
    out = ["# Rapprochement recettes — factures clients ↔ virements Qonto", ""]
    out.append(
        f"**{n_ok} facture(s) rapprochée(s)** "
        f"({n_auto} auto-attachée(s), {n_warn} à vérifier), "
        f"**{len(report.unmatched_invoices)} facture(s)** sans virement, "
        f"**{len(report.unmatched_transactions)} virement(s)** sans facture."
    )
    out.append("")

    out.append("## Factures rapprochées")
    out.append("")
    out.append("| Facture | Client | Total facture | Reçu Qonto | Libellé | Date fact. | Date virt. | Statut |")
    out.append("|---|---|---:|---:|---|---|---|---|")
    for m in sorted(report.matched, key=lambda m: m.transaction.settled_at):
        out.append(
            f"| {m.invoice.number} | {m.invoice.client_name} | "
            f"{_fmt_amount(m.invoice.total, m.invoice.currency)} | "
            f"{_fmt_amount(m.transaction.amount, m.transaction.currency)} | "
            f"{m.transaction.label} | {m.invoice.issue_date} | {m.transaction.settled_at} | "
            f"{_CONF_LABEL.get(m.confidence, m.confidence)} |"
        )
    out.append("")

    out.append("## Factures sans virement (impayées ou paiement non trouvé)")
    out.append("")
    out.append("| Facture | Client | Total | Statut Qonto | Date |")
    out.append("|---|---|---:|---|---|")
    for inv in sorted(report.unmatched_invoices, key=lambda i: i.issue_date or ""):
        if not inv.is_valid:
            continue  # factures sans PDF/total : bruit, non listées
        out.append(f"| {inv.number} | {inv.client_name} | "
                   f"{_fmt_amount(inv.total, inv.currency)} | {inv.status} | {inv.issue_date} |")
    out.append("")

    out.append("## Virements Qonto sans facture")
    out.append("")
    out.append("| Libellé | Montant | Devise | Date | ID transaction |")
    out.append("|---|---:|---|---|---|")
    for t in sorted(report.unmatched_transactions, key=lambda t: t.settled_at):
        dev = t.local_currency or "EUR"
        amt = _fmt_amount(t.local_amount if t.local_amount else t.amount, dev)
        out.append(f"| {t.label} | {amt} | {dev} | {t.settled_at} | {t.id} |")
    out.append("")

    out.append("---")
    out.append(
        f"_Généré par `scripts/reconcile_recettes.py` le {report.run_date}. "
        f"Pièces jointes à attacher : voir `matches.json`._"
    )
    path.write_text("\n".join(out), encoding="utf-8")


def print_summary(report: ReconciliationReport) -> None:
    n_auto = sum(1 for m in report.matched if m.confidence in AUTO_ATTACH)
    n_warn = len(report.matched) - n_auto
    print(
        f"{len(report.matched)} rapprochée(s) "
        f"({n_auto} auto, {n_warn} à vérifier) · "
        f"{sum(1 for i in report.unmatched_invoices if i.is_valid)} facture(s) sans virement · "
        f"{len(report.unmatched_transactions)} virement(s) orphelin(s)",
        file=sys.stderr,
    )
    for m in report.matched:
        flag = "  " if m.confidence in AUTO_ATTACH else "⚠ "
        print(
            f"  {flag}{m.invoice.number:<12} {m.invoice.client_name[:22]:<22} "
            f"{_fmt_amount(m.invoice.total, m.invoice.currency):>12} "
            f"↔ {m.transaction.settled_at} [{m.confidence}]",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rapproche les factures clients avec les virements créditeurs Qonto."
    )
    parser.add_argument("--invoices", default="reports/recettes/client_invoices.json")
    parser.add_argument("--transactions", default="reports/recettes/qonto_credits.json")
    parser.add_argument("--out-dir", default="reports/recettes")
    parser.add_argument("--since", default=DEFAULT_SINCE,
                        help=f"Ignore les virements avant cette date (défaut {DEFAULT_SINCE}).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Affiche le rapprochement sans écrire de fichier.")
    args = parser.parse_args()

    invoices_path = Path(args.invoices)
    transactions_path = Path(args.transactions)
    if not invoices_path.exists() or not transactions_path.exists():
        raise SystemExit(
            "Fichiers introuvables. Lancer d'abord run_recettes.py pour récupérer "
            "les factures clients et les virements Qonto."
        )

    invoices = load_client_invoices(invoices_path)
    credits = [t for t in load_qonto_credits(transactions_path)
               if not t.settled_at or t.settled_at >= args.since]

    report = match(invoices, credits)

    from datetime import datetime
    report.run_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    today = report.run_date[:10]

    print_summary(report)
    if args.dry_run:
        print("\n[dry-run] Aucun fichier écrit.", file=sys.stderr)
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_matches_json(report, out_dir / "matches.json")
    write_reconciliation_report(report, out_dir / f"reconciliation_{today}.md")
    print(f"\nRapport : {out_dir / f'reconciliation_{today}.md'}", file=sys.stderr)
    print(f"À attacher : {out_dir / 'matches.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
