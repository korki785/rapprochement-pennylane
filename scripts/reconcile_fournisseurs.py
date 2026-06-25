#!/usr/bin/env python3
"""Rapprochement des factures fournisseurs (Google Drive) avec les transactions Qonto.

Pour chaque facture PDF téléchargée depuis le Drive « Factures fournisseurs »,
ce script trouve la transaction Qonto correspondante et produit `matches.json`
(consommé par run_fournisseurs.py pour attacher les PDF) + un rapport markdown.

Trois stratégies de rapprochement :
  - EUR / carte    : débit carte en EUR, montant ± 0,01 €, date ± 7 j.
  - EUR / virement : virement perso « Nael Darwish » (facture payée perso, remboursée).
  - Devise         : paiement carte en devise étrangère. Pas de conversion :
                     Qonto stocke local_amount + local_currency ; on matche dessus.

Usage :
    python3 scripts/reconcile_fournisseurs.py \\
        --input        reports/fournisseurs/input \\
        --transactions reports/fournisseurs/qonto_debits.json \\
        --out-dir      reports/fournisseurs \\
        [--since YYYY-MM-DD] [--warn-days 7] [--dry-run]

Nécessite pdftotext (poppler) : brew install poppler.
Repli : reports/fournisseurs/manifest.csv (filename,date,amount,currency,drive_file_id).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Permet `python3 scripts/reconcile_fournisseurs.py` sans installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

DEFAULT_SINCE = "2026-04-01"
DEFAULT_WARN_DAYS = 7
AMOUNT_TOLERANCE = 0.01
PERSO_TERMS = ("nael", "darwish")

# Mois français pour le parsing des dates longues (« 15 mai 2026 »).
_FR_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}

# Symboles / codes -> code ISO 4217.
_CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP"}


# --------------------------------------------------------------------------- #
# Structures de données
# --------------------------------------------------------------------------- #
@dataclass
class SupplierInvoice:
    path: Path
    drive_file_id: str
    drive_name: str
    date: str           # ISO YYYY-MM-DD ("" si introuvable)
    amount: float       # montant dans la devise de la facture (0.0 si introuvable)
    currency: str       # ISO 4217 (EUR par défaut)
    raw_text: str = ""
    error: str = ""

    @property
    def is_valid(self) -> bool:
        return not self.error and bool(self.date) and self.amount > 0

    @property
    def is_foreign(self) -> bool:
        return self.currency.upper() not in ("EUR", "")


@dataclass
class QontoDebit:
    id: str
    label: str
    amount: float           # EUR, positif
    currency: str           # devise du compte (EUR)
    local_amount: float     # montant en devise d'origine (0.0 si EUR)
    local_currency: str     # code devise d'origine ("" si EUR)
    settled_at: str         # ISO YYYY-MM-DD
    operation_type: str     # "card" | "transfer" | ...


@dataclass
class MatchResult:
    invoice: SupplierInvoice
    transaction: QontoDebit
    date_gap_days: int
    match_strategy: str     # "eur_card" | "eur_transfer" | "foreign_currency"
    confidence: str         # "exact" | "warn"


@dataclass
class ReconciliationReport:
    matched: List[MatchResult] = field(default_factory=list)
    unmatched_invoices: List[SupplierInvoice] = field(default_factory=list)
    unmatched_transactions: List[QontoDebit] = field(default_factory=list)
    run_date: str = ""


# --------------------------------------------------------------------------- #
# Lecture des PDF
# --------------------------------------------------------------------------- #
def scan_input_dir(input_dir: Path) -> List[Path]:
    return sorted(input_dir.glob("*.pdf"))


def _pdftotext_bin() -> Optional[str]:
    found = shutil.which("pdftotext")
    if found:
        return found
    for candidate in ["/opt/homebrew/bin/pdftotext", "/usr/local/bin/pdftotext"]:
        if Path(candidate).exists():
            return candidate
    return None


def _run_pdftotext(path: Path) -> str:
    bin_path = _pdftotext_bin() or "pdftotext"
    proc = subprocess.run(
        [bin_path, "-layout", str(path), "-"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        raise ValueError(f"pdftotext a échoué : {proc.stderr.strip()}")
    return proc.stdout


def _parse_date(text: str) -> str:
    """Première date trouvée au format ISO, sinon ""."""
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})\b", text)
    if m:
        mo = _FR_MONTHS.get(m.group(2).lower())
        if mo:
            return f"{int(m.group(3)):04d}-{mo:02d}-{int(m.group(1)):02d}"
    return ""


def _detect_currency(text: str) -> str:
    """Devise dominante du texte. EUR par défaut.

    Si plusieurs devises apparaissent, prend la plus fréquente ; départage par
    EUR (devise par défaut des factures françaises).
    """
    from collections import Counter

    counts: Counter = Counter()
    for sym, code in _CURRENCY_SYMBOLS.items():
        counts[code] += text.count(sym)
    for code in ("EUR", "USD", "GBP"):
        counts[code] += len(re.findall(rf"\b{code}\b", text))

    counts = Counter({c: n for c, n in counts.items() if n > 0})
    if not counts:
        return "EUR"
    top = counts.most_common()
    best_n = top[0][1]
    tied = [c for c, n in top if n == best_n]
    return "EUR" if "EUR" in tied else tied[0]


def _parse_amount(text: str, currency: str) -> float:
    """Total TTC dans la devise donnée, sinon 0.0.

    Cherche le montant près de « Total TTC » / « Total » / « Montant », puis
    se rabat sur le plus grand montant répété (= total confirmé).
    """
    sym = {"EUR": "€", "USD": r"\$", "GBP": "£"}.get(currency, "€")
    code = currency
    # Motif de montant : devise avant ou après le nombre.
    num = r"\d{1,3}(?:[  ,.]\d{3})*[.,]\d{2}"
    money = rf"(?:(?:{sym}|{code})\s*({num})|({num})\s*(?:{sym}|{code}))"

    def _to_float(s: str) -> float:
        s = s.replace(" ", "").replace(" ", "")
        # Dernier séparateur = décimal ; les autres = milliers.
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            s = s.replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return 0.0

    # 1. Montant proche d'un libellé « Total » (frontière de mot pour éviter
    #    « Sous-total » / « Subtotal »).
    for label in ("Total TTC", "Montant total", "Total amount", "Amount due", "Total", "Montant"):
        m = re.search(rf"(?<![A-Za-zÀ-ÿ]){label}[^\d\n]{{0,40}}{money}", text, re.IGNORECASE)
        if m:
            val = _to_float(m.group(1) or m.group(2) or "")
            if val > 0:
                return val

    # 2. Plus grand montant apparaissant au moins deux fois.
    cands = [m.group(1) or m.group(2) for m in re.finditer(money, text)]
    cands = [c for c in cands if c]
    if cands:
        from collections import Counter
        counts = Counter(cands)
        repeated = [c for c, n in counts.items() if n >= 2]
        if repeated:
            return max(_to_float(c) for c in repeated)
        return max(_to_float(c) for c in cands)
    return 0.0


def parse_invoice(pdf_path: Path, drive_file_id: str = "", drive_name: str = "") -> SupplierInvoice:
    """Parse un PDF fournisseur. En cas d'échec : champ `error` rempli."""
    drive_name = drive_name or pdf_path.name
    try:
        text = _run_pdftotext(pdf_path)
    except Exception as exc:
        return SupplierInvoice(pdf_path, drive_file_id, drive_name, "", 0.0, "EUR", error=str(exc))

    date = _parse_date(text)
    currency = _detect_currency(text)
    amount = _parse_amount(text, currency)

    error = ""
    if not date:
        error = "date introuvable"
    elif amount <= 0:
        error = "montant introuvable"

    return SupplierInvoice(
        pdf_path, drive_file_id, drive_name, date, amount, currency,
        raw_text=text, error=error,
    )


