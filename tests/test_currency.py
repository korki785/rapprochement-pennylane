"""Tests de la logique multi-devises (pur, sans réseau).

Couvre : détection de devise étrangère, calcul du taux de change réel depuis les
montants Qonto, et construction du payload d'import Pennylane.
"""
from recon.pennylane_client import build_invoice_payload
from recon.transactions import UnmatchedTransaction


def _txn(**kw) -> UnmatchedTransaction:
    base = dict(id="tx_1", date="2026-04-15", label="SOUTHWEST AIRLINES",
                amount=-299.68, currency="EUR", is_expense=True,
                local_amount=-314.40, local_currency="USD")
    base.update(kw)
    return UnmatchedTransaction(**base)


def test_foreign_currency_detected():
    assert _txn().is_foreign_currency is True


def test_same_currency_not_foreign():
    t = _txn(local_amount=-299.68, local_currency="EUR")
    assert t.is_foreign_currency is False


def test_no_local_currency_not_foreign():
    t = _txn(local_amount=0.0, local_currency="")
    assert t.is_foreign_currency is False


def test_exchange_rate_eur_per_usd():
    # 299.68 EUR pour 314.40 USD -> 0.9532 EUR par USD
    assert round(_txn().exchange_rate, 4) == 0.9532


def test_exchange_rate_defaults_to_one_when_domestic():
    t = _txn(local_amount=-299.68, local_currency="EUR")
    assert t.exchange_rate == 1.0


def test_payload_foreign_includes_currency_and_rate():
    payload = build_invoice_payload("fa_123", _txn())
    assert payload["file_attachment_id"] == "fa_123"
    assert payload["transaction_reference"] == "tx_1"
    assert payload["currency"] == "USD"
    assert payload["amount"] == 314.40              # montant USD positif
    assert round(payload["exchange_rate"], 4) == 0.9532


def test_payload_uses_invoice_amount_when_provided():
    # Le montant de la facture (extrait email/PDF) prime sur le montant Qonto.
    payload = build_invoice_payload("fa_123", _txn(), amount_foreign=314.40)
    assert payload["amount"] == 314.40


def test_payload_domestic_has_no_currency_fields():
    t = _txn(local_amount=-50.0, local_currency="EUR", amount=-50.0)
    payload = build_invoice_payload("fa_9", t)
    assert "currency" not in payload
    assert "exchange_rate" not in payload
    assert payload["transaction_reference"] == "tx_1"
