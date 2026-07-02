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
from recon.normalize import label_merchant_tokens  # noqa: E402
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


def build_receipt_query(tokens: list, amt_strs: list, d_from: str, d_to: str) -> str:
    """Requête X-GM-RAW : (montants) ET (jetons marchands) ET fenêtre de dates, expéditeurs exclus.

    PAS de guillemets internes (sinon X-GM-RAW = « Could not parse command »). Le montant TROUVE
    l'email, le jeton marchand le DÉSAMBIGUÏSE (ex. deux reçus à 40 € : on garde celui « UBER »).
    """
    amt_grp = "(" + " OR ".join(amt_strs) + ")"
    tok_grp = "(" + " OR ".join(tokens) + ")"
    excl = " ".join(f"-from:{d}" for d in audit._SEARCH_EXCLUDE)
    return f"{amt_grp} {tok_grp} {excl} after:{d_from} before:{d_to}"


def search_receipt_uids(mail, tokens: list, amt_strs: list, d_from: str, d_to: str) -> list:
    """UIDs des emails dont le montant ET un jeton marchand correspondent (fenêtre de dates)."""
    q = build_receipt_query(tokens, amt_strs, d_from, d_to)
    try:
        typ, data = mail.uid("search", None, "X-GM-RAW", f'"{q}"')
    except Exception as exc:
        print(f"    (recherche Gmail échouée : {exc})", file=sys.stderr)
        return []
    return data[0].split() if (typ == "OK" and data and data[0]) else []


def verify_pdf(pdf_txt: str, amt_strs: list, tokens: list, sender: str, subject: str) -> bool:
    """Accepte le PDF SEULEMENT si le montant est un vrai total ET un jeton marchand est présent."""
    if not pdf_txt or not audit._pdf_has_total(pdf_txt, amt_strs):
        return False
    hay = saas.strip_accents(f"{pdf_txt}\n{sender}\n{subject}").upper()
    return any(tok in hay for tok in tokens)


def _connect_boxes() -> list:
    """(mail, tag) pour chaque boîte Gmail configurée (identifiants présents)."""
    import os
    boxes = []
    for env_user, env_pass, tag in audit.GMAIL_BOXES:
        user = (os.environ.get(env_user) or "").strip()
        pwd = (os.environ.get(env_pass) or "").strip().replace(" ", "")
        if not user or not pwd:
            continue
        try:
            boxes.append((fetch.connect_imap(user, pwd), tag))
        except Exception as exc:
            print(f"  ⚠ connexion {env_user} impossible : {exc}", file=sys.stderr)
    return boxes


def find_receipt(tx: dict, boxes: list, tokens: list) -> tuple:
    """Cherche un justificatif vérifié pour la transaction. Renvoie (pdf_path, tag, status).

    status ∈ {"found", "none", "ambiguous"}. "ambiguous" = ≥2 emails distincts passent la vérif.
    """
    amt_strs = audit._amount_strings(audit._amount_targets(tx))
    if not amt_strs:
        return (None, "", "none")
    try:
        ref = datetime.strptime(tx["settled_at"][:10], "%Y-%m-%d").date()
    except (ValueError, KeyError, TypeError):
        return (None, "", "none")
    d_from = (ref - timedelta(days=audit.DATE_WINDOW)).strftime("%Y/%m/%d")
    d_to = (ref + timedelta(days=audit.DATE_WINDOW)).strftime("%Y/%m/%d")

    verified = []          # (pdf_path, tag, email_key)
    for mail, tag in boxes:
        for uid in search_receipt_uids(mail, tokens, amt_strs, d_from, d_to):
            msg = fetch.fetch_message(mail, uid)
            if msg is None:
                continue
            msgid = fetch._decode(msg.get("Message-ID") or uid.decode("ascii", "replace"))
            sender = fetch._decode(msg.get("From") or "")
            subject = fetch._decode(msg.get("Subject") or "")
            for pdf in fetch.extract_pdfs(msg, INPUT_DIR, msgid):
                if verify_pdf(audit._pdftotext(pdf), amt_strs, tokens, sender, subject):
                    verified.append((pdf, tag, msgid))
    if not verified:
        return (None, "", "none")
    if len({v[2] for v in verified}) > 1:               # ≥2 emails distincts → on ne devine pas
        return (None, "", "ambiguous")
    # Un seul email : préfère un PDF qui « ressemble à une facture », sinon le premier.
    best = next((v for v in verified if saas.looks_like_invoice(audit._pdftotext(v[0]))), verified[0])
    return (best[0], best[1], "found")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rapprochement piloté par la transaction Qonto.")
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--dry-run", action="store_true",
                        help="N'attache RIEN : liste les propositions dans reports/qonto/proposals_<date>.md.")
    args = parser.parse_args()

    load_dotenv()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
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
        tokens = label_merchant_tokens(tx.get("label", ""))
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

    for mail, _ in boxes:
        try:
            mail.logout()
        except Exception:
            pass

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
