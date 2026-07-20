"""Justificatifs PHOTOGRAPHIÉS et remboursements GROUPÉS (régressions du 2026-07-20).

Trois bugs figés ici :

1. **Repli photo** — un reçu pris en photo (JPEG, corps de mail vide) ne porte son montant que
   dans les PIXELS. L'index texte de Gmail ne peut pas le trouver → la recherche `(montant) ET
   (jeton)` ne remonte rien → le mail n'est JAMAIS téléchargé et l'OCR n'a jamais sa chance.
   (L'OCR lui-même a toujours fonctionné : le diagnostic « le modèle ne lit pas les JPEG » était
   faux.)
2. **Somme de justificatifs** — un remboursement unique peut couvrir PLUSIEURS factures réunies
   dans un même document (Amazon 599,00 + 13,99 = 612,99) ; le montant de la transaction
   n'apparaît alors comme total nulle part.
3. **Adresse perso exclue** — `_SEARCH_EXCLUDE` contenait l'adresse personnelle de l'utilisateur,
   ce qui masquait TOUT justificatif qu'il se transfère à lui-même.

Le garde-fou anti « mauvais marchand » doit rester intact dans les trois cas.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import reconcile_qonto as rq  # noqa: E402
import audit_unreconciled as audit  # noqa: E402


# --------------------------------------------------------------------------- #
#  1. Repli photo : la requête sans montant
# --------------------------------------------------------------------------- #
def test_fallback_query_drops_amount_keeps_guards():
    q = rq.build_receipt_query(["AMAZON", "LUNETTES"], ["612.99", "612,99"],
                               "2026/07/08", "2026/07/28", with_amount=False)
    assert "612" not in q                      # le montant n'est PAS cherchable (pixels)
    assert "(AMAZON OR LUNETTES)" in q         # le jeton marchand reste exigé
    assert "filename:jpg" in q                 # ... et une PHOTO (pas un PDF : déjà indexé)
    assert "after:2026/07/08" in q and "before:2026/07/28" in q


def test_normal_query_still_requires_amount():
    q = rq.build_receipt_query(["AMAZON"], ["612.99"], "2026/07/08", "2026/07/28")
    assert "(612.99)" in q and "filename:jpg" not in q


# --------------------------------------------------------------------------- #
#  2. Somme de plusieurs totaux
# --------------------------------------------------------------------------- #
_DEUX_FACTURES = """
    Récapitulatif de commande
    Articles :                    13,99 €
    Total :                       13,99 €
    Montant total TTC :           13,99 €

    XREAL One Pro AR Lunettes     499,17 €     20 %      599,00 €
    Facture Total                                        599,00 €
    Total à payer                                        599,00 €
