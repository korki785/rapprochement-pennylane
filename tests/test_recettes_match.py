"""Rapprochement recettes : factures clients ↔ virements créditeurs.

Fige les règles métier des virements entrants :
  - SWIFT : montant reçu ≠ total facture (frais banque / arrondi FX) → bande tolérante.
  - Nom du client EXIGÉ dans le libellé (garde anti-faux-positif « bon montant, autre client »).
  - Auto-attache seulement exact ou SWIFT-unique ; ambiguïté → warn.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import reconcile_recettes as rr  # noqa: E402


def _inv(number, client, total, att="att-" + "x", issue="2026-06-01", status="unpaid"):
    return rr.ClientInvoice(
        id="inv-" + number, number=number, client_name=client, total=total,
        currency="EUR", status=status, attachment_id=att or "", issue_date=issue,
    )


def _credit(tx_id, label, amount, local=0.0, op="income", settled="2026-06-10", local_ccy=""):
    return rr.CreditTx(
        id=tx_id, label=label, amount=amount, currency="EUR",
        local_amount=local, local_currency=local_ccy, settled_at=settled,
        operation_type=op,
    )


def test_name_matches():
    assert rr.name_matches("ALLEGRIA INC", "ALLEGRIA, INC")
    assert rr.name_matches("SEE YOU SOON", "SEE YOU SOON")
    assert not rr.name_matches("ALLEGRIA INC", "SGC PERPIGNAN")
    assert not rr.name_matches("", "N'IMPORTE QUOI")


def test_exact_domestic():
    """See You Soon : virement EUR domestique, montant exact -> exact, auto-attaché."""
    invs = [_inv("F-2026-003", "SEE YOU SOON", 7657.50, att="att-syn", issue="2026-01-26")]
    cr = [_credit("tx1", "SEE YOU SOON", 7657.50, op="income", settled="2026-05-18")]
    rep = rr.match(invs, cr)
    assert len(rep.matched) == 1
    m = rep.matched[0]
    assert m.confidence == "exact" and m.confidence in rr.AUTO_ATTACH


def test_swift_fee_deducted_unique():
    """Allegria : SWIFT, frais 40€ déduits, mais local_amount = total exact -> exact."""
    invs = [_inv("F-2026-007", "ALLEGRIA INC", 10514.56, att="att-a7")]
    cr = [_credit("tx2", "ALLEGRIA, INC", 10474.56, local=10514.56,
                  op="swift_income", settled="2026-06-05", local_ccy="EUR")]
    rep = rr.match(invs, cr)
    assert len(rep.matched) == 1
    assert rep.matched[0].confidence == "exact"   # local_amount tombe pile


def test_swift_rounding_unique():
    """Allegria : SWIFT, +0,06 d'arrondi FX, aucun montant exact -> swift (unique)."""
    invs = [_inv("F-2026-010", "ALLEGRIA INC", 1817.94, att="att-a10", issue="2026-06-23")]
    cr = [_credit("tx3", "ALLEGRIA, INC", 1818.00, local=1833.00,
                  op="swift_income", settled="2026-07-01", local_ccy="EUR")]
    rep = rr.match(invs, cr)
    assert len(rep.matched) == 1
    m = rep.matched[0]
    assert m.match_strategy == "swift" and m.confidence == "swift"
    assert m.confidence in rr.AUTO_ATTACH


def test_false_positive_other_client_rejected():
    """Même montant mais nom de client différent -> pas de rapprochement."""
    invs = [_inv("F-2026-010", "ALLEGRIA INC", 1818.00, att="att-a10")]
    cr = [_credit("tx4", "SGC PERPIGNAN", 1818.00, op="income")]
    rep = rr.match(invs, cr)
    assert rep.matched == []
    assert len(rep.unmatched_transactions) == 1


def test_ambiguous_two_invoices_warn():
    """Deux factures du même client, même montant, écart non exact -> warn (pas d'auto)."""
    invs = [
        _inv("F-A", "ALLEGRIA INC", 1000.00, att="att-a"),
        _inv("F-B", "ALLEGRIA INC", 1000.00, att="att-b"),
    ]
    # SWIFT avec 30€ de frais : deux factures candidates -> pas unique -> warn.
    cr = [_credit("tx5", "ALLEGRIA, INC", 970.00, op="swift_income")]
    rep = rr.match(invs, cr)
    assert len(rep.matched) == 1
    assert rep.matched[0].confidence == "warn"
    assert rep.matched[0].confidence not in rr.AUTO_ATTACH


def test_out_of_band_rejected():
    """Frais > FEE_MAX (bande dépassée) -> pas candidat."""
    invs = [_inv("F-X", "ALLEGRIA INC", 1000.00, att="att-x")]
    cr = [_credit("tx6", "ALLEGRIA, INC", 900.00, op="swift_income")]  # -100 > 45
    rep = rr.match(invs, cr)
    assert rep.matched == []


def test_payment_cannot_precede_invoice():
    """Un virement bien avant l'émission n'est pas un paiement de cette facture."""
    invs = [_inv("F-Y", "ALLEGRIA INC", 500.00, att="att-y", issue="2026-06-20")]
    cr = [_credit("tx7", "ALLEGRIA, INC", 500.00, op="income", settled="2026-05-01")]
    rep = rr.match(invs, cr)
    assert rep.matched == []


def test_invoice_without_pdf_ignored():
    """Facture sans attachment_id (pas de PDF) -> non valide, jamais rapprochée."""
    invs = [_inv("F-Z", "ALLEGRIA INC", 500.00, att="")]
    cr = [_credit("tx8", "ALLEGRIA, INC", 500.00, op="income")]
    rep = rr.match(invs, cr)
    assert rep.matched == []
