#!/usr/bin/env python3
"""Télécharge les reçus de courses Uber depuis riders.uber.com.

Note : différent du flux UberEats (livraisons). Ici = courses en voiture.

Usage :
    python3 scripts/portals/fetch_uber_rides.py [--since YYYY-MM-DD] [--headed] [--init-session]
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

VENDOR = "uber_rides"
LOGIN_URL = "https://auth.uber.com/v2/"
TRIPS_URL = "https://riders.uber.com/trips"


def _is_logged_in(page) -> bool:
    return "auth.uber.com" not in page.url and "/login" not in page.url


def _login(page, ctx, email: str, password: str, init_session: bool) -> None:
    page.goto(TRIPS_URL, timeout=60_000)
    page.wait_for_load_state("load", timeout=60_000)
    page.wait_for_timeout(3000)

    if _is_logged_in(page):
        return

    if init_session or not email:
        wait_for_manual_login(page, "Uber", lambda: _is_logged_in(page))
        page.goto(TRIPS_URL, timeout=60_000)
        page.wait_for_load_state("load", timeout=60_000)
        return

    try:
        page.goto(LOGIN_URL, timeout=60_000)
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        page.fill('input[type="email"], input[name="email"]', email)
        page.wait_for_timeout(400)
        page.click('button:has-text("Continue"), button[type="submit"]')
        page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT_MS)
        page.wait_for_timeout(1000)
        pw_field = page.query_selector('input[type="password"]')
        if pw_field and pw_field.is_visible():
            pw_field.fill(password)
            page.wait_for_timeout(300)
            page.click('button[type="submit"]')
            page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT_MS)
        page.goto(TRIPS_URL, timeout=60_000)
        page.wait_for_load_state("load", timeout=60_000)
        page.wait_for_timeout(3000)
    except Exception as exc:
        print(f"  ⚠ Connexion Uber échouée ({exc}). Relancer avec --init-session.", file=sys.stderr)


def _collect_trips(page, ctx, since: str, out_dir: Path, dry_run: bool) -> list:
    dismiss_popups(page)

    for _ in range(15):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1200)

    trip_links = page.query_selector_all(
        'a[href*="/trips/"], a:has-text("View receipt"), '
        'button:has-text("View receipt"), [data-testid*="trip"]'
    )
    print(f"  {len(trip_links)} course(s) trouvée(s).", file=sys.stderr)

    rows = []
    seen = set()
    for i, link in enumerate(trip_links):
        try:
            card_text = link.evaluate("""el => {
                let p = el;
                for (let i = 0; i < 10; i++) {
                    if (!p.parentElement) break;
                    p = p.parentElement;
                    const t = p.innerText || '';
                    if ((t.includes('€') || t.includes('$')) && t.length < 600) return t;
                }
                return '';
            }""") or ""
            href = link.get_attribute("href") or ""
        except Exception:
            card_text = ""
            href = ""

        amt_m = re.search(r'[\$€]\s*(\d+[.,]\d{2})', card_text) \
             or re.search(r'(\d+[.,]\d{2})\s*[\$€]', card_text)
        amount = float(amt_m.group(1).replace(",", ".")) if amt_m else 0.0
        date_iso = _extract_date(card_text)

        if date_iso and since and date_iso < since:
            continue

        key = f"{date_iso}_{amount:.2f}"
        if key in seen:
            continue
        seen.add(key)

        if dry_run:
            print(f"  [dry-run] {date_iso}  {amount:.2f}", file=sys.stderr)
            continue

        filename = f"uber_{date_iso or f'item{i:03d}'}_{amount:.2f}.pdf"
        out_path = out_dir / filename
        if out_path.exists():
            rows.append({"vendor": VENDOR, "invoice_id": filename.removesuffix(".pdf"),
                         "amount": amount, "date": date_iso, "pdf_path": str(out_path)})
            continue

        try:
            if href:
                url = href if href.startswith("http") else "https://riders.uber.com" + href
                page.goto(url, timeout=TIMEOUT_MS)
            else:
                link.click()
            page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT_MS)
            page.wait_for_timeout(2000)
            dismiss_popups(page)

            ok = download_or_screenshot(ctx, page,
                                        lambda: page.click('a:has-text("Download"), button:has-text("Download")'),
                                        out_path)
            if not ok:
                ok = render_page_to_pdf(page, out_path)

            if ok:
                print(f"  ✓ {filename}", file=sys.stderr)
                rows.append({"vendor": VENDOR, "invoice_id": filename.removesuffix(".pdf"),
                             "amount": amount, "date": date_iso, "pdf_path": str(out_path)})
            else:
                print(f"  ✗ {date_iso} {amount:.2f}", file=sys.stderr)

            page.go_back(timeout=TIMEOUT_MS)
            page.wait_for_timeout(1200)
        except Exception as exc:
            print(f"  ✗ {date_iso} {amount:.2f} : {exc}", file=sys.stderr)

    return rows


def _extract_date(text: str) -> str:
    months = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
               "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
    m = re.search(
        r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s+(\d{4})\b',
        text
    )
    if m:
        mo = months.get(m.group(1), 0)
        if mo:
            return f"{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}"
    m2 = re.search(r'\b(\d{4})-(\d{2})-(\d{2})\b', text)
    return m2.group(0) if m2 else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Télécharge les reçus Uber (courses).")
    std_args(parser)
    args = parser.parse_args()

    email, password, _ = credentials("UBER")
    sess = Path(args.session) if args.session else session_path(VENDOR)
    out_dir = Path(args.out_dir) if args.out_dir else _ROOT / "reports/portals/uber_rides/input"
    out_dir.mkdir(parents=True, exist_ok=True)

    headed = args.headed or args.init_session
    pw, browser, ctx, page = launch_browser(sess if not args.init_session else None, headed)
    try:
        _login(page, ctx, email, password, args.init_session)
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
