#!/usr/bin/env python3
"""Audit FIABLE des transactions sans justificatif + vérification adversariale.

Étapes 2-3 du récap hebdo (voir weekly_recap.sh). Base sur la VÉRITÉ LIVE de Qonto
(statut PJ réel, pas un snapshot), puis cherche LARGEMENT à prouver qu'un justificatif
existe avant de déclarer une transaction « non rapprochée ».

Pipeline :
  1. fetch_unreconciled_expenses(since)  → périmètre (CB + prélèvements + virements hors
     internes « MAISON DARWISH », hors frais Qonto).
  2. Re-vérif LIVE par tx : get_transaction_attachments → ne garder que celles à 0 PJ.
  3. Pour chaque candidat, recherche adversariale d'un justificatif (même montant ±0.02
     sur EUR ou local_amount, date ±10 j) dans : PDF déjà téléchargés (reports/*/input),
     Gmail hello@ (saas), Gmail perso (ubereats), Gmail parishouse (lovable).
       - rien trouvé        → CONFIRMED (vraiment sans justificatif)
       - justificatif trouvé → SUSPECT (existe mais pas attaché = raté d'automation à corriger)

Sorties : reports/recap/confirmed.json, reports/recap/suspects.json, reports/recap/audit_<date>.md

Usage :
    python3 scripts/audit_unreconciled.py [--since YYYY-MM-DD] [--days 7] [--dry-run]
"""
from __future__ import annotations

import argparse
import imaplib
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from recon.qonto_client import QontoClient, load_qonto_credentials  # noqa: E402
from recon.config import load_dotenv  # noqa: E402
from recon import saas  # noqa: E402

OUT_DIR = ROOT / "reports" / "recap"
AMOUNT_TOL = 0.02            # tolérance large (≠ matching strict des flux)
DATE_WINDOW = 10            # jours

# Boîtes Gmail à fouiller : (var_email, var_password, étiquette).
GMAIL_BOXES = [
    ("SAAS_GMAIL", "SAAS_GMAIL_APP_PASSWORD", "hello"),
    ("UBEREATS_GMAIL", "UBEREATS_GMAIL_APP_PASSWORD", "perso"),
    ("LOVABLE_GMAIL", "LOVABLE_GMAIL_APP_PASSWORD", "parishouse"),
]
# Expéditeurs à IGNORER dans la recherche email (ne sont pas des justificatifs) :
# Qonto (notifications « virement exécuté » qui répètent le montant) + nos propres récaps.
_SEARCH_EXCLUDE = ["qonto.com", "naelkodmani@gmail.com"]


# --------------------------------------------------------------------------- #
#  Recherche adversariale dans les sources de justificatifs
# --------------------------------------------------------------------------- #
_PDFTEXT_CACHE: dict = {}   # path -> texte (audit sur l'exercice = bcp de candidats × PDF)


def _ocr_fallback(path: Path) -> str:
    """OCR d'un PDF scanné (image) via le pipeline partagé fournisseurs (Vision + cache).

    CRITIQUE : sans ça, un justificatif scanné (photo de reçu — ex. N0569 « Reste à payer
    24,70 » du resto Le Pschill) renvoie un texte pdftotext VIDE → l'audit le rate → déclare
    À TORT la transaction « non rapprochée ». Le filet anti-faux-positif du récap DOIT OCR-iser.
    """
    try:
        from reconcile_fournisseurs import _extract_text  # scripts/ déjà sur sys.path
        return _extract_text(path) or ""
    except Exception:
        return ""


def _pdftotext(path: Path) -> str:
    key = str(path)
    if key in _PDFTEXT_CACHE:
        return _PDFTEXT_CACHE[key]
    txt = _pdftotext_raw(path)
    if len(txt.strip()) < 20:            # PDF scanné (image) → pdftotext vide → repli OCR
        txt = _ocr_fallback(path) or txt
    _PDFTEXT_CACHE[key] = txt
    return txt


def _pdftotext_raw(path: Path) -> str:
    for binp in ("pdftotext", "/opt/homebrew/bin/pdftotext", "/usr/local/bin/pdftotext"):
        try:
            r = subprocess.run([binp, "-layout", str(path), "-"],
                               capture_output=True, timeout=20)
            if r.returncode == 0:
                return r.stdout.decode("utf-8", "ignore")
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
    return ""


