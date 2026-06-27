#!/usr/bin/env python3
"""Télécharge les reçus PDF UberEats depuis Gmail via IMAP (stdlib uniquement).

Cherche les emails de "Reçu Uber" depuis --since, extrait le lien PDF
du corps de l'email, télécharge chaque PDF dans --out-dir.

Prérequis dans .env :
    UBEREATS_GMAIL=naelkodmani@gmail.com
    UBEREATS_GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx

Usage :
    python3 scripts/fetch_ubereats_gmail.py \\
        --out-dir reports/ubereats/input \\
        [--since YYYY-MM-DD] [--dry-run]
"""
from __future__ import annotations

import argparse
import base64
import email
import email.header
import imaplib
import os
import re
import sys
from pathlib import Path
from typing import List, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recon.config import load_dotenv  # noqa: E402

DEFAULT_SINCE = "2026-04-01"
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993


def load_credentials() -> tuple[str, str]:
    load_dotenv()
    gmail = os.environ.get("UBEREATS_GMAIL", "").strip()
    password = os.environ.get("UBEREATS_GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
    if not gmail or not password:
        raise SystemExit(
            "UBEREATS_GMAIL / UBEREATS_GMAIL_APP_PASSWORD manquants dans .env."
        )
    return gmail, password


def connect_imap(gmail: str, password: str) -> imaplib.IMAP4_SSL:
    """Connexion IMAP SSL à Gmail."""
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(gmail, password)
    mail.select("inbox")
    return mail


def search_emails(mail: imaplib.IMAP4_SSL, since: str) -> List[bytes]:
    """Cherche les emails UberEats depuis --since. Retourne les IDs de messages."""
    # Convertir YYYY-MM-DD → DD-Mon-YYYY (format IMAP)
    from datetime import datetime
    dt = datetime.strptime(since, "%Y-%m-%d")
    imap_date = dt.strftime("%d-%b-%Y")

    # Recherche par expéditeur + date.
    _, data = mail.search(None, f'FROM "uber" SINCE "{imap_date}"')
    ids = data[0].split() if data[0] else []
    return ids


def get_email_body(mail: imaplib.IMAP4_SSL, msg_id: bytes) -> tuple[str, str]:
    """Retourne (subject, html_body) de l'email."""
    _, data = mail.fetch(msg_id, "(RFC822)")
    raw = data[0][1]
    msg = email.message_from_bytes(raw)

    # Décode le sujet.
    subject_parts = email.header.decode_header(msg.get("Subject", ""))
    subject = ""
    for part, enc in subject_parts:
        if isinstance(part, bytes):
            subject += part.decode(enc or "utf-8", errors="replace")
        else:
            subject += part

    # Extrait le corps HTML.
    html = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    break
    else:
        if msg.get_content_type() == "text/html":
            payload = msg.get_payload(decode=True)
            if payload:
                html = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

    return subject, html


def find_pdf_url(html: str) -> Optional[str]:
    """Extrait le lien de téléchargement PDF depuis le corps HTML de l'email.

    UberEats utilise des liens de tracking (email.mgt.uber.com, click.uber.com)
    qui redirigent vers le vrai PDF. On cherche le lien dont le texte visible
    contient "PDF" ou "Téléchargez".
    """
    # On parcourt CHAQUE ancre <a>…</a> et on retient celle dont le TEXTE VISIBLE évoque
    # le reçu/PDF. L'ancienne regex (DOTALL + fenêtre de 5 balises) traversait plusieurs
    # ancres et capturait par erreur le 1er href (logo « Uber Eats ») qui redirige vers
    # UNE AUTRE commande → mauvais reçu téléchargé / reçu manquant.
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']{20,})["\'][^>]*>(.*?)</a>',
                         html, re.IGNORECASE | re.DOTALL):
        url, inner = m.group(1), m.group(2)
        if any(x in url.lower() for x in ["unsubscribe", "optout", "mailto", "preference"]):
            continue
        text = re.sub(r"<[^>]+>", " ", inner)
        if re.search(r"PDF|t[eé]l[eé]charg|download|re[çc]u|receipt", text, re.IGNORECASE):
            return url

    # Lien direct .pdf
    m = re.search(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', html, re.IGNORECASE)
    if m:
        return m.group(1)

    return None


def get_receipt_url(tracking_url: str) -> Optional[str]:
    """Suit la redirection du lien de tracking Uber → URL de reçu UberEats."""
    import subprocess
    result = subprocess.run(
        ["curl", "-s", "-L", "-o", "/dev/null", "-w", "%{url_effective}",
         "--max-time", "15", tracking_url],
        capture_output=True, text=True,
    )
    final = result.stdout.strip()
    if "ubereats.com/orders/" in final:
        return final
    return None


def download_receipts_playwright(receipt_urls: List[str], out_dir: Path) -> int:
    """Utilise Playwright avec la session UberEats pour générer des PDFs des pages reçu."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Playwright absent. pip3 install playwright && python3 -m playwright install chromium")

    session_path = Path(".ubereats_session.json")
    if not session_path.exists():
        print("  Session UberEats introuvable. Lancer d'abord : python3 scripts/fetch_ubereats_invoices.py --headed",
              file=sys.stderr)
        return 0

    downloaded = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            storage_state=str(session_path),
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        manifest_rows = []
        for i, url in enumerate(receipt_urls):
            # Extraire UUID depuis l'URL.
            m = re.search(r'/orders/([a-f0-9-]{36})', url)
            if not m:
                continue
            uuid = m.group(1)
            dest = out_dir / f"ubereats_{uuid[:12]}.pdf"
            if dest.exists():
                print(f"  → [{i+1}] déjà présent : {dest.name}", file=sys.stderr)
                downloaded += 1
                # Lire manifest si besoin depuis fichier existant.
                continue

            # Les liens « téléchargez ce PDF » résolvent vers /orders/<uuid>/download-receipt
            # = téléchargement DIRECT (page.goto lève « Download is starting »). On capte
            # donc le download ; repli sur le rendu HTML si l'URL est une page de reçu.
            got = False
            try:
                with page.expect_download(timeout=25000) as di:
                    try:
                        page.goto(url, timeout=20000)
                    except Exception:
                        pass  # « Download is starting » attendu pour /download-receipt
                di.value.save_as(str(dest))
                got = dest.exists() and dest.stat().st_size > 1000
            except Exception:
                got = False

            if not got:
                # Repli : l'URL est une page HTML de reçu → rendu en PDF.
                try:
                    page.goto(url, timeout=20000)
                    page.wait_for_load_state("load", timeout=20000)
                    page.wait_for_timeout(2000)
                    page.pdf(path=str(dest), format="A4", print_background=True)
                    got = dest.exists() and dest.stat().st_size > 1000
                except Exception as exc:
                    print(f"  ✗ [{i+1}] erreur : {exc}", file=sys.stderr)

            if got:
                downloaded += 1
                print(f"  ✓ [{i+1}] {dest.name}", file=sys.stderr)
                # Montant/date parsés par reconcile_ubereats (pdftotext) — pas besoin du DOM.
            else:
                dest.unlink(missing_ok=True)
                print(f"  ✗ [{i+1}] échec téléchargement ({url[:60]}…)", file=sys.stderr)

        # Écrire manifest.csv pour reconcile_ubereats.py.
        if manifest_rows:
            import csv as _csv
            manifest_path = out_dir.parent / "manifest.csv"
            with manifest_path.open("w", newline="", encoding="utf-8") as f:
                w = _csv.DictWriter(f, fieldnames=["filename", "date", "amount", "invoice_id"])
                w.writeheader()
                w.writerows(manifest_rows)
            print(f"  manifest.csv écrit ({len(manifest_rows)} entrées)", file=sys.stderr)

        context.close()
        browser.close()
    return downloaded


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Télécharge les reçus PDF UberEats depuis Gmail (IMAP)."
    )
    parser.add_argument("--out-dir", default="reports/ubereats/input")
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--dry-run", action="store_true",
                        help="Liste les emails sans télécharger.")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    gmail, password = load_credentials()
    print(f"Connexion IMAP Gmail ({gmail})…", file=sys.stderr)

    try:
        mail = connect_imap(gmail, password)
        print("Connecté. Recherche des emails…", file=sys.stderr)
    except imaplib.IMAP4.error as e:
        raise SystemExit(
            f"Connexion IMAP échouée : {e}\n"
            "Vérifier UBEREATS_GMAIL et UBEREATS_GMAIL_APP_PASSWORD dans .env."
        )

    msg_ids = search_emails(mail, args.since)
    print(f"Recherche terminée.", file=sys.stderr)
    print(f"{len(msg_ids)} email(s) Uber trouvé(s) depuis {args.since}.", file=sys.stderr)

    if not msg_ids:
        mail.logout()
        return 0

    receipt_urls: List[str] = []
    for i, mid in enumerate(msg_ids):
        subject, html = get_email_body(mail, mid)

        # Filtrer : garder uniquement les reçus UberEats.
        if not re.search(r'reçu|receipt|facture|invoice|uber.?eats', subject, re.IGNORECASE):
            if not re.search(r'reçu|receipt|facture', html[:500], re.IGNORECASE):
                continue

        if args.dry_run:
            print(f"  [{i+1}] {subject[:60]}", file=sys.stderr)
            continue

        # Extraire le lien de tracking → URL du reçu UberEats.
        tracking_url = find_pdf_url(html)
        if not tracking_url:
            print(f"  ✗ [{i+1}] pas de lien trouvé — {subject[:50]}", file=sys.stderr)
            continue

        print(f"  [{i+1}/{len(msg_ids)}] résolution lien…", file=sys.stderr)
        receipt_url = get_receipt_url(tracking_url)
        if receipt_url:
            receipt_urls.append(receipt_url)
        else:
            print(f"  ✗ [{i+1}] redirection introuvable", file=sys.stderr)

    mail.logout()

    if args.dry_run:
        print(f"\n{len([m for m in msg_ids])} email(s) listés.", file=sys.stderr)
        return 0

    print(f"\n{len(receipt_urls)} page(s) reçu à télécharger via Playwright…", file=sys.stderr)
    downloaded = download_receipts_playwright(receipt_urls, out_dir)
    print(f"\n{downloaded}/{len(receipt_urls)} PDF(s) téléchargé(s) dans {out_dir}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
