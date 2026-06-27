#!/usr/bin/env python3
"""Télécharge les factures Bouygues Telecom depuis l'espace client.

Auth Bouygues = numéro de téléphone + mot de passe → env BOUYGUES_LOGIN (numéro) + BOUYGUES_PASSWORD.

Usage :
    python3 scripts/portals/fetch_bouygues.py [--since YYYY-MM-DD] [--headed] [--init-session]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_SCRIPTS))

from recon.portals import session_path  # noqa: E402
from recon.config import load_dotenv  # noqa: E402
from _portal_base import (  # noqa: E402
    launch_browser, save_session, dismiss_popups, download_or_screenshot,
    write_manifest, std_args, wait_for_manual_login, TIMEOUT_MS,
)

VENDOR = "bouygues"
LOGIN_URL = "https://www.bouyguestelecom.fr/mon-compte/identification"
BILLS_URL = "https://www.bouyguestelecom.fr/mon-compte/mes-factures"


def _load_creds() -> tuple:
    load_dotenv()
    # Accepte BOUYGUES_LOGIN (num de tel) OU BOUYGUES_EMAIL (identifiant email).
    login = (os.environ.get("BOUYGUES_LOGIN", "").strip()
             or os.environ.get("BOUYGUES_EMAIL", "").strip())
    password = os.environ.get("BOUYGUES_PASSWORD", "").strip()
    return login, password


def _is_logged_in(page) -> bool:
    return "/identification" not in page.url and "/connexion" not in page.url


def _login(page, login: str, password: str, init_session: bool) -> None:
    page.goto(LOGIN_URL, timeout=60_000)
    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2000)
    dismiss_popups(page)

    if _is_logged_in(page):
        return

    if init_session or not login:
        wait_for_manual_login(page, "Bouygues Telecom (numéro + mot de passe)", lambda: _is_logged_in(page))
        return

    try:
        page.fill('input[name="login"], input[id*="login"], input[type="tel"]', login)
        page.wait_for_timeout(400)
        page.fill('input[type="password"]', password)
        page.wait_for_timeout(300)
        page.click('button[type="submit"], input[type="submit"], button:has-text("Me connecter")')
        page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT_MS)
        page.wait_for_timeout(2000)
    except Exception as exc:
        print(f"  ⚠ Connexion Bouygues échouée ({exc}). Relancer avec --init-session.", file=sys.stderr)


def _collect_bills(page, ctx, since: str, out_dir: Path, dry_run: bool) -> list:
    page.goto(BILLS_URL, timeout=60_000)
    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3000)
    dismiss_popups(page)

    for _ in range(5):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)

    download_triggers = page.query_selector_all(
        'a:has-text("Télécharger"), a:has-text("Download"), a:has-text("PDF"), '
        'button:has-text("Télécharger"), a[href*=".pdf"], a[href*="facture"]'
    )
    print(f"  {len(download_triggers)} facture(s) trouvée(s).", file=sys.stderr)

    rows = []
    for i, trigger in enumerate(download_triggers):
        try:
            card_text = trigger.evaluate("""el => {
                let p = el;
                for (let i = 0; i < 8; i++) {
                    if (!p.parentElement) break;
                    p = p.parentElement;
                    const t = p.innerText || '';
                    if ((t.includes('€') || t.includes('EUR')) && t.length < 600) return t;
                }
                return '';
            }""") or ""
        except Exception:
            card_text = ""

        amt_m = re.search(r'(\d+[.,]\d{2})\s*€', card_text) \
             or re.search(r'€\s*(\d+[.,]\d{2})', card_text)
        amount = float(amt_m.group(1).replace(",", ".")) if amt_m else 0.0
        date_iso = _extract_date_fr(card_text)

        if date_iso and since and date_iso < since:
            continue
        if dry_run:
            print(f"  [dry-run] {date_iso}  {amount:.2f} €", file=sys.stderr)
            continue

        filename = f"bouygues_{date_iso or f'item{i:03d}'}_{amount:.2f}.pdf"
        out_path = out_dir / filename
        if out_path.exists():
            rows.append({"vendor": VENDOR, "invoice_id": filename.removesuffix(".pdf"),
                         "amount": amount, "date": date_iso, "pdf_path": str(out_path)})
            continue

        ok = download_or_screenshot(ctx, page, trigger.click, out_path)
        if ok:
            print(f"  ✓ {filename}", file=sys.stderr)
            rows.append({"vendor": VENDOR, "invoice_id": filename.removesuffix(".pdf"),
                         "amount": amount, "date": date_iso, "pdf_path": str(out_path)})
        else:
            print(f"  ✗ {date_iso} {amount:.2f} €", file=sys.stderr)

    return rows


def _extract_date_fr(text: str) -> str:
    fr_months = {
        "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
        "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
        "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
    }
    m = re.search(r'\b(\d{1,2})\s+([A-Za-zéèêà]+)\s+(\d{4})\b', text)
    if m:
        mo = fr_months.get(m.group(2).lower())
        if mo:
            return f"{m.group(3)}-{mo:02d}-{int(m.group(1)):02d}"
    m2 = re.search(r'\b(\d{2})[./](\d{2})[./](\d{4})\b', text)
    if m2:
        return f"{m2.group(3)}-{m2.group(2)}-{m2.group(1)}"
    m3 = re.search(r'\b(\d{4})-(\d{2})-(\d{2})\b', text)
    return m3.group(0) if m3 else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Télécharge les factures Bouygues Telecom.")
    std_args(parser)
    args = parser.parse_args()

    login, password = _load_creds()
    sess = Path(args.session) if args.session else session_path(VENDOR)
    out_dir = Path(args.out_dir) if args.out_dir else _ROOT / "reports/portals/bouygues/input"
    out_dir.mkdir(parents=True, exist_ok=True)

    headed = args.headed or args.init_session
    pw, browser, ctx, page = launch_browser(sess if not args.init_session else None, headed)
    page.set_default_timeout(30_000)  # portail FR parfois lent
    try:
        _login(page, login, password, args.init_session)
        save_session(ctx, sess)
        rows = _collect_bills(page, ctx, args.since, out_dir, args.dry_run)
    finally:
        ctx.close()
        browser.close()
        pw.stop()

    if not args.dry_run and rows:
        write_manifest(rows, out_dir.parent / "manifest.json")
        print(f"\n{len(rows)} facture(s) → {out_dir.parent / 'manifest.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
