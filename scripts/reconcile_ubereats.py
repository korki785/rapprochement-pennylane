#!/usr/bin/env python3
"""Rapprochement des factures UberEats avec les remboursements Qonto.

L'utilisateur paie UberEats avec sa carte perso ; l'entreprise rembourse par
virement Qonto à « Nael Darwish ». Ce script associe chaque facture PDF
(téléchargée depuis le portail UberEats) au virement de remboursement Qonto,
puis produit un rapport et un fichier `matches.json` consommé par Claude pour
attacher les PDF aux transactions via le MCP Qonto.

Usage :
    python3 scripts/reconcile_ubereats.py \\
        --input     reports/ubereats/input \\
        --transfers reports/ubereats/qonto_transfers.json \\
        --out-dir   reports/ubereats \\
        [--since YYYY-MM-DD] [--warn-days 7] [--dry-run]

Sans pdftotext (poppler) : fournir reports/ubereats/manifest.csv en repli.
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
from typing import List, Optional

# Permet `python3 scripts/reconcile_ubereats.py` sans installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

DEFAULT_SINCE = "2026-04-01"
DEFAULT_WARN_DAYS = 7
AMOUNT_TOLERANCE = 0.01

# Mois français pour le parsing des dates longues (« 15 mai 2026 »).
_FR_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}


# --------------------------------------------------------------------------- #
# Structures de données
# --------------------------------------------------------------------------- #
@dataclass
class UberEatsInvoice:
    path: Path
    invoice_id: str
    date: str           # ISO YYYY-MM-DD ("" si introuvable)
    amount: float       # EUR (0.0 si introuvable)
    raw_text: str = ""
    error: str = ""     # note d'erreur de parsing (sinon "")

    @property
    def is_valid(self) -> bool:
        return not self.error and bool(self.date) and self.amount > 0


@dataclass
class QontoTransfer:
    id: str
    label: str
    amount: float       # positif
    date: str           # ISO YYYY-MM-DD


@dataclass
class MatchResult:
    invoice: UberEatsInvoice
    transfer: QontoTransfer
    date_gap_days: int
    confidence: str     # "exact" | "amount_only"


@dataclass
class ReconciliationReport:
    matched: List[MatchResult] = field(default_factory=list)
    unmatched_invoices: List[UberEatsInvoice] = field(default_factory=list)
    unmatched_transfers: List[QontoTransfer] = field(default_factory=list)
    run_date: str = ""


# --------------------------------------------------------------------------- #
# Lecture des PDF
# --------------------------------------------------------------------------- #
def scan_input_dir(input_dir: Path) -> List[Path]:
    """Tous les *.pdf du dossier, triés par nom."""
    return sorted(input_dir.glob("*.pdf"))


def _pdftotext_bin() -> Optional[str]:
    """Cherche pdftotext dans PATH et chemins Homebrew connus."""
    found = shutil.which("pdftotext")
    if found:
        return found
    for candidate in ["/opt/homebrew/bin/pdftotext", "/usr/local/bin/pdftotext"]:
        if Path(candidate).exists():
            return candidate
    return None


def _run_pdftotext(path: Path) -> str:
    """Extrait le texte d'un PDF via `pdftotext -layout` (poppler)."""
    bin_path = _pdftotext_bin() or "pdftotext"
    proc = subprocess.run(
        [bin_path, "-layout", str(path), "-"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        raise ValueError(f"pdftotext a échoué : {proc.stderr.strip()}")
    return proc.stdout


def _parse_date(text: str) -> str:
    """Renvoie la première date trouvée au format ISO, sinon ""."""
    # ISO YYYY-MM-DD
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # Français numérique DD/MM/YYYY
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    # Français long « 15 mai 2026 »
    m = re.search(r"\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})\b", text)
    if m:
        mo = _FR_MONTHS.get(m.group(2).lower())
        if mo:
            return f"{int(m.group(3)):04d}-{mo:02d}-{int(m.group(1)):02d}"
    return ""


def _parse_amount(text: str) -> float:
    """Renvoie le total TTC en EUR depuis un reçu UberEats, sinon 0.0.

    Les PDFs UberEats contiennent toute la page commandes (liste + modal reçu).
    Stratégie : chercher le montant près de l'info de paiement (carte bancaire),
    qui correspond au vrai total payé.
    """
    # 1. Montant près du libellé de paiement (Mastercard, Visa, carte…).
    m = re.search(
        r"(?:Mastercard|Visa|Carte|carte|Paiements?|payment)[^\d\n]{0,40}(\d{1,4}[.,]\d{2})\s*€",
        text, re.IGNORECASE
    )
    if m:
        return float(m.group(1).replace(",", "."))

    # 2. Ligne commençant par "Total" (montant chargé) + montant même ligne OU ligne suivante.
    #    Ancrage début de ligne pour EXCLURE "Sous-total des articles" (montant brut).
    #    Ex. layout : "Total                         16,75 €"  (≠ "Sous-total … 27,80 €").
    m = re.search(r"(?:^|\n)\s*Total\b[^\n\d]*?(\d{1,4}[.,]\d{2})\s*€", text, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", "."))

    # 3. Montant qui apparaît au moins deux fois (total = confirmation paiement).
    cands = re.findall(r"(\d{1,4}[.,]\d{2})\s*€", text)
    if cands:
        from collections import Counter
        counts = Counter(cands)
        repeated = [c for c, n in counts.items() if n >= 2]
        if repeated:
            return max(float(c.replace(",", ".")) for c in repeated)
        # Repli : plus petit montant supérieur à 5€ (exclut frais de livraison isolés).
        values = sorted(set(float(c.replace(",", ".")) for c in cands))
        plausible = [v for v in values if v > 5.0]
        if plausible:
            # Prendre la médiane pour éviter les grosses sommes parasites.
            return plausible[len(plausible) // 2]
    return 0.0


def _parse_invoice_id(text: str, fallback: str) -> str:
    m = re.search(
        r"(?:facture\s*n[°o]?|invoice\s*#?)\s*:?\s*([A-Z0-9][A-Z0-9\-]{4,})",
        text, re.IGNORECASE,
    )
    return m.group(1) if m else fallback


def parse_invoice(pdf_path: Path) -> UberEatsInvoice:
    """Parse un PDF UberEats. En cas d'échec : champ `error` rempli."""
    try:
        text = _run_pdftotext(pdf_path)
    except Exception as exc:  # pdftotext absent ou PDF illisible
        return UberEatsInvoice(pdf_path, pdf_path.stem, "", 0.0, error=str(exc))

    date = _parse_date(text)
    amount = _parse_amount(text)
    invoice_id = _parse_invoice_id(text, pdf_path.stem)

    error = ""
    if not date:
        error = "date introuvable"
    elif amount <= 0:
        error = "montant introuvable"

    return UberEatsInvoice(pdf_path, invoice_id, date, amount, raw_text=text, error=error)


def _load_manifest(input_dir: Path) -> List[UberEatsInvoice]:
    """Repli quand pdftotext est absent : lit reports/ubereats/manifest.csv."""
    manifest = input_dir.parent / "manifest.csv"
    if not manifest.exists():
        return []
    invoices: List[UberEatsInvoice] = []
    with manifest.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            path = input_dir / row["filename"]
            try:
                amount = float(str(row.get("amount", "")).replace(",", "."))
            except ValueError:
                amount = 0.0
            date = str(row.get("date", "")).strip()
            inv_id = str(row.get("invoice_id", "")).strip() or path.stem
            error = "" if (date and amount > 0) else "ligne manifest incomplète"
            invoices.append(UberEatsInvoice(path, inv_id, date, amount, error=error))
    return invoices


def load_invoices(input_dir: Path) -> List[UberEatsInvoice]:
    """Charge les factures : manifest.csv si présent, sinon pdftotext sur PDF."""
    manifest_path = input_dir.parent / "manifest.csv"
    if manifest_path.exists():
        return _load_manifest(input_dir)
    pdfs = scan_input_dir(input_dir)
    if pdfs and _pdftotext_bin():
        return [parse_invoice(p) for p in pdfs]
    if not pdfs:
        raise SystemExit("Aucune facture UberEats trouvée dans le dossier input.")
    raise SystemExit(
        "pdftotext introuvable. Installer : brew install poppler"
    )


# --------------------------------------------------------------------------- #
# Lecture des virements Qonto
# --------------------------------------------------------------------------- #
def load_qonto_transfers(json_path: Path) -> List[QontoTransfer]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    transfers: List[QontoTransfer] = []
    for t in data:
        try:
            amount = abs(float(str(t.get("amount", "0")).replace(",", ".")))
        except (TypeError, ValueError):
            amount = 0.0
        date = str(t.get("settled_at") or t.get("date") or "")[:10]
        transfers.append(QontoTransfer(
            id=str(t.get("id", "")),
            label=str(t.get("label", "")),
            amount=amount,
            date=date,
        ))
    return transfers


# --------------------------------------------------------------------------- #
# Rapprochement
# --------------------------------------------------------------------------- #
def _days_between(d1: str, d2: str) -> int:
    """Écart absolu en jours entre deux dates ISO."""
    from datetime import date
    a = date.fromisoformat(d1)
    b = date.fromisoformat(d2)
    return abs((a - b).days)


def match(invoices: List[UberEatsInvoice], transfers: List[QontoTransfer],
          warn_days: int) -> ReconciliationReport:
    """Assignation bipartie gloutonne (cardinalité faible)."""
    report = ReconciliationReport()

    valid = sorted((i for i in invoices if i.is_valid), key=lambda i: i.date)
    report.unmatched_invoices.extend(i for i in invoices if not i.is_valid)

    used: set[str] = set()
    for inv in valid:
        candidates = [
            t for t in transfers
            if t.id not in used
            and abs(round(t.amount, 2) - round(inv.amount, 2)) <= AMOUNT_TOLERANCE
        ]
        if not candidates:
            report.unmatched_invoices.append(inv)
            continue
        # Le virement le plus proche dans le temps gagne.
        candidates.sort(key=lambda t: _days_between(inv.date, t.date))
        best = candidates[0]
        gap = _days_between(inv.date, best.date)
        confidence = "exact" if gap <= warn_days else "amount_only"
        report.matched.append(MatchResult(inv, best, gap, confidence))
        used.add(best.id)

    report.unmatched_transfers = [t for t in transfers if t.id not in used]
    return report


# --------------------------------------------------------------------------- #
# Sorties
# --------------------------------------------------------------------------- #
def write_matches_json(report: ReconciliationReport, path: Path) -> None:
    payload = [
        {
            "invoice_path": str(m.invoice.path.resolve()),
            "invoice_id": m.invoice.invoice_id,
            "invoice_date": m.invoice.date,
            "invoice_amount": round(m.invoice.amount, 2),
            "qonto_transaction_id": m.transfer.id,
            "qonto_date": m.transfer.date,
            "confidence": m.confidence,
        }
        for m in report.matched
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_reconciliation_report(report: ReconciliationReport, path: Path) -> None:
    n_ok = len(report.matched)
    n_exact = sum(1 for m in report.matched if m.confidence == "exact")
    n_warn = n_ok - n_exact
    out = ["# Rapprochement UberEats — factures ↔ remboursements Qonto", ""]
    out.append(
        f"**{n_ok} facture(s) rapprochée(s)** "
        f"({n_exact} fiable(s), {n_warn} à vérifier), "
        f"**{len(report.unmatched_invoices)} facture(s)** sans virement, "
        f"**{len(report.unmatched_transfers)} virement(s)** sans facture."
    )
    out.append("")

    out.append("## Factures rapprochées")
    out.append("")
    out.append("| Facture | Montant | Date facture | Date virement | Écart (j) | Statut |")
    out.append("|---|---:|---|---|---:|---|")
    for m in sorted(report.matched, key=lambda m: m.invoice.date):
        status = "✅ exact" if m.confidence == "exact" else "⚠️ montant seul"
        out.append(
            f"| {m.invoice.invoice_id} | {m.invoice.amount:.2f} € | "
            f"{m.invoice.date} | {m.transfer.date} | {m.date_gap_days} | {status} |"
        )
    out.append("")

    out.append("## Factures sans virement")
    out.append("")
    out.append("| Facture | Montant | Date | Raison |")
    out.append("|---|---:|---|---|")
    for inv in sorted(report.unmatched_invoices, key=lambda i: (i.date, i.invoice_id)):
        reason = inv.error or "aucun virement correspondant"
        amount = f"{inv.amount:.2f} €" if inv.amount > 0 else "—"
        out.append(f"| {inv.invoice_id} | {amount} | {inv.date or '—'} | {reason} |")
    out.append("")

    out.append("## Virements Qonto sans facture")
    out.append("")
    out.append("| Libellé | Montant | Date | ID transaction |")
    out.append("|---|---:|---|---|")
    for t in sorted(report.unmatched_transfers, key=lambda t: t.date):
        out.append(f"| {t.label} | {t.amount:.2f} € | {t.date} | {t.id} |")
    out.append("")

    out.append("---")
    out.append(
        f"_Généré par `scripts/reconcile_ubereats.py` le {report.run_date}. "
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
        f"{len(report.unmatched_transfers)} virement(s) orphelin(s)",
        file=sys.stderr,
    )
    for m in report.matched:
        flag = "  " if m.confidence == "exact" else "⚠ "
        print(
            f"  {flag}{m.invoice.invoice_id:<16} {m.invoice.amount:>8.2f} € "
            f"↔ {m.transfer.date} (écart {m.date_gap_days} j)",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rapproche les factures UberEats avec les remboursements Qonto."
    )
    parser.add_argument("--input", default="reports/ubereats/input",
                        help="Dossier des PDF UberEats.")
    parser.add_argument("--transfers", default="reports/ubereats/qonto_transfers.json",
                        help="JSON des virements Qonto (écrit par Claude).")
    parser.add_argument("--out-dir", default="reports/ubereats",
                        help="Dossier de sortie (rapport + matches.json).")
    parser.add_argument("--since", default=DEFAULT_SINCE,
                        help=f"Ignore les factures avant cette date (défaut {DEFAULT_SINCE}).")
    parser.add_argument("--warn-days", type=int, default=DEFAULT_WARN_DAYS,
                        help=f"Écart max facture↔virement avant alerte (défaut {DEFAULT_WARN_DAYS}).")
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
        print("Aucune facture UberEats trouvée.", file=sys.stderr)
        return 0

    if args.dry_run:
        print(f"[dry-run] {len(invoices)} facture(s) lue(s) :", file=sys.stderr)
        for inv in invoices:
            note = f"  ⚠ {inv.error}" if inv.error else ""
            print(f"  {inv.invoice_id:<16} {inv.date or '—':<12} "
                  f"{inv.amount:>8.2f} €{note}", file=sys.stderr)
        return 0

    transfers_path = Path(args.transfers)
    if not transfers_path.exists():
        raise SystemExit(
            f"Virements Qonto introuvables : {transfers_path}\n"
            "Étape A : Claude doit d'abord récupérer les virements via le MCP Qonto."
        )
    transfers = load_qonto_transfers(transfers_path)

    report = match(invoices, transfers, args.warn_days)

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