def _load_manifest(input_dir: Path) -> List[SupplierInvoice]:
    """Repli quand pdftotext est absent : reports/fournisseurs/manifest.csv."""
    manifest = input_dir.parent / "manifest.csv"
    if not manifest.exists():
        return []
    invoices: List[SupplierInvoice] = []
    with manifest.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            path = input_dir / row["filename"]
            try:
                amount = float(str(row.get("amount", "")).replace(",", "."))
            except ValueError:
                amount = 0.0
            date = str(row.get("date", "")).strip()
            currency = (str(row.get("currency", "")).strip() or "EUR").upper()
            drive_id = str(row.get("drive_file_id", "")).strip()
            error = "" if (date and amount > 0) else "ligne manifest incomplète"
            invoices.append(SupplierInvoice(
                path, drive_id, path.name, date, amount, currency, error=error,
            ))
    return invoices


def load_invoices(input_dir: Path, id_map: Optional[Dict[str, str]] = None) -> List[SupplierInvoice]:
    """Charge les factures : manifest.csv si présent, sinon pdftotext sur PDF.

    id_map : {nom_fichier -> drive_file_id} pour rattacher l'ID Drive au PDF.
    """
    id_map = id_map or {}
    manifest_path = input_dir.parent / "manifest.csv"
    if manifest_path.exists():
        return _load_manifest(input_dir)
    pdfs = scan_input_dir(input_dir)
    if pdfs and _pdftotext_bin():
        return [parse_invoice(p, drive_file_id=id_map.get(p.name, "")) for p in pdfs]
    if not pdfs:
        raise SystemExit("Aucune facture fournisseur trouvée dans le dossier input.")
    raise SystemExit("pdftotext introuvable. Installer : brew install poppler")


