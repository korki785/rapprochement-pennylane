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
# Photos de reçus (justificatif pris en photo) : converties en PDF puis OCR-isées comme un
# scan. Sans ça, un reçu envoyé en JPEG est invisible pour tout le pipeline.
IMAGE_TYPES = ("image/jpeg", "image/jpg", "image/png", "image/heic", "image/heif")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".heic", ".heif")
MAX_IMAGE_BYTES = 20_000_000  # une photo de téléphone dépasse largement MAX_PDF_BYTES


def load_credentials() -> Tuple[str, str]:
    load_dotenv()
    gmail = os.environ.get("SAAS_GMAIL", "").strip()
    password = os.environ.get("SAAS_GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
    if not gmail or not password:
        raise SystemExit("SAAS_GMAIL / SAAS_GMAIL_APP_PASSWORD manquants dans .env.")
    return gmail, password


# Boîtes mail SECONDAIRES balayées en plus de SAAS_GMAIL. Certaines factures
# fournisseur arrivent sur une autre adresse qui n'est PAS une boîte Maison Darwish
# (ex. Lovable -> nael@parishouseofprayer.com, boîte perso/autre orga). On NE balaye
# donc PAS tous les PDF (pollution par factures sans lien) : on RESTREINT aux
# expéditeurs connus via `senders` (domaines). Champ par boîte :
#   email_var, pass_var, senders(domaines à filtrer en From).
_EXTRA_ACCOUNT_ENV = [
    {"email_var": "LOVABLE_GMAIL", "pass_var": "LOVABLE_GMAIL_APP_PASSWORD",
     "senders": ["lovable.dev", "lovable.app"]},
]


def load_extra_accounts() -> List[Tuple[str, str, Optional[List[str]]]]:
    """(email, app_password, senders) des boîtes secondaires renseignées dans .env."""
    load_dotenv()
    out: List[Tuple[str, str, Optional[List[str]]]] = []
    for acc in _EXTRA_ACCOUNT_ENV:
        gmail = os.environ.get(acc["email_var"], "").strip()
        password = os.environ.get(acc["pass_var"], "").strip().replace(" ", "")
        if gmail and password:
            out.append((gmail, password, acc.get("senders")))
    return out


def all_accounts() -> List[Tuple[str, str, Optional[List[str]]]]:
    """Boîte principale (sweep complet, senders=None) + boîtes secondaires (filtrées)."""
    gmail, password = load_credentials()
    return [(gmail, password, None)] + load_extra_accounts()


def connect_imap(gmail: str, password: str) -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(gmail, password)
    mail.select("inbox")
    return mail


def _gmail_date(since: str) -> str:
    return datetime.strptime(since, "%Y-%m-%d").strftime("%Y/%m/%d")


def search_pdf_emails(mail: imaplib.IMAP4_SSL, since: str,
                      senders: Optional[List[str]] = None) -> List[bytes]:
    """UIDs des emails avec PJ PDF depuis `since` (recherche Gmail X-GM-RAW).

    `senders` : si fourni, restreint aux expéditeurs (domaines) listés — utilisé pour
    les boîtes secondaires qui ne sont pas des boîtes Maison Darwish (évite d'aspirer
    des factures sans lien). None = toute la boîte (boîte principale).
    """
    query = f'has:attachment filename:pdf after:{_gmail_date(since)}'
    if senders:
        froms = " OR ".join(f"from:{d}" for d in senders)
        query = f'has:attachment filename:pdf ({froms}) after:{_gmail_date(since)}'
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


def _ocr_sidecar(src_image: Path, dest_pdf: Path) -> None:
    """OCR-ise l'image D'ORIGINE et écrit le cache texte du PDF converti.

    CRITIQUE : ne pas laisser l'OCR passer par le PDF. `_ocr_pdf` re-rastérise à 200 dpi, ce qui
    détruit la colonne de prix d'une photo de reçu (ticket La Pause 4032 px : « 247.50 » lisible
    en natif, absent après re-rastérisation) — le montant disparaît et le rapprochement échoue.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from reconcile_fournisseurs import _cache_path, _ensure_ocr_bin
        import subprocess

        binp = _ensure_ocr_bin()
        if not binp:
            return
        r = subprocess.run([binp, str(src_image)], capture_output=True, text=True, timeout=120)
        txt = r.stdout or ""
        if not txt.strip():
            return
        cache = _cache_path(dest_pdf)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(txt, encoding="utf-8")
    except Exception:
        pass  # sans OCR, le PDF reste attachable — seul le rapprochement auto est perdu


def _image_to_pdf(payload: bytes, suffix: str, dest: Path) -> bool:
    """Convertit une image en PDF via `sips` (macOS). True si le PDF est écrit.

    Le reste du pipeline est PDF-centrique (`_pdftotext` + repli OCR Vision, `upload_attachment`) :
    convertir en amont évite de dupliquer ce chemin pour les images.
    """
    import shutil
    import subprocess
    import tempfile

    sips = shutil.which("sips") or "/usr/bin/sips"
    if not Path(sips).exists():
        return False
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / f"img{suffix or '.jpg'}"
        src.write_bytes(payload)
        try:
            subprocess.run([sips, "-s", "format", "pdf", str(src), "--out", str(dest)],
                           check=True, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return False
        if not (dest.exists() and dest.stat().st_size > 500):
            return False
        _ocr_sidecar(src, dest)
    return True


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
        low = filename.lower()
        is_pdf = ctype == "application/pdf" or low.endswith(".pdf")
        is_image = ctype in IMAGE_TYPES or low.endswith(IMAGE_EXTS)
        if not is_pdf and not is_image:
            continue
        payload = part.get_payload(decode=True)
        cap = MAX_PDF_BYTES if is_pdf else MAX_IMAGE_BYTES
        if not payload or len(payload) < 500 or len(payload) > cap:
            continue
        dest = out_dir / _safe_pdf_name(filename)
        if dest.exists():
            tag = "".join(c for c in msgid if c.isalnum())[:8] or "x"
            dest = out_dir / f"{tag}_{dest.name}"
        if is_pdf:
            dest.write_bytes(payload)
        else:
            suffix = next((e for e in IMAGE_EXTS if low.endswith(e)), ".jpg")
            if not _image_to_pdf(payload, suffix, dest):
                continue
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
    """Balaye TOUTES les boîtes (principale + secondaires), parse, garde les factures.

    Idempotence par Message-ID, partagée entre boîtes (`already` accumulé) : un même
    mail transféré sur deux adresses n'est gardé qu'une fois.
    """
    vendors = saas.load_vendors()
    all_entries: List[Dict] = []
    already = set(processed_msgids)
    for gmail, password, senders in all_accounts():
        print(f"Connexion IMAP Gmail ({gmail})…", file=sys.stderr)
        mail = connect_imap(gmail, password)
        entries = _sweep_mailbox(mail, out_dir, since, already, vendors, dry_run, senders)
        mail.logout()
        already |= {e["msgid"] for e in entries}
        all_entries.extend(entries)
    return all_entries


def _sweep_mailbox(mail: imaplib.IMAP4_SSL, out_dir: Path, since: str,
                   already: set, vendors, dry_run: bool,
                   senders: Optional[List[str]] = None) -> List[Dict]:
    """Balaye une boîte : PDF joints (factures) + reçus HTML. Renvoie les entrées manifest.

    `senders` restreint le balayage PDF aux expéditeurs connus (boîtes secondaires).
    La 2e passe reçus HTML n'est faite que sur la boîte principale (senders=None).
    """
    uids = search_pdf_emails(mail, since, senders)
    print(f"{len(uids)} email(s) avec PDF depuis {since}.", file=sys.stderr)

    entries: List[Dict] = []
    kept = skipped = 0
    for uid in uids:
        # En-têtes d'abord (léger) : on saute les emails déjà traités SANS télécharger le corps.
        head = fetch_headers(mail, uid)
        if head is None:
            continue
        msgid = _decode(head.get("Message-ID", "")).strip() or f"uid:{uid.decode()}"
        if msgid in already:
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
        # Marchand lu DANS le corps du PDF (ex. reçu Airbnb auto-transféré : expéditeur =
        # soi-même, donc aucun alias marchand sans ça → non rapproché malgré montant exact).
        aliases += saas.vendor_aliases_from_text(saas.extract_text(primary), vendors)
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
    # Uniquement sur la boîte principale (les boîtes secondaires sont filtrées par vendeur).
    if senders is None:
        seen_ids = already | {e["msgid"] for e in entries}
        entries.extend(fetch_html_receipts(mail, out_dir, since, seen_ids, dry_run))

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
    """Message-IDs des emails avec PDF depuis `since`, TOUTES boîtes (pré-check poller)."""
    ids: List[str] = []
    for gmail, password, senders in all_accounts():
        mail = connect_imap(gmail, password)
        for uid in search_pdf_emails(mail, since, senders):
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
