#!/usr/bin/env python3
"""Rapprochement PILOTÉ PAR LA TRANSACTION (sans liste blanche de fournisseurs).

Sens inverse du flux « saas » (qui balaie les emails puis cherche un débit) : ici on part de
CHAQUE transaction Qonto sans justificatif, et on utilise SON PROPRE libellé (= le nom du
marchand) + son montant pour retrouver le reçu/facture dans Gmail, le télécharger, le vérifier,
puis l'attacher. Aucun fournisseur n'a besoin d'être pré-enregistré : le nom vient du libellé.

Garde-fou anti « mauvais marchand » : on n'attache QUE si (i) le montant de la transaction (EUR
OU devise) est un VRAI total dans le PDF, ET (ii) un jeton marchand tiré du libellé apparaît dans
le PDF ou l'expéditeur/sujet. Si ≥2 emails distincts passent la vérif → on ne devine pas (skip).

Usage :
    python3 scripts/reconcile_qonto.py [--since YYYY-MM-DD] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from recon.qonto_client import QontoClient, load_qonto_credentials  # noqa: E402
from recon.config import load_dotenv  # noqa: E402
from recon.normalize import tx_merchant_tokens  # noqa: E402
from recon import saas  # noqa: E402
import audit_unreconciled as audit  # noqa: E402  (réutilise _amount_*, _pdf_has_total, _pdftotext, GMAIL_BOXES…)
import fetch_saas_gmail as fetch  # noqa: E402  (connect_imap, fetch_message, extract_pdfs, _decode)

OUT_DIR = ROOT / "reports" / "qonto"
INPUT_DIR = OUT_DIR / "input"
PROCESSED_PATH = OUT_DIR / "processed.json"
DEFAULT_SINCE = "2026-04-01"


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


# Le repli ne vise QUE les justificatifs photographiés : c'est le seul cas où le montant est
# invisible pour l'index texte de Gmail. Le texte d'un PDF, lui, EST indexé — un PDF est donc
# déjà trouvé par la recherche normale, et l'inclure ici revenait à télécharger + OCR-iser des
# centaines de pièces jointes pour rien (run > 10 min, constaté 2026-07-20).
_IMAGE_FILTER = ("(filename:jpg OR filename:jpeg OR filename:png OR "
                 "filename:heic OR filename:heif)")

# Repli photo : nombre max d'emails inspectés quand la recherche par montant ne donne rien.
MAX_FALLBACK_MSGS = 6


def _q(term: str) -> str:
    """Met un terme entre guillemets s'il contient une espace.

    `_amount_strings` produit le format français à espace fine (« 1 000,00 »). Injecté SANS
    guillemets dans un groupe OR, Gmail lit l'espace comme un séparateur de termes : le groupe
    entier est corrompu et la requête ne renvoie plus RIEN (constaté 2026-07-20 : 9 résultats
    sans la variante, 0 avec). Tout montant ≥ 1000 était donc introuvable, en silence.

    Suppose un envoi en LITTÉRAL IMAP (cf. `_uid_search_raw`) : la requête n'est plus elle-même
    encadrée de guillemets, donc les guillemets internes sont enfin autorisés.
    """
    return f'"{term}"' if " " in term else term


def _uid_search_raw(mail, query: str) -> list:
    """UID SEARCH X-GM-RAW envoyé en LITTÉRAL (`{n}` + payload brut).

    L'ancienne forme `mail.uid("search", None, "X-GM-RAW", f'"{q}"')` encadrait la requête de
    guillemets : impossible d'en utiliser à l'intérieur, d'où l'interdiction historique et le
    bug du montant à espace. Le littéral supprime la contrainte.
    """
    tag = mail._new_tag()
    payload = query.encode("utf-8")
    mail.send(b"%s UID SEARCH X-GM-RAW {%d}\r\n" % (tag, len(payload)))
    while True:                                  # attend l'invite « + » du serveur
        line = mail.readline()
        if not line or line.startswith(b"+"):
            break
    mail.send(payload + b"\r\n")
    uids, status = [], b""
    while True:
        line = mail.readline()
        if not line:
            break
        if line.upper().startswith(b"* SEARCH"):
            uids = line.split()[2:]
        if line.startswith(tag):
            status = line
            break
    if b"OK" not in status.upper():
        raise OSError(f"UID SEARCH refusé : {status.decode('utf-8', 'replace').strip()}")
    return uids


def build_receipt_query(tokens: list, amt_strs: list, d_from: str, d_to: str,
                        with_amount: bool = True) -> str:
    """Requête X-GM-RAW : (montants) ET (jetons marchands) ET fenêtre de dates, expéditeurs exclus.

    Le montant TROUVE l'email, le jeton marchand le DÉSAMBIGUÏSE (ex. deux reçus à 40 € : on
    garde celui « UBER »). Les termes à espace passent par `_q` (guillemets obligatoires).

    `with_amount=False` : repli PHOTO — voir `search_receipt_uids`. Le montant est retiré de la
    requête (il n'existe que dans les pixels), le jeton marchand + la pièce jointe restent exigés.
    """
    tok_grp = "(" + " OR ".join(tokens) + ")"
    excl = " ".join(f"-from:{d}" for d in audit._SEARCH_EXCLUDE)
    excl += " " + " ".join(f"-subject:{s}" for s in audit._SEARCH_EXCLUDE_SUBJECT)
    if not with_amount:
        return f"{tok_grp} {_IMAGE_FILTER} {excl} after:{d_from} before:{d_to}"
    amt_grp = "(" + " OR ".join(_q(a) for a in amt_strs) + ")"
    return f"{amt_grp} {tok_grp} {excl} after:{d_from} before:{d_to}"


def search_receipt_uids(box, tokens: list, amt_strs: list, d_from: str, d_to: str,
                        with_amount: bool = True) -> list:
    """UIDs des emails dont le montant ET un jeton marchand correspondent (fenêtre de dates).

    Reconnecte UNE fois si la connexion IMAP est morte : elle est ouverte une seule fois puis
    réutilisée sur des centaines de recherches, et Gmail la coupe en cours de route
    (`[SSL: BAD_WRITE_RETRY]`). L'exception était avalée en renvoyant [] — indistinguable de
    « aucun reçu », donc des transactions étaient abandonnées EN SILENCE.
    """
    q = build_receipt_query(tokens, amt_strs, d_from, d_to, with_amount=with_amount)
    for attempt in (1, 2):
        try:
            return _uid_search_raw(box.mail, q)
        except Exception as exc:
            if attempt == 2 or not box.reconnect():
                print(f"    ⚠ recherche Gmail [{box.tag}] échouée ({exc}) — "
                      f"transaction NON conclue", file=sys.stderr)
                return []
    return []


VERIFY_GAP = 200   # tolère un total éloigné du montant (colonnes) — le nom marchand reste exigé


def verify_pdf(pdf_txt: str, amt_strs: list, tokens: list, sender: str, subject: str,
               targets: list = None) -> bool:
    """Accepte le PDF SEULEMENT si le montant est un vrai total ET un jeton marchand est présent.

    `targets` (montants numériques) active EN PLUS le repli « somme » : un remboursement unique
    peut couvrir PLUSIEURS factures réunies dans le même document (Amazon 599,00 + 13,99 =
    612,99) — le montant de la transaction n'y figure alors comme total nulle part.
    """
    if not pdf_txt:
        return False
    # allow_bare_total : ici le nom marchand est exigé juste après (double garde), donc on peut
    # accepter un ticket de caisse « TOTAL 247.50 » sans symbole de devise.
    ok_amount = audit._pdf_has_total(pdf_txt, amt_strs, gap=VERIFY_GAP, allow_bare_total=True)
    if not ok_amount and targets:
        ok_amount = audit.totals_sum_to(pdf_txt, targets, gap=VERIFY_GAP)
    if not ok_amount:
        return False
    hay = saas.strip_accents(f"{pdf_txt}\n{sender}\n{subject}").upper()
    return any(tok in hay for tok in tokens)


def _html_body(msg) -> str:
    """Corps HTML d'un email (reçus Square/Sunday/Bolt… sans PJ PDF)."""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            if payload:
                return payload.decode(part.get_content_charset() or "utf-8", "replace")
    return ""


class Box:
    """Connexion IMAP + ses identifiants, pour pouvoir la RÉTABLIR si Gmail la coupe."""

    def __init__(self, mail, tag: str, user: str, pwd: str):
        self.mail, self.tag, self.user, self.pwd = mail, tag, user, pwd

    def reconnect(self) -> bool:
        try:
            self.mail.logout()
        except Exception:
            pass
        try:
            self.mail = fetch.connect_imap(self.user, self.pwd)
            print(f"    (reconnexion IMAP [{self.tag}] OK)", file=sys.stderr)
            return True
        except Exception as exc:
            print(f"    ⚠ reconnexion IMAP [{self.tag}] impossible : {exc}", file=sys.stderr)
            return False

    def logout(self) -> None:
        try:
            self.mail.logout()
        except Exception:
            pass


def _connect_boxes() -> list:
    """Un `Box` par boîte Gmail configurée (identifiants présents)."""
    import os
    boxes = []
    for env_user, env_pass, tag in audit.GMAIL_BOXES:
        user = (os.environ.get(env_user) or "").strip()
        pwd = (os.environ.get(env_pass) or "").strip().replace(" ", "")
        if not user or not pwd:
            continue
        try:
            boxes.append(Box(fetch.connect_imap(user, pwd), tag, user, pwd))
        except Exception as exc:
            print(f"  ⚠ connexion {env_user} impossible : {exc}", file=sys.stderr)
    return boxes


def collect_verified_receipts(tx: dict, boxes: list, tokens: list) -> list:
    """Liste TOUS les justificatifs vérifiés : [{pdf, tag, msgid, sender, subject}].

    Ne tranche pas l'ambiguïté (≥2 emails) : renvoie tout, à charge de l'appelant de
    choisir (auto si un seul email, ou sélection manuelle côté dashboard).
    """
    targets = audit._amount_targets(tx)
    amt_strs = audit._amount_strings(targets)
    if not amt_strs:
        return []
    try:
        ref = datetime.strptime(tx["settled_at"][:10], "%Y-%m-%d").date()
    except (ValueError, KeyError, TypeError):
        return []
    d_from = (ref - timedelta(days=audit.DATE_WINDOW)).strftime("%Y/%m/%d")
    d_to = (ref + timedelta(days=audit.DATE_WINDOW)).strftime("%Y/%m/%d")

    verified = []
    for box in boxes:
        tag = box.tag
        uids = search_receipt_uids(box, tokens, amt_strs, d_from, d_to)
        if not uids:
            # REPLI PHOTO — un justificatif PHOTOGRAPHIÉ (JPEG, corps de mail vide) ne porte son
            # montant QUE dans les pixels : l'index texte de Gmail ne peut pas le trouver, donc
            # la recherche par montant ne remonte rien et le mail n'est JAMAIS téléchargé (l'OCR
            # n'a jamais sa chance). On re-cherche sans le montant, jeton marchand + pièce jointe
            # exigés, puis on OCR-ise : la VÉRIFICATION en aval, elle, ne change pas.
            uids = search_receipt_uids(box, tokens, amt_strs, d_from, d_to,
                                       with_amount=False)[:MAX_FALLBACK_MSGS]
        for uid in uids:
            msg = fetch.fetch_message(box.mail, uid)
            if msg is None:
                continue
            msgid = fetch._decode(msg.get("Message-ID") or uid.decode("ascii", "replace"))
            sender = fetch._decode(msg.get("From") or "")
            subject = fetch._decode(msg.get("Subject") or "")
            got = False
            for pdf in fetch.extract_pdfs(msg, INPUT_DIR, msgid):
                if verify_pdf(audit._pdftotext(pdf), amt_strs, tokens, sender, subject,
                              targets=targets):
                    verified.append({"pdf": pdf, "tag": tag, "msgid": msgid,
                                     "sender": sender, "subject": subject})
                    got = True
            if not got:
                # Reçu HTML sans PJ PDF (Square/Sunday/Bolt…) : rend le corps en PDF puis vérifie.
                html = _html_body(msg)
                if html:
                    tagname = "".join(ch for ch in msgid if ch.isalnum())[:10] or "x"
                    dest = INPUT_DIR / f"recu_{tagname}.pdf"
                    try:
                        if saas.render_html_to_pdf(html, dest) and verify_pdf(
                                audit._pdftotext(dest), amt_strs, tokens, sender, subject,
                                targets=targets):
                            verified.append({"pdf": dest, "tag": tag, "msgid": msgid,
                                             "sender": sender, "subject": subject})
                    except Exception:
                        pass
    return verified


def find_receipt(tx: dict, boxes: list, tokens: list) -> tuple:
    """Cherche un justificatif vérifié pour la transaction. Renvoie (pdf_path, tag, status).

    status ∈ {"found", "none", "ambiguous"}. "ambiguous" = ≥2 emails distincts passent la vérif.
    """
    verified = collect_verified_receipts(tx, boxes, tokens)
    if not verified:
        return (None, "", "none")
    if len({v["msgid"] for v in verified}) > 1:         # ≥2 emails distincts → on ne devine pas
        return (None, "", "ambiguous")
    # Un seul email : préfère un PDF qui « ressemble à une facture », sinon le premier.
    best = next((v for v in verified if saas.looks_like_invoice(audit._pdftotext(v["pdf"]))), verified[0])
    return (best["pdf"], best["tag"], "found")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rapprochement piloté par la transaction Qonto.")
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--dry-run", action="store_true",
                        help="N'attache RIEN : liste les propositions dans reports/qonto/proposals_<date>.md.")
    args = parser.parse_args()

    load_dotenv()
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    client = QontoClient(*load_qonto_credentials())
    processed = _load_json(PROCESSED_PATH)

    candidates = client.fetch_unreconciled_expenses(args.since)
    print(f"{len(candidates)} transaction(s) sans justificatif depuis {args.since}.", file=sys.stderr)
    boxes = _connect_boxes()
    if not boxes:
        print("  Aucune boîte Gmail configurée — arrêt.", file=sys.stderr)
        return 1

    proposals, attached, ambiguous, notoken = [], [], [], []
    for tx in candidates:
        tx_id = tx.get("id")
        if tx_id in processed:
            continue
        try:
            if client.get_transaction_attachments(tx_id):     # vérité LIVE : déjà documentée
                processed[tx_id] = {"label": tx.get("label"), "status": "deja_pj"}
                continue
        except Exception:
            pass
        tokens = tx_merchant_tokens(tx)
        if not tokens:
            notoken.append(tx)
            continue

        pdf, tag, status = find_receipt(tx, boxes, tokens)
        if status == "ambiguous":
            ambiguous.append((tx, tokens))
            print(f"  ? {tx.get('label')} {tx.get('amount')} : ≥2 emails, à confirmer", file=sys.stderr)
            continue
        if status != "found":
            continue

        if args.dry_run:
            proposals.append((tx, pdf, tag))
            print(f"  ~ {tx.get('label')} {tx.get('amount')} -> {pdf.name} [{tag}]", file=sys.stderr)
            continue

        try:
            if client.get_transaction_attachments(tx_id):     # re-check juste avant l'écriture
                processed[tx_id] = {"label": tx.get("label"), "status": "deja_pj"}
                continue
            client.upload_attachment(tx_id, pdf)
        except Exception as exc:
            print(f"  ⚠ attache impossible ({tx.get('label')}) : {exc}", file=sys.stderr)
            continue
        processed[tx_id] = {"label": tx.get("label"), "pdf": pdf.name, "box": tag,
                            "amount": tx.get("amount")}
        attached.append((tx, pdf, tag))
        print(f"  ✓ {tx.get('label')} {tx.get('amount')} -> {pdf.name} [{tag}]", file=sys.stderr)

    for box in boxes:
        box.logout()

    if not args.dry_run:
        PROCESSED_PATH.write_text(json.dumps(processed, ensure_ascii=False, indent=2), encoding="utf-8")

    # Rapport / propositions
    stamp = date.today().isoformat()
    if args.dry_run:
        lines = [f"# Propositions rapprochement Qonto ({stamp}) — DRY-RUN, rien attaché", "",
                 "| Transaction | Montant | -> PDF | Boîte |", "|---|---|---|---|"]
        lines += [f"| {tx.get('label')} | {tx.get('amount')} {tx.get('currency')} | {pdf.name} | {tag} |"
                  for tx, pdf, tag in proposals]
        (OUT_DIR / f"proposals_{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\nRésumé : {len(attached)} attaché(s), {len(proposals)} proposé(s) [dry-run], "
          f"{len(ambiguous)} ambigu(s), {len(notoken)} sans jeton marchand.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
