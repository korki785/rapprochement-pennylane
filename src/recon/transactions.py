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
    amount: float          # négatif = dépense (sortie), positif = encaissement ; en `currency`
    currency: str          # devise de règlement du compte (typiquement EUR)
    is_expense: bool
    local_amount: float = 0.0      # montant dans la devise d'origine (ce que le marchand a facturé)
    local_currency: str = ""       # devise d'origine : "USD", "GBP"… ("" = inconnue)

    @property
    def abs_amount(self) -> float:
        return abs(self.amount)

    @property
    def is_foreign_currency(self) -> bool:
        """Vrai si la transaction a été facturée dans une devise ≠ devise du compte."""
        lc = self.local_currency.upper()
        return bool(lc) and lc != self.currency.upper()

    @property
    def exchange_rate(self) -> float:
        """Taux réel `currency`/`local_currency` calculé depuis les montants Qonto.

        Ex : amount=-299.68 EUR pour local_amount=-314.40 USD -> 0.9532 (EUR par USD).
        Renvoie 1.0 si pas de devise étrangère ou données manquantes.
        """
        if self.is_foreign_currency and self.local_amount:
            return abs(self.amount) / abs(self.local_amount)
        return 1.0


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# Clés possibles selon la source (Qonto vs Pennylane v2) pour le montant/devise d'origine.
_LOCAL_AMOUNT_KEYS = ("local_amount", "currency_amount", "original_amount", "amount_currency")
_LOCAL_CURRENCY_KEYS = ("local_currency", "currency_amount_currency", "original_currency")


def _first_present(t: dict, keys) -> Optional[str]:
    for k in keys:
        v = t.get(k)
        if v not in (None, ""):
            return v
    return None


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
            currency = str(t.get("currency") or "EUR")
            local_amount = _to_float(_first_present(t, _LOCAL_AMOUNT_KEYS))
            local_currency = str(_first_present(t, _LOCAL_CURRENCY_KEYS) or "")
            results.append(
                UnmatchedTransaction(
                    id=str(t.get("id")),
                    date=str(t.get("date") or ""),
                    label=str(t.get("label") or ""),
                    amount=amount,
                    currency=currency,
                    is_expense=amount < 0,
                    local_amount=local_amount,
                    local_currency=local_currency,
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
