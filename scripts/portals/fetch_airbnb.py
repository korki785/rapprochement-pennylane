#!/usr/bin/env python3
"""Télécharge les reçus Airbnb depuis le portail (airbnb.fr/trips).

Usage :
    python3 scripts/portals/fetch_airbnb.py [--since YYYY-MM-DD] [--headed] [--init-session]
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
    render_page_to_pdf, write_manifest, std_args, wait_for_manual_login, TIMEOUT_MS,
)

VENDOR = "airbnb"
LOGIN_URL = "https://www.airbnb.fr/login"
TRIPS_URL = "https://www.airbnb.fr/trips/v1"


def _is_logged_in(page) -> bool:
    return "/login" not in page.url and "airbnb." in page.url


def _login(page, email: str, password: str, init_session: bool) -> None:
    page.goto(TRIPS_URL, timeout=60_000)
    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3000)
    dismiss_popups(page)

    if _is_logged_in(page) and "trips" in page.url:
        return

    if init_session or not email:
        wait_for_manual_login(page, "Airbnb", lambda: _is_logged_in(page))
        page.goto(TRIPS_URL, timeout=60_000)
        return

    try:
        page.goto(LOGIN_URL, timeout=60_000)
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        page.fill('input[type="email"], input[name="email"], #email', email)
        page.wait_for_timeout(500)
        page.click('button:has-text("Continue"), button[type="submit"]')
        page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT_MS)
        page.wait_for_timeout(1000)
        pw_field = page.query_selector('input[type="password"]')
        if pw_field and pw_field.is_visible():
            pw_field.fill(password)
            page.click('button[type="submit"]')
            page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT_MS)
        page.goto(TRIPS_URL, timeout=60_000)
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
    except Exception as exc:
        print(f"  ⚠ Connexion Airbnb échouée ({exc}). Relancer avec --init-session.", file=sys.stderr)


def _collect_trips(page, ctx, since: str, out_dir: Path, dry_run: bool) -> list:
    page.wait_for_timeout(4000)
    dismiss_popups(page)

    for _ in range(10):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)

    # Filtrer sur les séjours passés.
    for sel in ['button:has-text("Past trips")', 'a:has-text("Past trips")',
                'button:has-text("Passés")', 'a:has-text("Passés")']:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(2000)
                break
        except Exception:
            pass

    trip_cards = page.query_selector_all(
        'a[href*="/trips/"], a[href*="/booking/"], '
        '[data-testid*="trip-card"], [data-testid*="reservation"]'
    )
    print(f"  {len(trip_cards)} séjour(s) trouvé(s).", file=sys.stderr)

    rows = []
    seen = set()
    for i, card in enumerate(trip_cards):
        try:
            href = card.get_attribute("href") or ""
            card_text = card.evaluate("el => el.innerText || ''") or ""
        except Exception:
            href = ""
            card_text = ""

        amt_m = re.search(r'(\d+\s?\d+[.,]\d{2})\s*€', card_text) \
             or re.search(r'€\s*(\d+\s?\d+[.,]\d{2})', card_text)
        if amt_m:
            amount = float(amt_m.group(1).replace(" ", "").replace(",", "."))
        else:
            amount = 0.0
        date_iso = _extract_date(card_text)

        if date_iso and since and date_iso < since:
            continue

        key = f"{date_iso}_{amount:.2f}"
        if key in seen:
            continue
        seen.add(key)

        if dry_run:
            print(f"  [dry-run] {date_iso}  {amount:.2f} €  {href[:60]}", file=sys.stderr)
            continue

        filename = f"airbnb_{date_iso or f'item{i:03d}'}_{amount:.2f}.pdf"
        out_path = out_dir / filename
        if out_path.exists():
            rows.append({"vendor": VENDOR, "invoice_id": filename.removesuffix(".pdf"),
                         "amount": amount, "date": date_iso, "pdf_path": str(out_path)})
            continue

        try:
            if href:
                url = href if href.startswith("http") else "https://www.airbnb.fr" + href
                page.goto(url, timeout=TIMEOUT_MS)
                page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT_MS)
                page.wait_for_timeout(2500)
                dismiss_popups(page)

            ok = download_or_screenshot(
                ctx, page,
                lambda: page.click(
                    'a:has-text("Receipt"), button:has-text("Receipt"), '
                    'a:has-text("Reçu"), button:has-text("Reçu"), '
                    'a:has-text("Facture"), [data-testid*="receipt"]'
                ),
                out_path
            )
            if not ok:
                ok = render_page_to_pdf(page, out_path)

            if ok:
                print(f"  ✓ {filename}", file=sys.stderr)
                rows.append({"vendor": VENDOR, "invoice_id": filename.removesuffix(".pdf"),
                             "amount": amount, "date": date_iso, "pdf_path": str(out_path)})
            else:
                print(f"  ✗ {date_iso} {amount:.2f}", file=sys.stderr)

            page.go_back(timeout=TIMEOUT_MS)
            page.wait_for_timeout(1500)
        except Exception as exc:
            print(f"  ✗ {date_iso} {amount:.2f} : {exc}", file=sys.stderr)

    return rows


def _extract_date(text: str) -> str:
    fr_months = {"jan": 1, "fév": 2, "mar": 3, "avr": 4, "mai": 5, "juin": 6,
                 "juil": 7, "août": 8, "sep": 9, "oct": 10, "nov": 11, "déc": 12}
    en_months = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
                 "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
    for d, mo_map in [(r'\b(\d{1,2})\s+([A-Za-zÀ-ÿ]{3,})\s+(\d{4})\b', {**fr_months, **{k.lower()[:3]: v for k, v in en_months.items()}})]:
        m = re.search(d, text)
        if m:
            mo = mo_map.get(m.group(2).lower()[:3])
            if mo:
                return f"{m.group(3)}-{mo:02d}-{int(m.group(1)):02d}"
    m2 = re.search(r'\b(\d{4})-(\d{2})-(\d{2})\b', text)
    return m2.group(0) if m2 else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Télécharge les reçus Airbnb.")
    std_args(parser)
    args = parser.parse_args()

    email, password, _ = credentials("AIRBNB")
    sess = Path(args.session) if args.session else session_path(VENDOR)
    out_dir = Path(args.out_dir) if args.out_dir else _ROOT / "reports/portals/airbnb/input"
    out_dir.mkdir(parents=True, exist_ok=True)

    headed = args.headed or args.init_session
    pw, browser, ctx, page = launch_browser(sess if not args.init_session else None, headed)
    try:
        _login(page, email, password, args.init_session)
        save_session(ctx, sess)
        rows = _collect_trips(page, ctx, args.since, out_dir, args.dry_run)
    finally:
        ctx.close()
        browser.close()
        pw.stop()

    if not args.dry_run and rows:
        write_manifest(rows, out_dir.parent / "manifest.json")
        print(f"\n{len(rows)} reçu(s) → {out_dir.parent / 'manifest.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
