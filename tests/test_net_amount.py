"""Régression : le rapprochement doit cibler le montant NET réellement débité, JAMAIS le
« Total » brut. Deux incidents réels verrouillés ici (Total ≠ débit carte) :

- Bolt (facture PDF, promo/voucher) : « Total TTC 20,00 » puis « Facturé Apple Pay 16,00 »
  → le débit carte = 16,00.
- Resto Le Pschill (scan Drive N0569, addition partagée + pourboire) : « Total 30,70 »,
  « déjà payé 7,00 », « pourboire -1,00 » → « Reste à payer 24,70 » = le débit carte.

Si un de ces asserts casse, un justificatif RÉEL redeviendrait « non rapproché » à tort
(exactement le bug corrigé le 2026-07-02). Ces tests sont le garde-fou permanent.
"""
import sys
from pathlib import Path

# scripts/ n'est pas un package : on l'ajoute au path (src/ y est déjà via pyproject pythonpath).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from recon import saas                      # noqa: E402
import reconcile_fournisseurs as rf         # noqa: E402
import audit_unreconciled as audit          # noqa: E402


# Extraits fidèles du texte réellement extrait des deux justificatifs.
BOLT_TXT = """\
Frais de trajet                 18.18        1.82        20.00
Sous-total (EUR):             18.18
Total TTC (EUR):              20.00
Payé par FRTRUMA9QBDAA6M-1:                          -4.00
Facturé           Apple Pay:               16.00
"""

PSCHILL_TXT = """\
1 Salade thaï                 18,70
Total : 30,70 €
Déjà payé : - CB 7,00
Pourboire CB -1,00
Reste à payer : 24,70 €
"""


def test_bolt_net_not_gross():
    # 16,00 (Facturé), surtout PAS 20,00 (Total TTC brut).
    assert saas.extract_amount(BOLT_TXT) == 16.0


def test_pschill_reste_a_payer_is_primary():
    cands = rf._amount_candidates(PSCHILL_TXT)
    assert cands[0] == 24.7          # net = candidat prioritaire
    assert 24.7 in cands
    assert 30.7 in cands             # brut conservé en repli, non prioritaire


def test_reste_a_payer_ocr_variants():
    # Tolérance OCR : accent/espace optionnels, casse, famille du solde restant.
    for line in ("Reste à payer : 24,70 €", "Reste a payer 24,70",
                 "Reste  à  payer   24,70 €", "RESTE À PAYER 24,70",
                 "Solde à payer : 24,70", "Net à régler 24,70 EUR", "Reste dû 24,70"):
        assert rf._parse_amount(line) == 24.7, line


def test_no_false_positive_on_prose():
    # Aucun montant sur la ligne → aucun candidat (ne capte pas « le reste du groupe »).
    assert rf._amount_candidates("le reste du groupe arrive demain") == []
    assert rf._amount_candidates("à régler avant le concert") == []


def test_date_two_digit_year():
    # Villa Duflot : facture d'acompte datée « 02.07.26 » (DD.MM.YY). Sans ce format,
    # date=None → pas de fenêtre → jamais rapproché malgré montant exact.
    assert saas.extract_date("1  02.07.26   Acompte 30% séjour") == "2026-07-02"
    assert saas.extract_date("Facture du 09/06/25") == "2025-06-09"


def test_date_four_digit_still_works():
    # Non-régression des formats déjà gérés.
    assert saas.extract_date("2026-07-02") == "2026-07-02"
    assert saas.extract_date("02/07/2026") == "2026-07-02"
    assert saas.extract_date("25 April 2026") == "2026-04-25"


def test_date_not_fooled_by_amounts_or_versions():
    # « 1.70 18.70 » (TVA/prix, 2 groupes) et versions « 2.52.103 » ne sont pas des dates.
    assert saas.extract_date("TVA 1,70   18,70") is None
    assert saas.extract_date("build v2.52.103") is None
    # mois > 12 rejeté (pas une date DD.MM.YY plausible).
    assert saas.extract_date("ref 30.99.12 lot") is None


def test_audit_safety_net_detects_net_amount():
    # Le filet anti-faux-positif du récap reconnaît 24,70 comme total payé (près de « Reste
    # à payer ») → ne déclarera pas la transaction « non rapprochée » à tort.
    assert audit._pdf_has_total(PSCHILL_TXT, ["24,70", "24.70"]) is True
    assert audit._pdf_has_total(BOLT_TXT, ["16.00", "16,00"]) is True