def _amount_targets(cand: dict) -> list:
    """Montants à chercher : EUR + montant d'origine (devise) le cas échéant."""
    vals = []
    for k in ("amount", "local_amount"):
        v = cand.get(k)
        if v:
            vals.append(round(float(v), 2))
    return vals


def _amount_strings(targets: list) -> list:
    """Variantes textuelles d'un montant, séparateurs de milliers inclus (US/FR)."""
    out = []
    for t in targets:
        s = f"{t:.2f}"
        out.append(s)                        # 1156.41
        out.append(s.replace(".", ","))      # 1156,41
        intp, dec = s.split(".")
        if len(intp) > 3:
            us = f"{int(intp):,}.{dec}"                       # 1,156.41
            fr = us.replace(",", " ").replace(".", ",")       # 1 156,41
            out.append(us)
            out.append(fr)
    return list(dict.fromkeys(out))


# Montant considéré comme un VRAI total payé s'il est proche d'un de ces mots-clés
# (évite les faux positifs : « TVA (20,00 %) », sous-totaux, numéros, stats).
# « pay[ée]\w* » couvre déjà « payer » (reste/solde/net à payer) ; on ajoute la famille du
# solde restant (reste/solde/restant dû) pour le net réellement débité (paiement partagé/acompte).
_TOTAL_KW = (r"(?:total|montant|pay[ée]\w*|factur[ée]|net\s+[àa]\s+payer|reste|solde|restant|"
             r"amount\s+(?:paid|due)|grand\s+total|charg)")

# Sous-ensemble de labels de paiement FORTS (haute précision) : on accepte le montant adossé
# à eux MÊME SANS symbole devise. Certaines factures listent les montants sans € par ligne
# (colonne « (EUR) »), ex. Bolt « Facturé Apple Pay 16.00 ». Ces labels sont assez spécifiques
# pour ne pas capter un nombre parasite (≠ le « total » générique, gardé, lui, avec symbole).
_NET_KW = (r"(?:factur[ée]|reste\s*[àa]?\s*(?:payer|r[ée]gler)|solde\s*[àa]?\s*(?:payer|r[ée]gler)|"
           r"net\s*[àa]?\s*(?:payer|r[ée]gler)|total\s+t\.?\s*t\.?\s*c|montant\s+(?:pay[ée]\w*|total))")


# Marqueur de devise : symbole OU code ISO en toutes lettres (« 15,04 EUR », « USD 6.00 »).
_CUR = r"(?:[€$£]|\bEUR\b|\bUSD\b|\bGBP\b)"


def _pdf_has_total(txt: str, amt_strs: list, gap: int = 75) -> bool:
    """`gap` = largeur max (caractères, même ligne) entre le mot-clé « total/payé… » et le
    montant. 75 par défaut (audit strict) ; le rapprochement piloté-transaction passe plus large
    (montant souvent en colonne, loin du label — ex. reçu Uber « Total …90 espaces… 15,04 € »),
    car il exige EN PLUS le nom marchand (double garde -> pas de faux positif)."""
    for s in amt_strs:
        esc = re.escape(s)
        # Devise (symbole ou code ISO) AVANT (« €35.99 ») OU APRÈS (« 35.99 € / 35.99 EUR »).
        money = r"(?:" + _CUR + r"\s*" + esc + r"|" + esc + r"\s*" + _CUR + r")"
        if re.search(_TOTAL_KW + r"[^\n]{0," + str(gap) + r"}" + money, txt, re.IGNORECASE):
            return True
        if re.search(money + r"[^\n]{0,40}" + _TOTAL_KW, txt, re.IGNORECASE):
            return True
        # Sans symbole devise : uniquement adossé à un label de paiement FORT (Facturé / Reste
        # à payer / Total TTC…). Lookbehind/lookahead = pas de sous-nombre (« 116.00 » ≠ 16.00).
        amt_only = r"(?<![\d.,])" + esc + r"(?![\d.,])"
        if re.search(_NET_KW + r"[^\n]{0,40}" + amt_only, txt, re.IGNORECASE):
            return True
    return False


