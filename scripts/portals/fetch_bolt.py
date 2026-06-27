#!/usr/bin/env python3
"""Télécharge les reçus de courses Bolt depuis le portail web.

Auth Bolt = numéro de téléphone + OTP SMS → --init-session obligatoire pour la première fois.

Usage :
    python3 scripts/portals/fetch_bolt.py --init-session  # première connexion
    python3 scripts/portals/fetch_bolt.py [--since YYYY-MM-DD] [--headed]
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
    launch_browser, save_session, dismiss_popups, render_page_to_pdf,
    write_manifest, std_args, wait_for_manual_login, TIMEOUT_MS, load_credentials,
)

VENDOR = "bolt"
TRIPS_URL = "https://bolt.eu/en/profile/trips/"


def _is_logged_in(page) -> bool:
    return "/login" not in page.url and "/auth" not in page.url and "bolt.eu" in page.url


def _collect_trips(page, ctx, since: str, out_dir: Path, dry_run: bool) -> list:
    page.goto(TRIPS_URL, timeout=60_000)
    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    page.wait_for_timeout(4000)
    dismiss_popups(page)

    if not _is_logged_in(page) or "trips" not in page.url:
        print("  ✗ Non connecté à Bolt — relancer avec --init-session.", file=sys.stderr)
        return []

    # Charger plus de courses.
    prev_h = 0
    for _ in range(15):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1200)
        h = page.evaluate("document.body.scrollHeight")
        if h == prev_h:
            break
        prev_h = h

    # Récupérer les liens vers les reçus individuels.
    receipt_links = page.query_selector_all(
        'a[href*="/receipt"], a[href*="/trip"], '
        'a:has-text("Get receipt"), a:has-text("View receipt"), '
        'button:has-text("Get receipt")'
    )
    # Aussi chercher les cartes de course avec date et montant.
    trip_cards = page.query_selector_all(
        '[class*="trip"], [class*="ride"], [class*="order"], '
        '[data-testid*="trip"], [data-testid*="ride"]'
    )
    print(f"  {len(receipt_links)} lien(s) reçu + {len(trip_cards)} carte(s) course.", file=sys.stderr)

    rows = []
    seen_keys = set()

    # Parcourir les liens directs vers les reçus.
    for i, link in enumerate(receipt_links):
        try:
            href = link.get_attribute("href") or ""
            card_text = link.evaluate("""el => {
                let p = el;
                for (let i = 0; i < 8; i++) {
                    if (!p.parentElement) break;
                    p = p.parentElement;
                    const t = p.innerText || '';
                    if ((t.includes('€') || t.includes('$')) && t.length < 600) return t;
                }
                return '';
            }""") or ""
        except Exception:
            href = ""
            card_text = ""

        amt_m = re.search(r'[\$€]\s*(\d+[.,]\d{2})', card_text) \
             or re.search(r'(\d+[.,]\d{2})\s*[\$€]', card_text)
        amount = float(amt_m.group(1).replace(",", ".")) if amt_m else 0.0
        date_iso = _extract_date(card_text)

        if date_iso and since and date_iso < since:
            continue

        key = f"{date_iso}_{amount:.2f}"
        if key in seen_keys:
            continue
        seen_keys.add(key)

        if dry_run:
            print(f"  [dry-run] {date_iso}  {amount:.2f}  {href[:60]}", file=sys.stderr)
            continue

        filename = f"bolt_{date_iso or f'item{i:03d}'}_{amount:.2f}.pdf"
        out_path = out_dir / filename
        if out_path.exists():
            rows.append({"vendor": VENDOR, "invoice_id": filename.removesuffix(".pdf"),
                         "amount": amount, "date": date_iso, "pdf_path": str(out_path)})
            continue

        # Naviguer vers la page du reçu et en faire un PDF.
        try:
            if href:
                url = href if href.startswith("http") else "https://bolt.eu" + href
                page.goto(url, timeout=TIMEOUT_MS)
                page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT_MS)
                page.wait_for_timeout(2000)
                dismiss_popups(page)
            else:
                link.click()
                page.wait_for_timeout(2000)

            ok = render_page_to_pdf(page, out_path)
            if ok:
                print(f"  ✓ {filename}", file=sys.stderr)
                rows.append({"vendor": VENDOR, "invoice_id": filename.removesuffix(".pdf"),
                             "amount": amount, "date": date_iso, "pdf_path": str(out_path)})
            else:
                print(f"  ✗ {date_iso} {amount:.2f}", file=sys.stderr)
            page.go_back(timeout=TIMEOUT_MS)
            page.wait_for_timeout(1000)
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
    if m2:
        return m2.group(0)
    # Format DD/MM/YYYY ou DD.MM.YYYY.
    m3 = re.search(r'\b(\d{2})[./](\d{2})[./](\d{4})\b', text)
    if m3:
        return f"{m3.group(3)}-{m3.group(2)}-{m3.group(1)}"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Télécharge les reçus Bolt.")
    std_args(parser)
    args = parser.parse_args()

    _, _, phone = load_credentials("BOLT")
    sess = Path(args.session) if args.session else session_path(VENDOR)
    out_dir = Path(args.out_dir) if args.out_dir else _ROOT / "reports/portals/bolt/input"
    out_dir.mkdir(parents=True, exist_ok=True)

    headed = args.headed or args.init_session
    pw, browser, ctx, page = launch_browser(sess if not args.init_session else None, headed)
    try:
        if args.init_session or not sess.exists():
            page.goto(TRIPS_URL, timeout=60_000)
            wait_for_manual_login(page, "Bolt (téléphone + OTP SMS)", lambda: _is_logged_in(page))
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