# --------------------------------------------------------------------------- #
# Lecture des transactions Qonto
# --------------------------------------------------------------------------- #
def load_qonto_debits(json_path: Path) -> List[QontoDebit]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    debits: List[QontoDebit] = []
    for t in data:
        def _f(key: str) -> float:
            try:
                return abs(float(str(t.get(key, "0")).replace(",", ".")))
            except (TypeError, ValueError):
                return 0.0
        debits.append(QontoDebit(
            id=str(t.get("id", "")),
            label=str(t.get("label", "")),
            amount=_f("amount"),
            currency=str(t.get("currency") or "EUR"),
            local_amount=_f("local_amount"),
            local_currency=str(t.get("local_currency") or "").upper(),
            settled_at=str(t.get("settled_at") or t.get("date") or "")[:10],
            operation_type=str(t.get("operation_type") or ""),
        ))
    return debits


# --------------------------------------------------------------------------- #
# Rapprochement
# --------------------------------------------------------------------------- #
def _days_between(d1: str, d2: str) -> int:
    from datetime import date
    return abs((date.fromisoformat(d1) - date.fromisoformat(d2)).days)


def _amount_close(a: float, b: float) -> bool:
    return abs(round(a, 2) - round(b, 2)) <= AMOUNT_TOLERANCE


def _pick_closest(inv: SupplierInvoice, candidates: List[QontoDebit]) -> Optional[QontoDebit]:
    """Candidat le plus proche dans le temps."""
    if not candidates:
        return None
    return min(candidates, key=lambda t: _days_between(inv.date, t.settled_at) if t.settled_at else 9999)