def search_local_pdfs(cand: dict) -> str:
    """Renvoie le chemin d'un PDF local où le montant cible est un TOTAL payé, sinon "".

    Un montant ne compte que s'il est adossé à un mot-clé « total/payé/facturé… » (≠ taux
    de TVA, sous-total, stat). Puis filtre date ±30 j (coupe les coïncidences type facture
    Anthropic 20€ ≠ remboursement 20€). Date PDF inconnue → on garde le doute (sécurité).
    """
    targets = _amount_targets(cand)
    if not targets:
        return ""
    amt_strs = _amount_strings(targets)
    ref = cand.get("settled_at", "")
    for pdf in ROOT.glob("reports/*/input/*.pdf"):
        txt = _pdftotext(pdf)
        if not txt or not _pdf_has_total(txt, amt_strs):
            continue
        try:
            pdate = saas.parse_pdf(pdf).date
        except Exception:
            pdate = ""
        if pdate and ref and abs(_days_between(pdate, ref)) > 30:
            continue
        return str(pdf.relative_to(ROOT))
    return ""


def _days_between(d1: str, d2: str) -> int:
    try:
        a = datetime.strptime(d1[:10], "%Y-%m-%d").date()
        b = datetime.strptime(d2[:10], "%Y-%m-%d").date()
        return (a - b).days
    except (ValueError, TypeError):
        return 0


def _gmail_search_box(env_user: str, env_pass: str, targets: list,
                      d_from: str, d_to: str) -> int:
    user = os.environ.get(env_user, "").strip()
    pwd = os.environ.get(env_pass, "").strip().replace(" ", "")
    if not user or not pwd:
        return 0
    # Variantes « 16,75 » et « 16.75 », SANS guillemets internes (sinon X-GM-RAW =
    # « Could not parse command » → l'exception était avalée et renvoyait 0 à tort).
    amt_terms = _amount_strings(targets)
    if not amt_terms:
        return 0
    excl = " ".join(f"-from:{d}" for d in _SEARCH_EXCLUDE)
    # Le SUJET doit ressembler à un justificatif (un vrai reçu/facture le porte en objet) ET
    # le montant mentionné — sinon un mail quelconque qui cite le nombre = faux SUSPECT
    # (bruit à l'échelle de l'exercice ; « total/payment » dans le corps = trop courant).
    # Le PDF local reste, lui, un signal fort sans ce filtre.
    kw = ("subject:(recu OR receipt OR facture OR invoice OR commande OR order OR "
          "reservation OR confirmation OR payment)")
    q = f'({" OR ".join(amt_terms)}) {kw} {excl} after:{d_from} before:{d_to}'
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        m.login(user, pwd)
        m.select("inbox")
        typ, data = m.uid("search", None, "X-GM-RAW", f'"{q}"')
        m.logout()
        return len(data[0].split()) if (typ == "OK" and data and data[0]) else 0
    except Exception as exc:
        print(f"    (recherche Gmail {env_user} échouée : {exc})", file=sys.stderr)
        return 0


def search_gmail(cand: dict) -> str:
    """Renvoie l'étiquette de la boîte où un justificatif probable existe, sinon ""."""
    targets = _amount_targets(cand)
    if not targets:
        return ""
    try:
        ref = datetime.strptime(cand["settled_at"], "%Y-%m-%d").date()
    except (ValueError, KeyError):
        return ""
    d_from = (ref - timedelta(days=DATE_WINDOW)).strftime("%Y/%m/%d")
    d_to = (ref + timedelta(days=DATE_WINDOW)).strftime("%Y/%m/%d")
    for env_user, env_pass, tag in GMAIL_BOXES:
        if _gmail_search_box(env_user, env_pass, targets, d_from, d_to) > 0:
            return tag
    return ""