"""


def test_sum_of_two_invoices_matches_single_reimbursement():
    # 599,00 + 13,99 = 612,99 : le montant du virement n'est un total NULLE PART.
    assert not audit._pdf_has_total(_DEUX_FACTURES, audit._amount_strings([612.99]),
                                    gap=rq.VERIFY_GAP, allow_bare_total=True)
    assert audit.totals_sum_to(_DEUX_FACTURES, [612.99], gap=rq.VERIFY_GAP)


def test_sum_rejects_amount_that_is_not_a_sum():
    assert not audit.totals_sum_to(_DEUX_FACTURES, [500.00], gap=rq.VERIFY_GAP)
    assert not audit.totals_sum_to(_DEUX_FACTURES, [999.99], gap=rq.VERIFY_GAP)


def test_sum_needs_at_least_two_totals():
    assert not audit.totals_sum_to("Total à payer   42,00 €", [42.0])


def test_parse_amount_formats():
    assert audit._parse_amount("599,00") == 599.00
    assert audit._parse_amount("1 156,41") == 1156.41
    assert audit._parse_amount("1,156.41") == 1156.41
    assert audit._parse_amount("13,99") == 13.99
    assert audit._parse_amount("nope") == -1.0


# --------------------------------------------------------------------------- #
#  3. Le garde-fou marchand survit aux deux nouveaux chemins
# --------------------------------------------------------------------------- #
def test_sum_match_still_requires_merchant_name():
    amt = audit._amount_strings([612.99])
    # bon montant (par somme) MAIS mauvais marchand -> refusé
    assert not rq.verify_pdf(_DEUX_FACTURES, amt, ["KLM"], "x@y.com", "sans rapport",
                             targets=[612.99])
    # bon montant + jeton marchand présent -> accepté
    assert rq.verify_pdf(_DEUX_FACTURES, amt, ["LUNETTES"], "x@y.com", "sans rapport",
                         targets=[612.99])


def test_verify_without_targets_keeps_old_strict_behaviour():
    # Sans `targets`, aucun repli somme : comportement historique inchangé.
    assert not rq.verify_pdf(_DEUX_FACTURES, audit._amount_strings([612.99]),
                             ["LUNETTES"], "x", "y")


# --------------------------------------------------------------------------- #
#  4. L'adresse perso n'est plus exclue (les récaps le sont, par sujet)
# --------------------------------------------------------------------------- #
def test_personal_address_not_excluded_but_recap_is():
    assert not any("naelkodmani" in d for d in audit._SEARCH_EXCLUDE), (
        "exclure l'adresse perso masque les justificatifs auto-transférés")
    assert "qonto.com" in audit._SEARCH_EXCLUDE          # notifs Qonto : toujours exclues
    assert "[Rapprochement]" in audit._SEARCH_EXCLUDE_SUBJECT
    q = rq.build_receipt_query(["AMAZON"], ["1.00"], "2026/07/01", "2026/07/10")
    assert "-subject:[Rapprochement]" in q and "naelkodmani" not in q


# --------------------------------------------------------------------------- #
#  5. Montant à ESPACE : doit être entre guillemets (régression 2026-07-20)
# --------------------------------------------------------------------------- #
def test_spaced_amount_is_quoted_in_query():
    """« 1 000,00 » non quoté = espace lue comme séparateur -> groupe OR corrompu -> 0 résultat.

    Bug SILENCIEUX et large : `_amount_strings` produit la variante à espace dès 4 chiffres,
    donc TOUTE transaction >= 1000 était introuvable (mesuré : 9 résultats sans la variante,
    0 avec). Aucune erreur n'était levée — juste « aucun justificatif ».
    """
    amts = audit._amount_strings([1000.0])
    assert "1 000,00" in amts                         # la variante existe bien
    q = rq.build_receipt_query(["ARTISTIC"], amts, "2026/07/06", "2026/07/26")
    assert '"1 000,00"' in q                          # ... et elle est quotée
    # les variantes sans espace restent nues (pas de sur-quotage inutile)
    assert '"1000.00"' not in q and "1000.00" in q


def test_q_helper_only_quotes_when_needed():
    assert rq._q("871.99") == "871.99"
    assert rq._q("1 000,00") == '"1 000,00"'
    assert rq._q("1,000.00") == "1,000.00"


# --------------------------------------------------------------------------- #
#  6. Format européen « 1.483,30 » (point = milliers) — régression 2026-07-20
# --------------------------------------------------------------------------- #
def test_european_thousands_separator_variant_exists():
    """Le POINT comme séparateur de milliers manquait, malgré le README.

    Facture Villa Duflot : « Solde: 1.483,30 € ». Aucune des variantes générées
    (1483.30 / 1483,30 / 1,483.30 / 1 483,30) ne correspond -> jamais rapprochée.
    """
    v = audit._amount_strings([1483.30])
    assert "1.483,30" in v
    assert {"1483.30", "1483,30", "1,483.30", "1 483,30"} <= set(v)   # les autres restent


def test_european_variant_matches_a_solde_line():
    facture = "Total:  1.826,30 €\nRèglement:  -343,00 €\nSolde:  1.483,30 €"
    assert audit._pdf_has_total(facture, audit._amount_strings([1483.30]),
                                gap=rq.VERIFY_GAP, allow_bare_total=True)
    # un montant absent ne doit pas matcher pour autant
    assert not audit._pdf_has_total(facture, audit._amount_strings([1234.56]),
                                    gap=rq.VERIFY_GAP, allow_bare_total=True)


def test_small_amounts_unaffected():
    """< 1000 : pas de séparateur de milliers, la liste ne change pas."""
    assert audit._amount_strings([343.0]) == ["343.00", "343,00"]