def match(invoices: List[SupplierInvoice], debits: List[QontoDebit],
          warn_days: int) -> ReconciliationReport:
    """Rapprochement glouton en deux passes (EUR puis devise)."""
    report = ReconciliationReport()
    report.unmatched_invoices.extend(i for i in invoices if not i.is_valid)

    valid = sorted((i for i in invoices if i.is_valid), key=lambda i: i.date)
    used: set[str] = set()

    def _gap_filter(t: QontoDebit) -> bool:
        return bool(t.settled_at) and _days_between(inv.date, t.settled_at) <= warn_days

    # Passe 1 — factures EUR.
    for inv in (i for i in valid if not i.is_foreign):
        # (a) carte / prélèvement EUR (hors virements, traités en (b)).
        cands = [
            t for t in debits
            if t.id not in used
            and t.operation_type != "transfer"
            and t.local_currency in ("", "EUR")
            and _amount_close(t.amount, inv.amount)
            and _gap_filter(t)
        ]
        strategy = "eur_card"
        # (b) virement perso si pas de carte.
        if not cands:
            cands = [
                t for t in debits
                if t.id not in used
                and t.operation_type == "transfer"
                and any(term in t.label.lower() for term in PERSO_TERMS)
                and _amount_close(t.amount, inv.amount)
                and _gap_filter(t)
            ]
            strategy = "eur_transfer"
        best = _pick_closest(inv, cands)
        if best is None:
            report.unmatched_invoices.append(inv)
            continue
        gap = _days_between(inv.date, best.settled_at)
        report.matched.append(MatchResult(
            inv, best, gap, strategy, "exact" if gap <= warn_days else "warn",
        ))
        used.add(best.id)

    # Passe 2 — factures en devise étrangère (match sur local_amount/local_currency).
    for inv in (i for i in valid if i.is_foreign):
        cands = [
            t for t in debits
            if t.id not in used
            and t.local_currency == inv.currency.upper()
            and _amount_close(t.local_amount, inv.amount)
            and _gap_filter(t)
        ]
        best = _pick_closest(inv, cands)
        if best is None:
            report.unmatched_invoices.append(inv)
            continue
        gap = _days_between(inv.date, best.settled_at)
        report.matched.append(MatchResult(
            inv, best, gap, "foreign_currency", "exact" if gap <= warn_days else "warn",
        ))
        used.add(best.id)

    report.unmatched_transactions = [t for t in debits if t.id not in used]
    return report


# --------------------------------------------------------------------------- #
# Sorties
# --------------------------------------------------------------------------- #
def write_matches_json(report: ReconciliationReport, path: Path) -> None:
    payload = [
        {
            "invoice_path": str(m.invoice.path.resolve()),
            "drive_file_id": m.invoice.drive_file_id,
            "invoice_name": m.invoice.drive_name,
            "invoice_date": m.invoice.date,
            "invoice_amount": round(m.invoice.amount, 2),
            "invoice_currency": m.invoice.currency,
            "qonto_transaction_id": m.transaction.id,
            "qonto_date": m.transaction.settled_at,
            "match_strategy": m.match_strategy,
            "confidence": m.confidence,
        }
        for m in report.matched
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _fmt_amount(amount: float, currency: str) -> str:
    sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency.upper(), currency)
    return f"{amount:.2f} {sym}"


_STRATEGY_LABEL = {
    "eur_card": "carte EUR",
    "eur_transfer": "virement perso",
    "foreign_currency": "carte devise",
}


def write_reconciliation_report(report: ReconciliationReport, path: Path) -> None:
    n_ok = len(report.matched)
    n_exact = sum(1 for m in report.matched if m.confidence == "exact")
    n_warn = n_ok - n_exact
    out = ["# Rapprochement fournisseurs — factures ↔ transactions Qonto", ""]
    out.append(
        f"**{n_ok} facture(s) rapprochée(s)** "
        f"({n_exact} fiable(s), {n_warn} à vérifier), "
        f"**{len(report.unmatched_invoices)} facture(s)** sans transaction, "
        f"**{len(report.unmatched_transactions)} transaction(s)** sans facture."
    )
    out.append("")

    out.append("## Factures rapprochées")
    out.append("")
    out.append("| Facture | Montant | Stratégie | Date facture | Date Qonto | Écart (j) | Statut |")
    out.append("|---|---:|---|---|---|---:|---|")
    for m in sorted(report.matched, key=lambda m: m.invoice.date):
        status = "✅ exact" if m.confidence == "exact" else "⚠️ à vérifier"
        out.append(
            f"| {m.invoice.drive_name} | {_fmt_amount(m.invoice.amount, m.invoice.currency)} | "
            f"{_STRATEGY_LABEL.get(m.match_strategy, m.match_strategy)} | "
            f"{m.invoice.date} | {m.transaction.settled_at} | {m.date_gap_days} | {status} |"
        )
    out.append("")

    out.append("## Factures sans transaction")
    out.append("")
    out.append("| Facture | Montant | Date | Raison |")
    out.append("|---|---:|---|---|")
    for inv in sorted(report.unmatched_invoices, key=lambda i: (i.date, i.drive_name)):
        reason = inv.error or "aucune transaction correspondante"
        amount = _fmt_amount(inv.amount, inv.currency) if inv.amount > 0 else "—"
        out.append(f"| {inv.drive_name} | {amount} | {inv.date or '—'} | {reason} |")
    out.append("")

    out.append("## Transactions Qonto sans facture")
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
        f"_Généré par `scripts/reconcile_fournisseurs.py` le {report.run_date}. "
        f"Pièces jointes à attacher : voir `matches.json`._"
    )
    path.write_text("\n".join(out), encoding="utf-8")


