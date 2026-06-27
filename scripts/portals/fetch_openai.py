#!/usr/bin/env python3
"""Télécharge les factures ChatGPT Plus (OPENAI *CHATGPT SUBSCR).

chatgpt.com sert un CAPTCHA Cloudflare qui bloque tout navigateur AUTOMATISÉ (Playwright/
Chromium = fingerprint webdriver). Solution : on s'ATTACHE via CDP au VRAI Chrome de
l'utilisateur (lancé avec --remote-debugging-port=9222), que Cloudflare laisse passer.

Pré-requis (mis en place une fois, gardé vivant par launchd — voir README) :
    open -na "Google Chrome" --args --remote-debugging-port=9222 "--remote-allow-origins=*" \\
         --user-data-dir="$HOME/.chrome-recon-debug" "https://chatgpt.com/"
    → l'utilisateur se connecte à ChatGPT UNE fois dans cette fenêtre (session persistante).

Chemin scrapé : compte → Paramètres → Facturation → chaque ligne « Afficher » pointe vers une
facture Stripe (invoice.stripe.com) → bouton « Télécharger la facture » → PDF.

Usage :
    python3 scripts/portals/fetch_openai.py [--since YYYY-MM-DD] [--cdp http://localhost:9222]
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

from _portal_base import write_manifest  # noqa: E402

VENDOR = "openai"
CDP_URL = "http://localhost:9222"
HOME_URL = "https://chatgpt.com/"
DEFAULT_SINCE = "2026-04-01"

_FR_MONTHS = {
    "janv": 1, "févr": 2, "fevr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6,
    "juil": 7, "août": 8, "aout": 8, "sept": 9, "oct": 10, "nov": 11, "déc": 12, "dec": 12,
}


def _fr_date(text: str) -> str:
    m = re.search(r'(\d{1,2})\s+([A-Za-zéûàç.]+)\.?\s+(\d{4})', text)
    if m:
        mo = _FR_MONTHS.get(m.group(2).lower().strip(".")[:4]) or _FR_MONTHS.get(m.group(2).lower()[:3])
        if mo:
            return f"{int(m.group(3)):04d}-{mo:02d}-{int(m.group(1)):02d}"
    return ""


def _connect(cdp_url: str):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit('Playwright absent. pip install -e ".[portal]"')
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(cdp_url)
    except Exception as exc:
        pw.stop()
        print(f"  ✗ Chrome debug (CDP {cdp_url}) injoignable : {exc}\n"
              "    Lancer le Chrome dédié (voir reports/portals/_drop/README ou la doc) "
              "et se connecter à ChatGPT.", file=sys.stderr)
        return None, None
    return pw, browser


def _open_billing(page) -> list:
    """Ouvre compte → Paramètres → Facturation. Renvoie [(date_iso, stripe_url)]."""
    page.goto(HOME_URL, timeout=60_000, wait_until="domcontentloaded")
    page.wait_for_timeout(6000)
    low = (page.inner_text("body") or "").lower()
    if "se connecter" in low or "inscription gratuite" in low:
        print("  ✗ Pas connecté à ChatGPT dans le Chrome debug — se connecter une fois.", file=sys.stderr)
        return []
    if "vérifiez que vous êtes humain" in low or "verify you are human" in low:
        print("  ✗ Challenge Cloudflare présent (rare en vrai Chrome) — réessayer.", file=sys.stderr)
        return []

    def _clk(texts, t=3000):
        for s in texts:
            try:
                e = page.query_selector(s)
                if e and e.is_visible():
                    e.click(); page.wait_for_timeout(t); return True
            except Exception:
                pass
        return False

    # Menu compte (bas-gauche) → Paramètres → onglet Facturation.
    _clk(['[data-testid="accounts-profile-button"]', 'text=Nael Darwish'])
    if not _clk(['text=Paramètres', 'text=Settings']):
        print("  ✗ Menu Paramètres introuvable.", file=sys.stderr); return []
    page.wait_for_timeout(1500)
    if not _clk(['text=Facturation', 'text=Billing'], 4000):
        print("  ✗ Onglet Facturation introuvable.", file=sys.stderr); return []
    page.wait_for_timeout(2500)

    # Chaque « Afficher » = un <a target=_blank href=invoice.stripe.com>. Ligne porte la date + $.
    rows = page.eval_on_selector_all(
        "a",
        """els => els.filter(e => (e.innerText||'').trim() === 'Afficher').map(e => {
            let p = e, row = '';
            for (let i = 0; i < 6; i++) {
                if (!p.parentElement) break; p = p.parentElement;
                const t = (p.innerText||'').replace(/\\n/g,' | ');
                if (t.includes('$') || t.includes('€')) { row = t.slice(0,100); break; }
            }
            return { href: e.getAttribute('href')||'', row };
        }).filter(x => x.href.includes('invoice.stripe.com'))"""
    )
    out = []
    for r in rows:
        d = _fr_date(r.get("row", ""))
        if d:
            out.append((d, r["href"]))
    return out


def _download(page, url: str, out_path: Path) -> bool:
    page.goto(url, timeout=45_000, wait_until="domcontentloaded")
    page.wait_for_timeout(4500)
    try:
        with page.expect_download(timeout=20_000) as di:
            page.get_by_text("Télécharger la facture", exact=True).first.click()
        di.value.save_as(str(out_path))
        return out_path.exists() and out_path.stat().st_size > 500
    except Exception as exc:
        print(f"  ⚠ download {out_path.name}: {exc}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Factures ChatGPT Plus via Chrome CDP.")
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--cdp", default=CDP_URL)
    parser.add_argument("--dry-run", action="store_true")
    # tolère les flags communs (--headed/--init-session/--session) sans s'en servir.
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--init-session", action="store_true")
    parser.add_argument("--session", default=None)
    args = parser.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else _ROOT / "reports/portals/openai/input"
    out_dir.mkdir(parents=True, exist_ok=True)

    pw, browser = _connect(args.cdp)
    if browser is None:
        return 0  # poller : sortie propre si Chrome debug absent
    try:
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        invoices = _open_billing(page)
        print(f"  {len(invoices)} facture(s) listée(s).", file=sys.stderr)
        rows = []
        for date_iso, url in invoices:
            if args.since and date_iso < args.since:
                continue
            out_path = out_dir / f"openai_{date_iso}.pdf"
            if args.dry_run:
                print(f"  [dry-run] {date_iso}", file=sys.stderr); continue
            if out_path.exists():
                rows.append({"vendor": VENDOR, "invoice_id": date_iso, "amount": 24.0,
                             "currency": "USD", "date": date_iso, "pdf_path": str(out_path)})
                continue
            if _download(page, url, out_path):
                print(f"  ✓ openai_{date_iso}.pdf", file=sys.stderr)
                rows.append({"vendor": VENDOR, "invoice_id": date_iso, "amount": 24.0,
                             "currency": "USD", "date": date_iso, "pdf_path": str(out_path)})
            else:
                print(f"  ✗ {date_iso}", file=sys.stderr)
    finally:
        pw.stop()   # détache CDP ; NE FERME PAS le Chrome de l'utilisateur

    if not args.dry_run and rows:
        write_manifest(rows, out_dir.parent / "manifest.json")
        print(f"\n{len(rows)} facture(s) → {out_dir.parent / 'manifest.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
