"""Régression : auto-attache SANS lien de nom, gardée par l'UNICITÉ montant+date.

Voie « exact_unique » de reconcile_saas.match : une facture identifiée dont le montant
(spécifique, non rond) correspond à EXACTEMENT UN débit non attaché dans une fenêtre serrée
(±5 j, débit non antérieur) est auto-attachée sans que le nom marchand figure dans le libellé
Qonto. Round / ambigu / non identifié / antérieur -> jamais auto (warn ou non rapproché).

Ces tests figent le compromis automation/sécurité (montant unique vs mauvais marchand).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import reconcile_saas as rs  # noqa: E402


def _inv(**kw):
    base = dict(amount=137.42, date="2026-07-02", aliases=[], company_id=False,
                trusted=False, vendor="inconnu", currency="EUR",
                primary_pdf="x.pdf", msgid="m1", pdf_paths=["x.pdf"])
    base.update(kw)
    return base


def _debit(**kw):
    base = dict(id="d1", label="UNRELATED MERCHANT", amount=None,
                local_amount=None, settled_at="2026-07-04")
    base.update(kw)
    return base


def _match(inv, debits):
    return rs.match([inv], debits, warn_days=10, window_days=15, unique_window_days=5)


def test_unique_specific_identified_autoattaches():
    m, u = _match(_inv(amount=137.42, company_id=True),
                  [_debit(id="d1", amount=137.42, settled_at="2026-07-04")])
    assert len(m) == 1 and not u
    assert m[0].confidence == "exact_unique"
    assert m[0].confidence in rs.AUTO_ATTACH
    assert m[0].debit["id"] == "d1"


def test_villa_duflot_type_company_id_no_name_in_label():
    # Facture d'acompte adressée à la société (company_id) dont le nom n'est PAS dans le libellé.
    m, u = _match(_inv(amount=1143.70, company_id=True, aliases=["VILLA-DUFLOT"]),
                  [_debit(id="d1", label="SUMUP *SOMETHING", amount=1143.70,
                          settled_at="2026-07-05")])
    assert m and m[0].confidence == "exact_unique"


def test_incidental_aliases_are_not_identity():
    # LEÇON disputes.pdf : des alias incidents (« QONTO » lu dans un doc de litige) NE valent
    # PAS identité. company_id False + trusted False -> jamais auto, même montant unique.
    m, u = _match(_inv(amount=97.10, company_id=False, trusted=False, aliases=["QONTO"]),
                  [_debit(id="d1", label="A LA DAUPHINE", amount=97.10, settled_at="2026-06-10")])
    assert not m and len(u) == 1


def test_two_candidates_is_ambiguous_warn():
    m, u = _match(_inv(amount=55.20, company_id=True),
                  [_debit(id="d1", amount=55.20, settled_at="2026-07-03"),
                   _debit(id="d2", amount=55.20, settled_at="2026-07-04")])
    assert m and m[0].confidence == "warn"
    assert m[0].confidence not in rs.AUTO_ATTACH


def test_round_amount_held_as_warn():
    m, u = _match(_inv(amount=100.00, company_id=True),
                  [_debit(id="d1", amount=100.00, settled_at="2026-07-04")])
    assert m and m[0].confidence == "warn"


def test_residual_nonround_attaches_but_round_caught():
    # Résidu assumé : un unique débit du bon montant NON rond -> auto (on l'accepte).
    m, _ = _match(_inv(amount=77.31, company_id=True),
                  [_debit(id="d1", amount=77.31, settled_at="2026-07-04")])
    assert m and m[0].confidence == "exact_unique"
    # …mais un montant rond identique (50,00) est retenu par la garde ronde -> warn.
    m2, _ = _match(_inv(amount=50.00, company_id=True),
                   [_debit(id="d1", amount=50.00, settled_at="2026-07-04")])
    assert m2 and m2[0].confidence == "warn"


def test_debit_predating_invoice_excluded():
    # Débit 4 j AVANT la facture (signed -4 < -2) : pas un vrai paiement -> pas exact_unique.
    m, u = _match(_inv(amount=63.90, date="2026-07-10", company_id=True),
                  [_debit(id="d1", amount=63.90, settled_at="2026-07-06")])
    assert m and m[0].confidence == "warn"        # retenu (company_id), jamais auto


def test_name_in_label_still_exact():
    m, u = _match(_inv(amount=88.10, aliases=["ACME"]),
                  [_debit(id="d1", label="ACME PARIS 75001", amount=88.10,
                          settled_at="2026-07-05")])
    assert m and m[0].confidence == "exact"


def test_anonymous_parse_never_auto():
    # Ni nom, ni company_id, ni alias -> non rapproché même avec un unique débit du bon montant.
    m, u = _match(_inv(amount=42.17, company_id=False, trusted=False, aliases=[]),
                  [_debit(id="d1", amount=42.17, settled_at="2026-07-04")])
    assert not m and len(u) == 1


def test_confirm_unique_flag_downgrades_to_warn():
    m, u = rs.match([_inv(amount=137.42, company_id=True)],
                    [_debit(id="d1", amount=137.42, settled_at="2026-07-04")],
                    warn_days=10, window_days=15, unique_window_days=5, confirm_unique=True)
    assert m and m[0].confidence == "warn"