def print_summary(report: ReconciliationReport) -> None:
    n_exact = sum(1 for m in report.matched if m.confidence == "exact")
    n_warn = len(report.matched) - n_exact
    print(
        f"{len(report.matched)} rapprochée(s) "
        f"({n_exact} fiable(s), {n_warn} à vérifier) · "
        f"{len(report.unmatched_invoices)} facture(s) orpheline(s) · "
        f"{len(report.unmatched_transactions)} transaction(s) orpheline(s)",
        file=sys.stderr,
    )
    for m in report.matched:
        flag = "  " if m.confidence == "exact" else "⚠ "
        print(
            f"  {flag}{m.invoice.drive_name[:32]:<32} "
            f"{_fmt_amount(m.invoice.amount, m.invoice.currency):>12} "
            f"[{_STRATEGY_LABEL.get(m.match_strategy, m.match_strategy)}] "
            f"↔ {m.transaction.settled_at} (écart {m.date_gap_days} j)",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rapproche les factures fournisseurs (Drive) avec les transactions Qonto."
    )
    parser.add_argument("--input", default="reports/fournisseurs/input")
    parser.add_argument("--transactions", default="reports/fournisseurs/qonto_debits.json")
    parser.add_argument("--out-dir", default="reports/fournisseurs")
    parser.add_argument("--since", default=DEFAULT_SINCE,
                        help=f"Ignore les factures avant cette date (défaut {DEFAULT_SINCE}).")
    parser.add_argument("--warn-days", type=int, default=DEFAULT_WARN_DAYS)
    parser.add_argument("--dry-run", action="store_true",
                        help="Affiche le parsing sans écrire de fichier.")
    args = parser.parse_args()

    input_dir = Path(args.input)
    out_dir = Path(args.out_dir)
    if not input_dir.exists():
        raise SystemExit(f"Dossier d'entrée introuvable : {input_dir}")

    invoices = load_invoices(input_dir)
    invoices = [i for i in invoices if not i.date or i.date >= args.since]

    if not invoices:
        print("Aucune facture fournisseur trouvée.", file=sys.stderr)
        return 0

    if args.dry_run:
        print(f"[dry-run] {len(invoices)} facture(s) lue(s) :", file=sys.stderr)
        for inv in invoices:
            note = f"  ⚠ {inv.error}" if inv.error else ""
            print(f"  {inv.drive_name[:36]:<36} {inv.date or '—':<12} "
                  f"{_fmt_amount(inv.amount, inv.currency):>12}{note}", file=sys.stderr)
        return 0

    transactions_path = Path(args.transactions)
    if not transactions_path.exists():
        raise SystemExit(
            f"Transactions Qonto introuvables : {transactions_path}\n"
            "Lancer d'abord run_fournisseurs.py (étape 2) pour les récupérer."
        )
    debits = load_qonto_debits(transactions_path)

    report = match(invoices, debits, args.warn_days)

    from datetime import datetime
    report.run_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    today = report.run_date[:10]

    out_dir.mkdir(parents=True, exist_ok=True)
    write_matches_json(report, out_dir / "matches.json")
    write_reconciliation_report(report, out_dir / f"reconciliation_{today}.md")

    print_summary(report)
    print(f"\nRapport : {out_dir / f'reconciliation_{today}.md'}", file=sys.stderr)
    print(f"À attacher : {out_dir / 'matches.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
