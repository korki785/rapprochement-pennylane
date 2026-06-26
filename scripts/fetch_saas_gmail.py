#!/usr/bin/env python3
"""Balaye TOUS les emails avec PJ PDF (Gmail IMAP) et parse les factures (flux saas v2).

Au lieu de filtrer par expéditeur, on récupère tous les PDF depuis `--since` via la
recherche Gmail `X-GM-RAW "has:attachment filename:pdf"`. Chaque PDF est parsé
(montant/devise/date), on déduit le fournisseur (table + nom d'expéditeur), et on
ne garde que ce qui ressemble à une facture (ou vient d'un fournisseur connu).

Idempotence par Message-ID. Garde-fous (looks_like_invoice / has_company_id) côté reconcile.

Prérequis .env :
    SAAS_GMAIL=hello@maisondarwish.com
    SAAS_GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx

Usage :
    python3 scripts/fetch_saas_gmail.py --out-dir reports/saas/input [--since YYYY-MM-DD] [--dry-run]
"""
from __future__ import annotations

import argparse
import email
import email.header
import email.utils
import imaplib
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon.config import load_dotenv  # noqa: E402
from recon import saas  # noqa: E402

DEFAULT_SINCE = "2026-04-01"
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
MAX_PDF_BYTES = 4_000_000  # ignore les gros PDF (riders/design) — les factures sont petites