# --------------------------------------------------------------------------- #
#  Audit
# --------------------------------------------------------------------------- #
def run_audit(since: str, client: QontoClient) -> tuple:
    candidates = client.fetch_unreconciled_expenses(since)
    print(f"  {len(candidates)} transaction(s) dans le périmètre depuis {since}.", file=sys.stderr)

    confirmed, suspects = [], []
    for cand in candidates:
        # Re-vérif LIVE autoritaire : a-t-elle vraiment 0 PJ maintenant ?
        try:
            atts = client.get_transaction_attachments(cand["id"])
        except Exception:
            atts = None
        if atts:  # PJ présente → rapprochée, on ignore (immunité snapshot périmé)
            continue

        # Recherche adversariale : un justificatif existe-t-il quelque part ?
        where = search_local_pdfs(cand)
        source = f"PDF local ({where})" if where else ""
        if not source:
            tag = search_gmail(cand)
            source = f"email ({tag})" if tag else ""

        item = {
            "id": cand["id"], "label": cand["label"],
            "amount": cand["amount"], "currency": cand["currency"],
            "local_amount": cand["local_amount"], "local_currency": cand["local_currency"],
            "date": cand["settled_at"], "operation_type": cand["operation_type"],
        }
        if source:
            item["found_in"] = source
            item["reason"] = "justificatif probable trouvé mais NON attaché (à corriger)"
            suspects.append(item)
        else:
            item["reason"] = "aucun justificatif trouvé (CB/prélèvement/remboursement)"
            confirmed.append(item)

    return confirmed, suspects


def write_outputs(confirmed: list, suspects: list, since: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "confirmed.json").write_text(
        json.dumps(confirmed, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "suspects.json").write_text(
        json.dumps(suspects, ensure_ascii=False, indent=2), encoding="utf-8")

    today = date.today().isoformat()
    lines = [f"# Audit récap — {today} (depuis {since})", ""]
    lines.append(f"- **{len(confirmed)}** CONFIRMÉ(s) sans justificatif")
    lines.append(f"- **{len(suspects)}** SUSPECT(s) (justificatif probable, à corriger/escalader)")
    lines.append("")
    if suspects:
        lines.append("## SUSPECTS (doute → escalade)")
        for s in suspects:
            lines.append(f"- {s['date']} · {s['amount']} {s['currency']} · {s['label']} "
                         f"→ {s.get('found_in', '?')}  (`{s['id']}`)")
        lines.append("")
    lines.append("## CONFIRMÉS (vraiment sans justificatif)")
    for c in confirmed:
        lines.append(f"- {c['date']} · {c['amount']} {c['currency']} · {c['label']}  (`{c['id']}`)")
    (OUT_DIR / f"audit_{today}.md").write_text("\n".join(lines), encoding="utf-8")


def _fiscal_year_start() -> str:
    """Début de l'exercice comptable en cours (1er avril → 1er avril suivant)."""
    t = date.today()
    year = t.year if t.month >= 4 else t.year - 1
    return f"{year:04d}-04-01"


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit fiable des tx sans justificatif.")
    ap.add_argument("--since", default=None,
                    help="YYYY-MM-DD (défaut : début de l'exercice comptable, 1er avril).")
    ap.add_argument("--days", type=int, default=None,
                    help="Fenêtre en jours (override ; défaut = exercice comptable).")
    ap.add_argument("--dry-run", action="store_true", help="N'écrit pas les fichiers, imprime le résumé.")
    args = ap.parse_args()

    load_dotenv()
    # Défaut = TOUT l'exercice comptable (on ré-audite les vieilles tx orphelines dont le
    # justificatif arrive plus tard). --since / --days restent pour override/tests.
    if args.since:
        since = args.since
    elif args.days is not None:
        since = (date.today() - timedelta(days=args.days)).isoformat()
    else:
        since = _fiscal_year_start()
    slug, secret = load_qonto_credentials()
    client = QontoClient(slug, secret)

    confirmed, suspects = run_audit(since, client)
    print(f"\n{len(confirmed)} CONFIRMÉ(s) · {len(suspects)} SUSPECT(s).", file=sys.stderr)
    for s in suspects:
        print(f"  ⚠ SUSPECT {s['date']} {s['amount']}{s['currency']} {s['label']} "
              f"→ {s.get('found_in')}", file=sys.stderr)

    if args.dry_run:
        print("--dry-run : fichiers non écrits.", file=sys.stderr)
        return 2 if suspects else 0

    write_outputs(confirmed, suspects, since)
    print(f"→ {OUT_DIR}/confirmed.json · suspects.json", file=sys.stderr)
    # Code retour : 2 si suspects (le shell déclenche l'escalade), 0 sinon.
    return 2 if suspects else 0


if __name__ == "__main__":
    raise SystemExit(main())
