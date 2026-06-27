#!/usr/bin/env python3
"""Télécharge les factures Wix depuis manage.wix.com.

Usage :
    python3 scripts/portals/fetch_wix.py [--since YYYY-MM-DD] [--headed] [--init-session]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_SCRIPTS))

from recon.portals import session_path, credentials  # noqa: E402
from _portal_base import (  # noqa: E402
    launch_browser, save_session, dismiss_popups, download_or_screenshot,
    write_manifest, std_args, wait_for_manual_login, TIMEOUT_MS,
)

VENDOR = "wix"
LOGIN_URL = "https://users.wix.com/signin"
# Historique des paiements Premium = les vrais débits (Forfait Premium, App, domaine).
# (PAS « Wix Invoices », qui sert à facturer SES propres clients.)
BILLING_URL = "https://manage.wix.com/account/billing-history"


def _is_logged_in(page) -> bool:
    return ("users.wix.com/signin" not in page.url
            and "wix.com/login" not in page.url
            and "users.wix.com/login" not in page.url)


def _login(page, email: str, password: str, init_session: bool) -> None:
    page.goto(LOGIN_URL, timeout=60_000)
    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    if _is_logged_in(page):
        return

    if init_session or not email:
        wait_for_manual_login(page, "Wix", lambda: _is_logged_in(page))
        return

    try:
        page.fill('input[type="email"], input[name="email"], #email', email)
        page.wait_for_timeout(400)
        page.click('button:has-text("Continue"), button[type="submit"]')
        page.wait_for_timeout(1000)
        pw_field = page.query_selector('input[type="password"], input[name="password"]')
        if pw_field and pw_field.is_visible():
            pw_field.fill(password)
            page.wait_for_timeout(300)
            page.click('button[type="submit"], button:has-text("Log in"), button:has-text("Sign in")')
            page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT_MS)
    except Exception as exc:
        print(f"  ⚠ Connexion Wix échouée ({exc}). Relancer avec --init-session.", file=sys.stderr)


def _billing_frame(page):
    """L'historique de facturation Wix est rendu dans une iframe `/studio/billing-history`."""
    for fr in page.frames:
        if "billing-history" in (fr.url or ""):
            return fr
    return page.main_frame


def _collect_invoices(page, ctx, since: str, out_dir: Path, dry_run: bool) -> list:
    page.goto(BILLING_URL, timeout=60_000)
    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    page.wait_for_timeout(7000)   # l'iframe + le tableau mettent du temps à charger
    dismiss_popups(page)
    page.wait_for_timeout(2000)

    fr = _billing_frame(page)
    # Chaque ligne porte un élément `data-hook="invoice-number"` (le n° de facture,
    # qui figure TEL QUEL dans le libellé Qonto « Wix.com <numéro> »). Cliquer dessus
    # déclenche le téléchargement direct de <numéro>.pdf.
    try:
        fr.wait_for_selector('[data-hook="invoice-number"]', timeout=20_000)
    except Exception:
        print("  ✗ aucune ligne de facturation trouvée (session expirée ?). "
              "Relancer avec --init-session.", file=sys.stderr)
        return []

    numbers = fr.eval_on_selector_all(
        '[data-hook="invoice-number"]',
        """els => els.map(e => {
            let p = e;
            let row = '';
            for (let i = 0; i < 8; i++) {
                if (!p.parentElement) break;
                p = p.parentElement;
                const t = p.innerText || '';
                if (t.includes('€') && t.length < 300) { row = t; break; }
            }
            return { number: (e.innerText || '').trim(), row };
        })"""
    )
    print(f"  {len(numbers)} ligne(s) de facturation trouvée(s).", file=sys.stderr)

    rows = []
    for item in numbers:
        number = item.get("number", "").strip()
        row_text = item.get("row", "")
        if not number:
            continue
        amt_m = re.search(r'(\d+[.,]\d{2})\s*€', row_text)
        amount = float(amt_m.group(1).replace(",", ".")) if amt_m else 0.0
        date_iso = _extract_date(row_text)

        if date_iso and since and date_iso < since:
            continue

        if dry_run:
            print(f"  [dry-run] {date_iso}  {amount:.2f}€  n°{number}", file=sys.stderr)
            continue

        out_path = out_dir / f"wix_{number}.pdf"
        if out_path.exists():
            rows.append({"vendor": VENDOR, "invoice_id": number,
                         "amount": amount, "date": date_iso, "pdf_path": str(out_path)})
            continue

        # Clic sur le n° → téléchargement direct du PDF.
        el = fr.query_selector(f'[data-hook="invoice-number"]:text-is("{number}")') \
            or fr.query_selector(f'text={number}')
        ok = False
        if el is not None:
            try:
                with page.expect_download(timeout=30_000) as di:
                    el.click()
                di.value.save_as(str(out_path))
                ok = out_path.exists() and out_path.stat().st_size > 500
            except Exception as exc:
                print(f"  ⚠ téléchargement n°{number} : {exc}", file=sys.stderr)

        if ok:
            print(f"  ✓ wix_{number}.pdf  {date_iso}  {amount:.2f}€", file=sys.stderr)
            rows.append({"vendor": VENDOR, "invoice_id": number,
                         "amount": amount, "date": date_iso, "pdf_path": str(out_path)})
        else:
            print(f"  ✗ n°{number} ({date_iso} {amount:.2f}€)", file=sys.stderr)

    return rows


# Mois FR (abréviations Wix : « 6 avr. 2026 », « 10 mai 2026 », « 12 juin 2026 »).
_FR_MONTHS = {
    "janv": 1, "févr": 2, "fevr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6,
    "juil": 7, "août": 8, "aout": 8, "sept": 9, "oct": 10, "nov": 11, "déc": 12, "dec": 12,
}


def _extract_date(text: str) -> str:
    m = re.search(r'\b(\d{1,2})\s+([A-Za-zéèêûàçï.]+)\.?\s+(\d{4})\b', text)
    if m:
        key = m.group(2).lower().strip(".")[:4]
        mo = _FR_MONTHS.get(key) or _FR_MONTHS.get(key[:3])
        if mo:
            return f"{int(m.group(3)):04d}-{mo:02d}-{int(m.group(1)):02d}"
    m2 = re.search(r'\b(\d{4})-(\d{2})-(\d{2})\b', text)
    return m2.group(0) if m2 else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Télécharge les factures Wix.")
    std_args(parser)
    args = parser.parse_args()

    email, password, _ = credentials("WIX")
    sess = Path(args.session) if args.session else session_path(VENDOR)
    out_dir = Path(args.out_dir) if args.out_dir else _ROOT / "reports/portals/wix/input"
    out_dir.mkdir(parents=True, exist_ok=True)

    headed = args.headed or args.init_session
    pw, browser, ctx, page = launch_browser(sess if not args.init_session else None, headed)
    try:
        _login(page, email, password, args.init_session)
        save_session(ctx, sess)
        rows = _collect_invoices(page, ctx, args.since, out_dir, args.dry_run)
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
