#!/usr/bin/env python3
"""Télécharge les factures Notion depuis le portail (notion.so).

Usage :
    python3 scripts/portals/fetch_notion.py [--since YYYY-MM-DD] [--headed] [--init-session]
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
    write_manifest, std_args, wait_for_manual_login, TIMEOUT_MS, render_page_to_pdf,
)

VENDOR = "notion"
# Notion a migré vers app.notion.com. Le login = app.notion.com/login ;
# l'app connectée = app.notion.com/<page> ; la home marketing = notion.com.
LOGIN_URL = "https://app.notion.com/login"
BILLING_URL = "https://app.notion.com/my-account"


def _is_logged_in(page) -> bool:
    # Connecté = sur l'app (app.notion.com ou ancien notion.so) HORS page de login.
    url = page.url
    on_app = "app.notion.com" in url or "notion.so" in url
    return on_app and "/login" not in url and "/signup" not in url


def _login(page, email: str, password: str, init_session: bool) -> None:
    # Notion = SSO / magic-link uniquement : pas de login email+password exploitable.
    # On s'appuie sur la session (.notion_session.json). init-session = connexion manuelle.
    page.goto(LOGIN_URL, timeout=60_000)
    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3000)
    try:
        page.bring_to_front()
    except Exception:
        pass
    if _is_logged_in(page):
        return
    if init_session:
        wait_for_manual_login(page, "Notion", lambda: _is_logged_in(page), timeout_s=600)
    else:
        print("  ⚠ Session Notion absente/expirée — relancer avec --init-session.", file=sys.stderr)


def _open_billing(page) -> bool:
    """Ouvre l'app Notion → menu espace de travail → Paramètres → onglet Billing.

    Notion = SPA au DOM obfusqué (pas de data-hook stable) : on navigue par le TEXTE
    des éléments (le nom de l'espace, « Settings », « Billing »), seuls repères fiables.
    """
    page.goto("https://app.notion.com/", timeout=60_000)
    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    page.wait_for_timeout(8000)
    dismiss_popups(page)

    # Le menu « espace de travail » (haut-gauche) ouvre une liste contenant « Settings ».
    # Son libellé varie (nom de l'espace) et le DOM est obfusqué → on essaie les éléments
    # cliquables du coin haut-gauche jusqu'à ce que « Settings » apparaisse.
    _BLACKLIST = {"skip to content", "home", "chat", "meetings", "search", "inbox", "more"}
    cands = page.evaluate(
        """() => {
            const seen = new Set(), out = [];
            for (const e of document.querySelectorAll('div,span,a')) {
                const r = e.getBoundingClientRect();
                const t = (e.innerText || '').trim();
                if (r.y < 60 && r.x < 280 && r.width > 30 && t.length > 2 && t.length < 40
                    && e.children.length <= 3 && !seen.has(t)) {
                    seen.add(t); out.push(t);
                }
            }
            return out.slice(0, 12);
        }"""
    )
    opened = False
    for label in cands:
        if label.lower() in _BLACKLIST:
            continue
        try:
            page.get_by_text(label, exact=True).first.click(timeout=4000)
            page.wait_for_timeout(1500)
            if page.get_by_text("Settings", exact=True).count() > 0:
                opened = True
                break
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            continue

    if not opened:
        print("  ⚠ Menu espace de travail introuvable.", file=sys.stderr)
        return False

    try:
        page.get_by_text("Settings", exact=True).first.click()
        page.wait_for_timeout(4000)
        page.get_by_text("Billing", exact=True).first.click()
        page.wait_for_timeout(4500)
        return True
    except Exception as exc:
        print(f"  ⚠ Settings→Billing échoué ({exc}).", file=sys.stderr)
        return False


def _collect_invoices(page, ctx, since: str, out_dir: Path, dry_run: bool) -> list:
    if not _open_billing(page):
        print("  ✗ Panneau Billing inaccessible — relancer avec --init-session.", file=sys.stderr)
        return []

    # Chaque facture = une ligne « <date> | Paid · €<montant> | View invoice ». Le 1er
    # bouton « View invoice » est la facture À VENIR (ligne sans date) → ignoré.
    row_texts = page.eval_on_selector_all(
        "text=View invoice",
        """els => els.map(e => {
            let p = e, row = '';
            for (let i = 0; i < 6; i++) {
                if (!p.parentElement) break;
                p = p.parentElement;
                const t = (p.innerText || '').replace(/\\n/g, ' | ').trim();
                if (t.includes('€') && t.length < 140) { row = t; break; }
            }
            return row;
        })"""
    )
    n = len(row_texts)
    print(f"  {n} ligne(s) « View invoice » trouvée(s).", file=sys.stderr)

    rows = []
    btns = page.get_by_text("View invoice", exact=True)
    for i, row_text in enumerate(row_texts):
        amt_m = re.search(r'€\s*(\d+[.,]\d{2})', row_text) or re.search(r'(\d+[.,]\d{2})\s*€', row_text)
        amount = float(amt_m.group(1).replace(",", ".")) if amt_m else 0.0
        date_iso = _extract_date(row_text)
        if not date_iso:           # ligne « Upcoming invoice » sans date → on saute
            continue
        if since and date_iso < since:
            continue

        if dry_run:
            print(f"  [dry-run] {date_iso}  {amount:.2f}€", file=sys.stderr)
            continue

        out_path = out_dir / f"notion_{date_iso}.pdf"
        if out_path.exists():
            rows.append({"vendor": VENDOR, "invoice_id": date_iso,
                         "amount": amount, "date": date_iso, "pdf_path": str(out_path)})
            continue

        # « View invoice » ouvre la facture dans un nouvel onglet → rendu en PDF.
        try:
            with page.context.expect_page(timeout=15_000) as pi:
                btns.nth(i).click()
            inv = pi.value
            inv.wait_for_load_state("domcontentloaded", timeout=30_000)
            inv.wait_for_timeout(3500)
            ok = render_page_to_pdf(inv, out_path)
            inv.close()
        except Exception as exc:
            print(f"  ✗ {date_iso} : {exc}", file=sys.stderr)
            ok = False

        if ok:
            print(f"  ✓ notion_{date_iso}.pdf  {amount:.2f}€", file=sys.stderr)
            rows.append({"vendor": VENDOR, "invoice_id": date_iso,
                         "amount": amount, "date": date_iso, "pdf_path": str(out_path)})
        else:
            print(f"  ✗ {date_iso} {amount:.2f}€", file=sys.stderr)

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
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Télécharge les factures Notion.")
    std_args(parser)
    args = parser.parse_args()

    email, password, _ = credentials("NOTION")
    sess = Path(args.session) if args.session else session_path(VENDOR)
    out_dir = Path(args.out_dir) if args.out_dir else _ROOT / "reports/portals/notion/input"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir.parent / "manifest.json"

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
        write_manifest(rows, manifest_path)
        print(f"\n{len(rows)} facture(s) → {manifest_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
