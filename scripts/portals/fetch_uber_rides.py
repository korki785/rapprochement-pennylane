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


_DL_SELECTOR = ('button:has-text("Download Invoice"), a:has-text("Download Invoice"), '
                '[role="button"]:has-text("Download Invoice")')


def _collect_trips(page, ctx, since: str, out_dir: Path, dry_run: bool) -> list:
    page.goto(TRIPS_URL, timeout=60_000, wait_until="domcontentloaded")
    page.wait_for_timeout(6000)
    dismiss_popups(page)
    for _ in range(6):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)

    # Chaque course = un lien « Details » href=/trips/<uuid> ; la carte parente porte
    # « <lieu> | <Mois JJ • heure> | €<montant> | … ». On mappe href → texte de carte.
    cards = page.eval_on_selector_all(
        "a[href*='/trips/']",
        """els => els.map(e => {
            const href = e.getAttribute('href') || '';
            let p = e, row = '';
            for (let i = 0; i < 10; i++) {
                if (!p.parentElement) break; p = p.parentElement;
                const t = (p.innerText || '').replace(/\\n/g,' | ');
                if ((t.includes('€') || t.includes('$')) && t.length < 400) { row = t; break; }
            }
            return { href, row };
        }).filter(x => /\\/trips\\/[0-9a-f]{8}-/.test(x.href) && x.row)"""
    )
    # Dédup par href.
    seen_href, trips = set(), []
    for c in cards:
        if c["href"] in seen_href:
            continue
        seen_href.add(c["href"])
        trips.append(c)
    print(f"  {len(trips)} course(s) trouvée(s).", file=sys.stderr)

    rows = []
    for c in trips:
        row = c["row"]
        amt_m = re.search(r'[\$€]\s*(\d+[.,]\d{2})', row) or re.search(r'(\d+[.,]\d{2})\s*[\$€]', row)
        amount = float(amt_m.group(1).replace(",", ".")) if amt_m else 0.0
        date_iso = _extract_date(row)
        if not date_iso:
            continue
        if since and date_iso < since:
            continue

        if dry_run:
            print(f"  [dry-run] {date_iso}  {amount:.2f}€", file=sys.stderr)
            continue

        out_path = out_dir / f"uber_{date_iso}.pdf"
        if out_path.exists():
            rows.append({"vendor": VENDOR, "invoice_id": date_iso, "amount": amount,
                         "date": date_iso, "pdf_path": str(out_path)})
            continue

        url = c["href"] if c["href"].startswith("http") else "https://riders.uber.com" + c["href"]
        try:
            page.goto(url, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            dismiss_popups(page)
            with page.expect_download(timeout=20_000) as di:
                page.click(_DL_SELECTOR)
            di.value.save_as(str(out_path))
            ok = out_path.exists() and out_path.stat().st_size > 500
        except Exception as exc:
            print(f"  ⚠ {date_iso} : {exc}", file=sys.stderr)
            ok = False

        if ok:
            print(f"  ✓ uber_{date_iso}.pdf  {amount:.2f}€", file=sys.stderr)
            rows.append({"vendor": VENDOR, "invoice_id": date_iso, "amount": amount,
                         "date": date_iso, "pdf_path": str(out_path)})
        else:
            print(f"  ✗ {date_iso} {amount:.2f}€", file=sys.stderr)

    return rows


_EN_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _extract_date(text: str) -> str:
    # Uber liste « May 23 • 11:24 PM » (sans année) → on infère l'année (≤ aujourd'hui).
    m = re.search(r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2})\b', text)
    if m:
        import datetime
        mo = _EN_MONTHS[m.group(1).lower()]
        day = int(m.group(2))
        today = datetime.date.today()
        year = today.year
        try:
            if datetime.date(year, mo, day) > today:
                year -= 1
        except ValueError:
            pass
        return f"{year:04d}-{mo:02d}-{day:02d}"
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
