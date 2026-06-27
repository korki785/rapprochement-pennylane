#!/usr/bin/env python3
"""Auto-test du récap fiable : prouve que la vérif adversariale détecte un justificatif
existant (→ SUSPECT, jamais confirmé à tort) et n'en invente pas (→ CONFIRMÉ).

- Tests UNITAIRES déterministes (sans données) : la logique « montant = vrai total ».
- Test LIVE optionnel : le 16,75 (reçu UberEats réel) doit ressortir SUSPECT.

Usage :  python3 scripts/selftest_recap.py
Sortie : OK / FAIL par test, code retour 1 si un test échoue.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from recon.config import load_dotenv  # noqa: E402
import audit_unreconciled as A  # noqa: E402

_fails = 0


def check(name: str, cond: bool):
    global _fails
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails += 1


def unit_tests():
    print("Tests unitaires (logique montant = vrai total) :")
    # « Total 16,75 € » → 16,75 reconnu comme total.
    txt_ok = "Total                                          16,75 €\nSous-total des articles   27,80 €"
    check("« Total … 16,75 € » détecté", A._pdf_has_total(txt_ok, ["16,75", "16.75"]))
    # « TVA (20,00 %) » → 20,00 N'EST PAS un total (faux positif évité).
    txt_tva = "Sous-total 67,00 €\nTVA (20,00 %)   5,36 €\nTotal   67,00 €"
    check("« TVA (20,00 %) » ignoré (pas un total)", not A._pdf_has_total(txt_tva, ["20,00", "20.00"]))
    # Le vrai total 67,00 de ce même reçu EST reconnu.
    check("« Total 67,00 € » détecté", A._pdf_has_total(txt_tva, ["67,00", "67.00"]))
    # Variantes de montant (point/virgule).
    check("variantes 16.75/16,75", set(A._amount_strings([16.75])) == {"16.75", "16,75"})


def live_test():
    print("\nTest live (justificatif réel 16,75 UberEats) :")
    cand = {"id": "test", "label": "Nael Darwish", "amount": 16.75, "currency": "EUR",
            "local_amount": 16.75, "local_currency": "EUR", "settled_at": "2026-06-24",
            "operation_type": "transfer"}
    local = A.search_local_pdfs(cand)
    gmail = "" if local else A.search_gmail(cand)
    found = bool(local or gmail)
    print(f"    PDF local : {local or '(aucun)'} | email : {gmail or '(aucun)'}")
    check("16,75 → justificatif TROUVÉ (donc SUSPECT, pas confirmé à tort)", found)

    fake = dict(cand, amount=8888.88, local_amount=8888.88)
    check("montant inexistant 8888.88 → RIEN trouvé (CONFIRMÉ)",
          not (A.search_local_pdfs(fake) or A.search_gmail(fake)))


def parse_tests():
    """Formats nombre/date + remise + position du symbole (figés pour éviter la récidive)."""
    print("\nTests parsing montant (remise / formats / symbole) :")
    import reconcile_fournisseurs as RF
    from recon import saas
    # Facture à remise (Hostinger) : le Total payé = 35,99, PAS le prix 63,99.
    hostinger = ("Invoice Amount # €35.99 (EUR)\n"
                 "€63.99 x 1   (€34.00)   €29.99   €6.00   €35.99\n"
                 "Total excl. VAT   €29.99\nTotal   €35.99\nAmount Due (EUR)   €0.00")
    cands = RF._amount_candidates(hostinger)
    check("remise : candidat primaire = 35.99 (PAS 63.99)", bool(cands) and cands[0] == 35.99)
    check("remise : 35.99 ∈ candidats", 35.99 in cands)
    # Reçu resto : TOTAL TTC = 95,00 (max OK ici aussi).
    resto = "TOTAL TTC   95,00€\nTVA 10%   8,64\nHT   86,36"
    check("resto : Total TTC = 95.00", RF._amount_candidates(resto)[0] == 95.0)
    # Format US à séparateur de milliers.
    check("US « 1,156.41 » → 1156.41", saas._to_float("1,156.41") == 1156.41)
    # Récap : symbole AVANT le montant (« €35.99 ») doit être trouvé.
    check("récap : « Total €35.99 » (symbole avant) détecté",
          A._pdf_has_total("Total   €35.99", ["35.99", "35,99"]))


def main() -> int:
    load_dotenv()
    unit_tests()
    parse_tests()
    try:
        live_test()
    except Exception as exc:
        print(f"    (test live ignoré : {exc})", file=sys.stderr)
    print(f"\n{'TOUS OK ✅' if _fails == 0 else f'{_fails} ÉCHEC(S) ❌'}")
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
