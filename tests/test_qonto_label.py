"""Rapprochement piloté par la transaction : extraction du marchand depuis le libellé Qonto,
construction de la requête Gmail, et garde-fou de vérification (montant-total ET nom).

Fige : le nom de recherche vient du LIBELLÉ (pas d'une liste blanche), et on n'attache jamais
sur le seul montant (le jeton marchand doit aussi apparaître).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from recon.normalize import label_merchant_tokens  # noqa: E402
import reconcile_qonto as rq  # noqa: E402
import audit_unreconciled as audit  # noqa: E402


def test_label_merchant_tokens():
    assert label_merchant_tokens("UBR* PENDING.UBER.COM") == ["UBER"]
    assert label_merchant_tokens("SUMUP *LE PSCHILL") == ["PSCHILL"]
    assert label_merchant_tokens("A LA DAUPHINE") == ["DAUPHINE"]
    assert label_merchant_tokens("GC RE AIRCALL") == ["AIRCALL"]
    assert label_merchant_tokens("SQ *CERTIFIED CAFE") == ["CERTIFIED", "CAFE"]
    assert label_merchant_tokens("PAIEMENT") == []          # aucun mot exploitable -> tx sautée


def test_build_receipt_query_shape():
    q = rq.build_receipt_query(["UBER"], ["15.04", "15,04"], "2026/06/25", "2026/07/06")
    assert "(15.04 OR 15,04)" in q
    assert "(UBER)" in q
    assert "after:2026/06/25" in q and "before:2026/07/06" in q
    assert "-from:qonto.com" in q                            # exclusions réutilisées
    assert '"' not in q                                      # PAS de guillemet interne (bug X-GM-RAW)


def _amt_strs(amount, local=None):
    tx = {"amount": amount, "local_amount": local}
    return audit._amount_strings(audit._amount_targets(tx))


PDF_OK = "LE PSCHILL - PARIS SAINT CLOUD\nSalade thai 18,70\nTotal TTC : 24,70 EUR\n"


def test_verify_accepts_amount_total_and_name():
    assert rq.verify_pdf(PDF_OK, _amt_strs(24.70), ["PSCHILL"], "caisse@pschill.fr", "Ticket") is True


def test_verify_rejects_when_name_absent():
    # Bon montant-total mais le jeton marchand du libellé n'apparaît nulle part -> refus.
    assert rq.verify_pdf(PDF_OK, _amt_strs(24.70), ["UBER"], "caisse@pschill.fr", "Ticket") is False


def test_verify_rejects_when_amount_not_a_total():
    # Le nom est présent mais le montant cible n'est pas un total du document -> refus.
    assert rq.verify_pdf(PDF_OK, _amt_strs(99.99), ["PSCHILL"], "caisse@pschill.fr", "Ticket") is False


def test_verify_name_can_come_from_sender_or_subject():
    txt = "Recu de votre course\nTotal 15,04 €\n"             # pas de "UBER" dans le corps…
    assert rq.verify_pdf(txt, _amt_strs(15.04), ["UBER"], "receipts@uber.com", "Uber") is True
