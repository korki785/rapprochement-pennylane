"""Construction et rendu de la todo-list : transactions sans facture, par fournisseur."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .config import SupplierRule
from .normalize import group_labels, normalize_label, ratio
from .transactions import UnmatchedTransaction

_SOURCE_LABEL = {
    "email": "📧 arrive par email (vérifier le filtre Gmail)",
    "portal": "🔐 à télécharger sur le portail",
    "manual": "✋ à récupérer manuellement",
}


@dataclass
class SupplierLine:
    supplier: str
    count: int
    total: float
    currency: str
    source: str
    url: str
    note: str
    transactions: List[UnmatchedTransaction] = field(default_factory=list)


def _match_rule(normalized: str, rules: List[SupplierRule],
                threshold: float = 80.0) -> Optional[SupplierRule]:
    best, best_score = None, 0.0
    for rule in rules:
        # match si le pattern est contenu dans le libellé OU fuzzy proche
        score = 100.0 if rule.pattern in normalized else ratio(normalized, rule.pattern)
        if score > best_score:
            best, best_score = rule, score
    return best if best and best_score >= threshold else None


def build_report(transactions: List[UnmatchedTransaction],
                 rules: List[SupplierRule]) -> List[SupplierLine]:
    """Regroupe les transactions sans facture par fournisseur deviné."""
    groups = group_labels([t.label for t in transactions])
    lines: List[SupplierLine] = []
    for g in groups:
        members = [transactions[i] for i in g.indices]
        rule = _match_rule(g.canonical, rules)
        currency = members[0].currency if members else "EUR"
        lines.append(
            SupplierLine(
                supplier=g.canonical or "(inconnu)",
                count=len(members),
                total=sum(m.abs_amount for m in members),
                currency=currency,
                source=rule.source if rule else "unknown",
                url=rule.url if rule else "",
                note=rule.note if rule else "",
                transactions=sorted(members, key=lambda m: m.date),
            )
        )
    # Les plus gros postes d'abord (nombre puis montant).
    lines.sort(key=lambda l: (l.count, l.total), reverse=True)
    return lines


def render_markdown(lines: List[SupplierLine]) -> str:
    total_tx = sum(l.count for l in lines)
    out = ["# Backlog — transactions sans facture rapprochée", ""]
    out.append(f"**{total_tx} transactions** à traiter, réparties sur **{len(lines)} fournisseurs**.")
    out.append("")
    out.append("| Fournisseur | Nb | Montant total | Où chercher | Lien |")
    out.append("|---|---:|---:|---|---|")
    for l in lines:
        src = _SOURCE_LABEL.get(l.source, "❓ source inconnue")
        link = l.url or ""
        out.append(f"| {l.supplier} | {l.count} | {l.total:.2f} {l.currency} | {src} | {link} |")
    out.append("")
    out.append("## Détail par fournisseur")
    for l in lines:
        out.append("")
        out.append(f"### {l.supplier} — {l.count} transaction(s), {l.total:.2f} {l.currency}")
        if l.note:
            out.append(f"> {l.note}")
        out.append("")
        out.append("| Date | Montant | Devise d'origine | Libellé | ID transaction |")
        out.append("|---|---:|---|---|---|")
        for t in l.transactions:
            if t.is_foreign_currency:
                origin = f"{t.local_amount:.2f} {t.local_currency} @ {t.exchange_rate:.4f}"
            else:
                origin = "—"
            out.append(f"| {t.date} | {t.amount:.2f} {t.currency} | {origin} | {t.label} | {t.id} |")
    return "\n".join(out)


def render_console(lines: List[SupplierLine]) -> str:
    total_tx = sum(l.count for l in lines)
    out = [f"{total_tx} transactions sans facture, sur {len(lines)} fournisseurs :", ""]
    width = max((len(l.supplier) for l in lines), default=10)
    for l in lines:
        src = {"email": "email", "portal": "PORTAIL", "manual": "manuel",
               "unknown": "?"}.get(l.source, "?")
        out.append(f"  {l.supplier.ljust(width)}  {str(l.count).rjust(3)} tx  "
                   f"{l.total:10.2f} {l.currency}  [{src}]")
    return "\n".join(out)