def load_credentials() -> Tuple[str, str]:
    load_dotenv()
    gmail = os.environ.get("SAAS_GMAIL", "").strip()
    password = os.environ.get("SAAS_GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
    if not gmail or not password:
        raise SystemExit("SAAS_GMAIL / SAAS_GMAIL_APP_PASSWORD manquants dans .env.")
    return gmail, password


def connect_imap(gmail: str, password: str) -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(gmail, password)
    mail.select("inbox")
    return mail


def _gmail_date(since: str) -> str:
    return datetime.strptime(since, "%Y-%m-%d").strftime("%Y/%m/%d")


def search_pdf_emails(mail: imaplib.IMAP4_SSL, since: str) -> List[bytes]:
    """UIDs de tous les emails avec PJ PDF depuis `since` (recherche Gmail X-GM-RAW)."""
    query = f'has:attachment filename:pdf after:{_gmail_date(since)}'
    try:
        _, data = mail.uid("search", None, "X-GM-RAW", f'"{query}"')
    except imaplib.IMAP4.error as exc:
        print(f"  ⚠ recherche X-GM-RAW échouée ({exc})", file=sys.stderr)
        return []
    return data[0].split() if data and data[0] else []


def _decode(value: str) -> str:
    out = ""
    for part, enc in email.header.decode_header(value or ""):
        out += part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part
    return out


def _safe_pdf_name(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in " ._-" else "_" for c in (name or "")).strip()
    keep = keep or "facture"
    return keep if keep.lower().endswith(".pdf") else keep + ".pdf"


def fetch_message(mail: imaplib.IMAP4_SSL, uid: bytes) -> Optional[email.message.Message]:
    _, data = mail.uid("fetch", uid, "(RFC822)")
    if not data or not data[0]:
        return None
    return email.message_from_bytes(data[0][1])


def fetch_headers(mail: imaplib.IMAP4_SSL, uid: bytes) -> Optional[email.message.Message]:
    """En-têtes seuls (rapide) — pour le dry-run."""
    _, data = mail.uid("fetch", uid, "(BODY.PEEK[HEADER])")
    if not data or not data[0]:
        return None
    return email.message_from_bytes(data[0][1])


def extract_pdfs(msg: email.message.Message, out_dir: Path, msgid: str) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: List[Path] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = _decode(part.get_filename() or "")
        ctype = (part.get_content_type() or "").lower()
        if ctype != "application/pdf" and not filename.lower().endswith(".pdf"):
            continue
        payload = part.get_payload(decode=True)
        if not payload or len(payload) < 500 or len(payload) > MAX_PDF_BYTES:
            continue
        dest = out_dir / _safe_pdf_name(filename)
        if dest.exists():
            tag = "".join(c for c in msgid if c.isalnum())[:8] or "x"
            dest = out_dir / f"{tag}_{dest.name}"
        dest.write_bytes(payload)
        paths.append(dest)
    return paths


def _pick_primary(parsed_pdfs: List[Tuple[Path, "saas.ParsedPdf"]]) -> Tuple[Path, "saas.ParsedPdf"]:
    """PDF de référence : préfère une vraie facture nommée invoice/facture, puis is_invoice."""
    for kw in ("invoice", "facture"):
        for p, parsed in parsed_pdfs:
            if kw in p.name.lower() and parsed.is_invoice:
                return p, parsed
    for p, parsed in parsed_pdfs:
        if parsed.is_invoice:
            return p, parsed
    return parsed_pdfs[0]


def fetch_invoices(out_dir: Path, since: str, processed_msgids: set,
                   dry_run: bool = False) -> List[Dict]:
    """Balaye les emails avec PDF, parse, garde les factures. Renvoie les entrées manifest."""
    gmail, password = load_credentials()
    print(f"Connexion IMAP Gmail ({gmail})…", file=sys.stderr)
    mail = connect_imap(gmail, password)
    vendors = saas.load_vendors()

    uids = search_pdf_emails(mail, since)
    print(f"{len(uids)} email(s) avec PDF depuis {since}.", file=sys.stderr)

    entries: List[Dict] = []
    kept = skipped = 0
    for uid in uids:
        # En-têtes d'abord (léger) : on saute les emails déjà traités SANS télécharger le corps.
        head = fetch_headers(mail, uid)
        if head is None:
            continue
        msgid = _decode(head.get("Message-ID", "")).strip() or f"uid:{uid.decode()}"
        if msgid in processed_msgids:
            continue
        from_hdr = _decode(head.get("From", ""))
        subject = _decode(head.get("Subject", ""))
        msg = head if dry_run else fetch_message(mail, uid)
        if msg is None:
            continue
        email_date = ""
        try:
            dt = email.utils.parsedate_to_datetime(msg.get("Date", ""))
            email_date = dt.date().isoformat() if dt else ""
        except (TypeError, ValueError):
            pass

        vendor = saas.match_vendor(from_hdr, subject, vendors)

        if dry_run:
            tag = vendor.name if vendor else "?"
            print(f"    [{tag}] {subject[:55]} — {from_hdr[:35]} — {email_date}", file=sys.stderr)
            continue

        pdfs = extract_pdfs(msg, out_dir, msgid)
        if not pdfs:
            continue
        parsed_pdfs = [(p, saas.parse_pdf(p)) for p in pdfs]
        invoice_pdfs = [(p, pr) for p, pr in parsed_pdfs if pr.is_invoice]

        # On garde si : au moins un PDF ressemble à une facture, OU fournisseur connu.
        if not invoice_pdfs and vendor is None:
            for p, _ in parsed_pdfs:
                p.unlink(missing_ok=True)
            skipped += 1
            continue

        usable = invoice_pdfs or parsed_pdfs
        primary, pp = _pick_primary(usable)
        aliases = list(vendor.aliases) if vendor else []
        aliases += saas.sender_aliases(from_hdr)
        seen = set()
        aliases = [a for a in aliases if not (a in seen or seen.add(a))]

        entries.append({
            "msgid": msgid,
            "vendor": vendor.name if vendor else "inconnu",
            "sender": from_hdr,
            "subject": subject,
            "email_date": email_date,
            "amount": pp.amount,
            "currency": pp.currency,
            "date": pp.date or email_date,
            "is_invoice": True,
            "company_id": any(pr.company_id for _, pr in usable),
            "trusted": bool(vendor.trusted) if vendor else False,
            "aliases": aliases,
            "pdf_paths": [str(p) for p, _ in usable],
            "primary_pdf": str(primary),
        })
        kept += 1
        amt = f"{pp.amount} {pp.currency}" if pp.amount is not None else "montant ?"
        cid = " [MD]" if any(pr.company_id for _, pr in usable) else ""
        print(f"    ✓ [{entries[-1]['vendor']}] {primary.name}  {entries[-1]['date']}  {amt}{cid}",
              file=sys.stderr)

    # 2e passe : reçus HTML (Square/Sunday/Toast…) sans pièce jointe.
    seen_ids = processed_msgids | {e["msgid"] for e in entries}
    entries.extend(fetch_html_receipts(mail, out_dir, since, seen_ids, dry_run))

    mail.logout()
    if not dry_run:
        print(f"{kept} facture(s) PDF gardée(s), {skipped} non-facture ignoré(s).", file=sys.stderr)
    return entries


def _bodies(msg: email.message.Message) -> Tuple[str, str]:
    """(texte brut, html) du message."""
    text = html = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and not text:
                p = part.get_payload(decode=True)
                if p:
                    text = p.decode(part.get_content_charset() or "utf-8", errors="replace")
            elif ct == "text/html" and not html:
                p = part.get_payload(decode=True)
                if p:
                    html = p.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        p = msg.get_payload(decode=True)
        body = p.decode(msg.get_content_charset() or "utf-8", errors="replace") if p else ""
        if msg.get_content_type() == "text/html":
            html = body
        else:
            text = body
    return text, html


def search_html_receipts(mail: imaplib.IMAP4_SSL, since: str) -> List[bytes]:
    """UIDs des reçus HTML (Square/Sunday/Toast…) — sans filtre PJ."""
    froms = " OR ".join(f"from:{d}" for d in saas.HTML_RECEIPT_DOMAINS)
    query = f'({froms}) after:{_gmail_date(since)}'
    try:
        _, data = mail.uid("search", None, "X-GM-RAW", f'"{query}"')
    except imaplib.IMAP4.error as exc:
        print(f"  ⚠ recherche reçus HTML échouée ({exc})", file=sys.stderr)
        return []
    return data[0].split() if data and data[0] else []


def fetch_html_receipts(mail: imaplib.IMAP4_SSL, out_dir: Path, since: str,
                        already: set, dry_run: bool) -> List[Dict]:
    """Reçus HTML rendus en PDF (Chrome). Renvoie les entrées manifest."""
    uids = search_html_receipts(mail, since)
    print(f"{len(uids)} reçu(s) HTML (Square/Sunday/Toast…) depuis {since}.", file=sys.stderr)
    out: List[Dict] = []
    for uid in uids:
        head = fetch_headers(mail, uid)
        if head is None:
            continue
        msgid = _decode(head.get("Message-ID", "")).strip() or f"uid:{uid.decode()}"
        if msgid in already:
            continue
        subject = _decode(head.get("Subject", ""))
        from_hdr = _decode(head.get("From", ""))
        email_date = ""
        try:
            dt = email.utils.parsedate_to_datetime(head.get("Date", ""))
            email_date = dt.date().isoformat() if dt else ""
        except (TypeError, ValueError):
            pass
        if dry_run:
            print(f"    [reçu] {subject[:50]} — {from_hdr[:32]} — {email_date}", file=sys.stderr)
            continue
        msg = fetch_message(mail, uid)
        if msg is None:
            continue
        text, html = _bodies(msg)
        rec = saas.parse_html_receipt(text or html, subject)
        if rec.amount is None:
            continue
        tag = "".join(c for c in msgid if c.isalnum())[:10] or "rec"
        dest = out_dir / f"recu_{tag}.pdf"
        out_dir.mkdir(parents=True, exist_ok=True)
        if not saas.render_html_to_pdf(html or f"<pre>{text}</pre>", dest):
            print(f"    ✗ rendu PDF échoué : {subject[:40]}", file=sys.stderr)
            continue
        aliases = saas.receipt_vendor_aliases(rec.vendor) or saas.sender_aliases(from_hdr)
        out.append({
            "msgid": msgid, "vendor": rec.vendor or "reçu", "sender": from_hdr,
            "subject": subject, "email_date": email_date,
            "amount": rec.amount, "currency": saas.detect_currency(text or html),
            "date": rec.date or email_date, "is_invoice": True, "company_id": False,
            "trusted": False, "aliases": aliases,
            "pdf_paths": [str(dest)], "primary_pdf": str(dest),
        })
        print(f"    ✓ [reçu {rec.vendor}] {rec.amount} {out[-1]['currency']} {out[-1]['date']}",
              file=sys.stderr)
    return out


def list_message_ids(since: str) -> List[str]:
    """Message-IDs des emails avec PDF depuis `since` (pré-check poller)."""
    gmail, password = load_credentials()
    mail = connect_imap(gmail, password)
    ids: List[str] = []
    for uid in search_pdf_emails(mail, since):
        _, data = mail.uid("fetch", uid, "(BODY[HEADER.FIELDS (MESSAGE-ID)])")
        if data and data[0]:
            raw = data[0][1].decode("utf-8", errors="replace")
            mid = raw.split(":", 1)[1].strip() if ":" in raw else raw.strip()
            if mid:
                ids.append(mid)
    mail.logout()
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description="Balaye les factures PDF depuis Gmail (saas v2).")
    parser.add_argument("--out-dir", default="reports/saas/input")
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--dry-run", action="store_true", help="Liste les emails sans télécharger.")
    args = parser.parse_args()

    entries = fetch_invoices(Path(args.out_dir), args.since, processed_msgids=set(),
                             dry_run=args.dry_run)
    if not args.dry_run:
        print(f"\n{len(entries)} facture(s) en manifest.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
