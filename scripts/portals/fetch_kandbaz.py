#!/usr/bin/env python3
"""Factures Kandbaz via le portail my.kandbaz.com — login + 2FA email lue automatiquement.

Kandbaz (domiciliation) impose une **2FA par email** : à chaque connexion, un code
**alphanumérique de 6 caractères** (valable 10 min) est envoyé par `no-reply@kandbaz.co` sur
**hello@** (SAAS_GMAIL). Ce fetcher le lit **tout seul en IMAP** → connexion sans intervention.
La session est ensuite réutilisée (`.kandbaz_session.json`, pas de re-2FA).

Portail : SPA Ionic/Angular, **pas de Cloudflare**. Login `my.kandbaz.com/login`
(`input[name=email]` / `input[name=password]`, bouton `ion-button "Connexion"` ; PIÈGE Ionic :
`fill` ne déclenche pas le binding → on tape char-par-char). Factures sur **`/mes-factures`**
(`/factures` redirige vers `/accueil`) : lignes `FZ-xxxxxx | date | montant€ | Payé`.

LIMITES CONNUES (2026-07-02) :
- Les factures du portail sont **les MÊMES que celles reçues par email** (déjà rapprochées par le
  flux `saas`) → ce fetcher est un **filet de secours**, pas la source primaire.
- Le **téléchargement PDF est bloqué** par un pop-up de conformité **LCB-FT** (« mettez à jour votre
  dossier ») qui intercepte les clics tant que le dossier Kandbaz n'est pas à jour. Le listing
  (n°/date/montant) fonctionne ; le download est best-effort et échouera tant que l'overlay est là.

Usage : python3 scripts/portals/fetch_kandbaz.py [--since YYYY-MM-DD] [--dry-run] [--init-session]
"""
from __future__ import annotations

import argparse
import email
import imaplib
import os
import re
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_SCRIPTS))

from recon.config import load_dotenv  # noqa: E402
from recon.portals import session_path, credentials  # noqa: E402
from _portal_base import (  # noqa: E402
    launch_browser, save_session, dismiss_popups, write_manifest, std_args,
)

VENDOR = "kandbaz"
LOGIN_URL = "https://my.kandbaz.com/login"
FACTURES_URL = "https://my.kandbaz.com/mes-factures"
# Code 2FA : 6 caractères ALPHANUMÉRIQUES (« Votre code : 73X4YS »). Les chiffres seuls ratent.
_CODE_RE = re.compile(r"[Vv]otre code\s*:?\s*([A-Z0-9]{5,8})")


# --------------------------------------------------------------------------- #
#  Lecture du code 2FA dans Gmail (hello@ = SAAS_GMAIL)
# --------------------------------------------------------------------------- #
def _gmail_creds() -> tuple:
    return (os.environ.get("SAAS_GMAIL", ""),
            (os.environ.get("SAAS_GMAIL_APP_PASSWORD", "") or "").replace(" ", ""))


def _count_and_latest_code() -> tuple:
    """(#emails 2FA Kandbaz du jour, dernier code extrait ou None)."""
    user, pwd = _gmail_creds()
    if not user or not pwd:
        return 0, None
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com")
        m.login(user, pwd)
        m.select("INBOX")
        typ, d = m.uid("search", None, "X-GM-RAW", '"from:kandbaz newer_than:1d double"')
        ids = d[0].split() if (d and d[0]) else []
        code = None
        if ids:
            t, md = m.uid("fetch", ids[-1], "(RFC822)")
            msg = email.message_from_bytes(md[0][1])
            html = ""
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    pl = part.get_payload(decode=True)
                    if pl:
                        html += pl.decode(part.get_content_charset() or "utf-8", "replace")
            body = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.S | re.I)
            body = re.sub(r"<[^>]+>", " ", body)
            mm = _CODE_RE.search(body)
            code = mm.group(1) if mm else None
        m.logout()
        return len(ids), code
    except Exception as exc:
        print(f"  ⚠ lecture code 2FA Gmail : {exc}", file=sys.stderr)
        return 0, None


def _is_logged_in(page) -> bool:
    return "/login" not in page.url and "code" not in (page.inner_text("body") or "").lower()[:200]


