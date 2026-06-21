"""Détection des transactions sans facture rapprochée (le « backlog »).

Pour chaque transaction on suit la sous-ressource matched_invoices ; si elle est
vide, la transaction est à traiter. Les appels matched_invoices sont parallélisés.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional

from .pennylane_client import PennylaneClient


@dataclass
class UnmatchedTransaction:
    id: str
    date: str
    label: str
    amount: float          # négatif = dépense (sortie), positif = encaissement
    currency: str
    is_expense: bool

    @property
    def abs_amount(self) -> float:
        return abs(self.amount)


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def find_unmatched(
    client: PennylaneClient,
    since: Optional[str] = None,
    only_expenses: bool = True,
    max_workers: int = 4,
) -> List[UnmatchedTransaction]:
    """Renvoie les transactions sans facture rapprochée.

    `since` : ne garde que les transactions dont `date` >= since (filtre client).
    `only_expenses` : ne garde que les sorties d'argent (amount < 0).
    """
    txns = [t for t in client.iter_transactions() if _keep(t, since, only_expenses)]

    def needs_invoice(t):
        return None if client.has_matched_invoice(t) else t

    results: List[UnmatchedTransaction] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for t in pool.map(needs_invoice, txns):
            if t is None:
                continue
            amount = _to_float(t.get("amount"))
            results.append(
                UnmatchedTransaction(
                    id=str(t.get("id")),
                    date=str(t.get("date") or ""),
                    label=str(t.get("label") or ""),
                    amount=amount,
                    currency=str(t.get("currency") or "EUR"),
                    is_expense=amount < 0,
                )
            )
    results.sort(key=lambda x: (x.date, x.label))
    return results


def _keep(t: dict, since: Optional[str], only_expenses: bool) -> bool:
    if since and str(t.get("date") or "") < since:
        return False
    if only_expenses and _to_float(t.get("amount")) >= 0:
        return False
    return True
