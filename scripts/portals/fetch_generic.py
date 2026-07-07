#!/usr/bin/env python3
"""Fetcher PORTAIL GÉNÉRIQUE — pour les abonnements ajoutés via le dashboard.

Pas de sélecteurs sur-mesure : on charge la session, on ouvre `billing_url` (lu dans
portal_vendors.csv), on ramasse les liens « Télécharger / PDF / facture », on extrait
montant + date du contexte, on télécharge. Marche pour les portails simples ; si 0 facture
sort, le vendeur est « à custom » (fetcher dédié à écrire à la main).

    python3 scripts/portals/fetch_generic.py --vendor <handler_key> [--init-session] [--dry-run]

Login auto (best effort) : remplit email+password si {PREFIX}_EMAIL/_PASSWORD présents ;
sinon (ou --init-session) → connexion manuelle headed (2FA/OTP), session sauvegardée.
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

from recon.portals import session_path, load_portal_vendors, credentials  # noqa: E402
from _portal_base import (  # noqa: E402
    launch_browser, save_session, dismiss_popups, download_or_screenshot,
    write_manifest, wait_for_manual_login, fill_login_form, std_args,
)

_LOGIN_PAT = re.compile(r"/(login|sign[_-]?in|identification|connexion|auth|users/sign)", re.I)
FR_MONTHS = {"janvier":1,"février":2,"fevrier":2,"mars":3,"avril":4,"mai":5,"juin":6,
             "juillet":7,"août":8,"aout":8,"septembre":9,"octobre":10,"novembre":11,
             "décembre":12,"decembre":12}


def _vendor(handler_key: str) -> dict:
    for v in load_portal_vendors():
        if v["handler_key"] == handler_key:
            return v
    raise SystemExit(f"handler_key '{handler_key}' absent de portal_vendors.csv")


def _logged_in(page) -> bool:
    from urllib.parse import urlparse
    try:
        return not bool(_LOGIN_PAT.search(urlparse(page.url).path or ""))
    except Exception:
        return False


def _extract_date(text: str) -> str:
    m = re.search(r'\b(\d{1,2})\s+([A-Za-zéèêàûî]+)\s+(\d{4})\b', text)
    if m and FR_MONTHS.get(m.group(2).lower()):
        return f"{m.group(3)}-{FR_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    m = re.search(r'\b(\d{2})[./](\d{2})[./](\d{4})\b', text)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.search(r'\b(\d{4})-(\d{2})-(\d{2})\b', text)
    return m.group(0) if m else ""


def _login(page, billing: str, email: str, password: str, init_session: bool) -> None:
    page.goto(billing, timeout=60_000)
    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2000)
    dismiss_popups(page)
    if _logged_in(page):
        return
    if init_session or not email:
        wait_for_manual_login(page, "portail (générique)", lambda: _logged_in(page))
        return
    # Tentative auto : sélecteurs email/password courants.
    fill_login_form(
        page, email, password,
        email_sel='input[type="email"], input[name*="email" i], input[name*="login" i], input[type="tel"]',
        pass_sel='input[type="password"]',
        submit_sel='button[type="submit"], input[type="submit"], button:has-text("connect"), button:has-text("Log in")',
    )
    page.wait_for_timeout(2000)
    if not _logged_in(page):
        print("  ⚠ login auto échoué — relancer avec --init-session.", file=sys.stderr)


def _collect(page, ctx, out_dir: Path, dry_run: bool) -> list:
    for _ in range(5):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(700)
    triggers = page.query_selector_all(
        'a:has-text("Télécharger"), a:has-text("Download"), a:has-text("PDF"), '
        'a:has-text("Facture"), a:has-text("Invoice"), a:has-text("Receipt"), '
        'button:has-text("Télécharger"), a[href*=".pdf"], a[href*="invoice"], a[href*="facture"]'
    )
    print(f"  {len(triggers)} lien(s) facture trouvé(s).", file=sys.stderr)
    rows = []
    for i, tr in enumerate(triggers):
        try:
            ctx_text = tr.evaluate("""el => { let p=el;
                for(let i=0;i<8;i++){ if(!p.parentElement)break; p=p.parentElement;
                  const t=p.innerText||''; if((t.includes('€')||t.includes('EUR'))&&t.length<600)return t; }
                return ''; }""") or ""
        except Exception:
            ctx_text = ""
        am = re.search(r'(\d+[.,]\d{2})\s*€', ctx_text) or re.search(r'€\s*(\d+[.,]\d{2})', ctx_text)
        amount = float(am.group(1).replace(",", ".")) if am else 0.0
        date_iso = _extract_date(ctx_text)
        if dry_run:
            print(f"  [dry-run] {date_iso or f'item{i:03d}'}  {amount:.2f} €", file=sys.stderr)
            continue
        fname = f"generic_{date_iso or f'item{i:03d}'}_{amount:.2f}.pdf"
        out = out_dir / fname
        if download_or_screenshot(ctx, page, tr.click, out):
            rows.append({"vendor": out_dir.parent.name, "invoice_id": fname.removesuffix(".pdf"),
                         "amount": amount, "date": date_iso, "pdf_path": str(out)})
            print(f"  ✓ {fname}", file=sys.stderr)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetcher portail générique.")
    ap.add_argument("--vendor", required=True, help="handler_key dans portal_vendors.csv")
    std_args(ap)
    args = ap.parse_args()

    v = _vendor(args.vendor)
    email, password, _ = credentials(v["env_prefix"])
    sess = Path(args.session) if args.session else session_path(args.vendor)
    out_dir = Path(args.out_dir) if args.out_dir else _ROOT / "reports/portals" / args.vendor / "input"
    out_dir.mkdir(parents=True, exist_ok=True)

    headed = args.headed or args.init_session
    pw, browser, ctx, page = launch_browser(sess if not args.init_session else None, headed)
    try:
        _login(page, v["billing_url"], email, password, args.init_session)
        save_session(ctx, sess)
        rows = _collect(page, ctx, out_dir, args.dry_run)
    finally:
        ctx.close(); browser.close(); pw.stop()

    if not args.dry_run and rows:
        write_manifest(rows, out_dir.parent / "manifest.json")
        print(f"\n{len(rows)} facture(s) → {out_dir.parent/'manifest.json'}", file=sys.stderr)
    elif not args.dry_run:
        print("  0 facture — vendeur « à custom » (fetcher dédié requis).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