# --------------------------------------------------------------------------- #
#  Connexion (session, sinon email/mot de passe + 2FA auto)
# --------------------------------------------------------------------------- #
def _login(page, ctx, email_addr: str, password: str, sess: Path, init_session: bool) -> bool:
    page.goto(FACTURES_URL, timeout=60_000, wait_until="domcontentloaded")
    page.wait_for_timeout(3500)
    if _is_logged_in(page):
        return True  # session encore valide

    n0, _ = _count_and_latest_code()  # base pour détecter un code FRAIS
    page.goto(LOGIN_URL, timeout=60_000, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    try:
        page.click('input[name="email"]'); page.type('input[name="email"]', email_addr, delay=20)
        page.click('input[name="password"]'); page.type('input[name="password"]', password, delay=20)
        btn = page.query_selector('ion-button:has-text("Connexion")')
        (btn.click() if btn else page.keyboard.press("Enter"))
        page.wait_for_timeout(4000)
    except Exception as exc:
        print(f"  ⚠ saisie login Kandbaz : {exc}", file=sys.stderr)
        return False

    # écran 2FA -> lire le code frais dans Gmail
    if "code" in (page.inner_text("body") or "").lower():
        code = None
        for _ in range(16):
            n1, c = _count_and_latest_code()
            if n1 > n0 and c:
                code = c
                break
            time.sleep(4)
        if not code:
            print("  ✗ code 2FA Kandbaz introuvable dans Gmail (hello@). --init-session.", file=sys.stderr)
            return False
        boxes = [i for i in page.query_selector_all("input") if i.get_attribute("maxlength") == "1"]
        if len(boxes) >= len(code):
            for ch, b in zip(code, boxes):
                b.click(); b.type(ch, delay=60)
        else:
            page.keyboard.type(code, delay=60)
        page.wait_for_timeout(1500)
        for sel in ('ion-button:has-text("Confirmer")', 'ion-button:has-text("Valider")',
                    'ion-button:has-text("Connexion")'):
            e = page.query_selector(sel)
            if e and e.is_visible():
                e.click(); break
        else:
            page.keyboard.press("Enter")
        page.wait_for_timeout(8000)

    if _is_logged_in(page):
        save_session(ctx, sess)
        return True
    return False


def _dismiss_conformite(page) -> None:
    """Best-effort : ferme le pop-up de conformité LCB-FT (bloque les clics)."""
    for _ in range(3):
        page.keyboard.press("Escape"); page.wait_for_timeout(300)
    for sel in ('ion-button:has-text("Plus tard")', 'ion-button:has-text("Fermer")',
                'ion-icon[name="close"]', '[aria-label="close"]'):
        try:
            e = page.query_selector(sel)
            if e and e.is_visible():
                e.click(); page.wait_for_timeout(500)
        except Exception:
            pass


def _collect_invoices(page, since: str, out_dir: Path, dry_run: bool) -> list:
    page.goto(FACTURES_URL, timeout=60_000, wait_until="domcontentloaded")
    page.wait_for_timeout(5000)
    _dismiss_conformite(page)
    for _ in range(4):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(700)

    # Lignes « FZ-xxxxxx | date | montant€ | Payé ».
    raw = page.eval_on_selector_all(
        "*",
        """els => [...new Set(els.map(e => (e.innerText||'').trim())
            .filter(t => /^FZ-\\d/.test(t) && t.includes('€') && t.length < 60))]"""
    )
    rows = []
    for r in raw:
        fz = re.match(r"(FZ-\d+)", r)
        md = re.search(r"(\d{2})/(\d{2})/(\d{4})", r)
        ma = re.search(r"(\d+[.,]\d{2})\s*€", r)
        if not (fz and md and ma):
            continue
        date_iso = f"{md.group(3)}-{md.group(2)}-{md.group(1)}"
        amount = float(ma.group(1).replace(",", "."))
        if since and date_iso < since:
            continue
        entry = {"vendor": VENDOR, "invoice_id": fz.group(1), "amount": amount,
                 "currency": "EUR", "date": date_iso,
                 "pdf_path": str(out_dir / f"{fz.group(1)}.pdf")}
        if dry_run:
            print(f"  [dry-run] {fz.group(1)}  {date_iso}  {amount:.2f}€", file=sys.stderr)
            rows.append(entry); continue
        # Téléchargement best-effort (bloqué tant que le pop-up conformité est présent).
        dest = out_dir / f"{fz.group(1)}.pdf"
        try:
            with page.expect_download(timeout=10_000) as di:
                page.get_by_text(fz.group(1), exact=False).first.click()
            di.value.save_as(str(dest))
            if dest.exists() and dest.stat().st_size > 500:
                print(f"  ✓ {dest.name}", file=sys.stderr)
                rows.append(entry)
        except Exception:
            print(f"  ⚠ {fz.group(1)} : téléchargement bloqué (pop-up conformité ?) — "
                  f"facture aussi dispo par email.", file=sys.stderr)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Factures Kandbaz (portail, 2FA email auto).")
    std_args(parser)
    args = parser.parse_args()

    load_dotenv()
    email_addr, password, _ = credentials("KANDBAZ")
    sess = Path(args.session) if args.session else session_path(VENDOR)
    out_dir = Path(args.out_dir) if args.out_dir else _ROOT / "reports/portals/kandbaz/input"
    out_dir.mkdir(parents=True, exist_ok=True)

    headed = args.headed or args.init_session
    pw, browser, ctx, page = launch_browser(sess if not args.init_session else None, headed)
    try:
        if not _login(page, ctx, email_addr, password, sess, args.init_session):
            print("  ✗ Connexion Kandbaz échouée.", file=sys.stderr)
            return 0
        rows = _collect_invoices(page, args.since, out_dir, args.dry_run)
    finally:
        ctx.close(); browser.close(); pw.stop()

    if not args.dry_run and rows:
        write_manifest(rows, out_dir.parent / "manifest.json")
        print(f"\n{len(rows)} facture(s) → {out_dir.parent / 'manifest.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
