#!/usr/bin/env python3
"""Télécharge les factures Hunter.io (HUNTER.IO STARTER, 49 €/mois).

hunter.io met un **Cloudflare Turnstile** au login (`/users/sign_in`) qui bloque tout navigateur
automatisé. Comme pour ChatGPT/OpenAI, on s'ATTACHE via CDP au VRAI Chrome de l'utilisateur
(--remote-debugging-port=9222), que Cloudflare laisse passer.

Pré-requis (une fois) :
    open -na "Google Chrome" --args --remote-debugging-port=9222 "--remote-allow-origins=*" \\
         --user-data-dir="$HOME/.chrome-recon-debug" "https://hunter.io/users/sign_in"
    → se connecter à Hunter UNE fois (résoudre le Turnstile + éventuel challenge de vérif).

Chemin scrapé : `hunter.io/subscriptions` → tableau de factures (date ISO, montant €, « Download »)
→ chaque « Download » pointe vers une facture Stripe (invoice.stripe.com) → bouton
« Télécharger la facture » / « Download invoice » → PDF.

Usage :
    python3 scripts/portals/fetch_hunter.py [--since YYYY-MM-DD] [--cdp http://localhost:9222]
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

VENDOR = "hunter"
CDP_URL = "http://localhost:9222"
BILLING_URL = "https://hunter.io/subscriptions"
DEFAULT_SINCE = "2026-04-01"
_DL_LABELS = ["Télécharger la facture", "Download invoice", "Download", "Télécharger"]


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
              "    Lancer le Chrome dédié (voir README) et se connecter à Hunter.", file=sys.stderr)
        return None, None
    return pw, browser


def _open_billing(page) -> list:
    """Renvoie [(date_iso, amount_float, stripe_url)] depuis hunter.io/subscriptions."""
    page.goto(BILLING_URL, timeout=60_000, wait_until="domcontentloaded")
    page.wait_for_timeout(3500)
    if "challenge" in page.url or "sign_in" in page.url:
        print("  ✗ Pas connecté à Hunter dans le Chrome debug (login/challenge requis).", file=sys.stderr)
        return []
    rows = page.eval_on_selector_all(
        "a",
        """els => els.filter(e => (e.innerText||'').trim() === 'Download').map(e => {
            let p = e, row = '';
            for (let i = 0; i < 6; i++) {
                if (!p.parentElement) break; p = p.parentElement;
                const t = (p.innerText||'').replace(/\\n/g,' | ');
                if (/\\d{4}-\\d{2}-\\d{2}/.test(t) && (t.includes('€') || t.includes('$'))) {
                    row = t.slice(0,120); break;
                }
            }
            return { href: e.getAttribute('href')||'', row };
        }).filter(x => x.href.includes('invoice.stripe.com'))"""
    )
    out = []
    for r in rows:
        md = re.search(r'\d{4}-\d{2}-\d{2}', r["row"])
        ma = re.search(r'(\d+[.,]\d{2})\s*[€$]', r["row"]) or re.search(r'[€$]\s*(\d+[.,]\d{2})', r["row"])
        if md:
            amt = float(ma.group(1).replace(",", ".")) if ma else 0.0
            out.append((md.group(0), amt, r["href"]))
    return out


def _download(page, url: str, out_path: Path) -> bool:
    page.goto(url, timeout=45_000, wait_until="domcontentloaded")
    page.wait_for_timeout(4500)
    for label in _DL_LABELS:
        try:
            with page.expect_download(timeout=12_000) as di:
                page.get_by_text(label, exact=True).first.click()
            di.value.save_as(str(out_path))
            if out_path.exists() and out_path.stat().st_size > 500:
                return True
        except Exception:
            continue
    print(f"  ⚠ download {out_path.name} : bouton Stripe introuvable", file=sys.stderr)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Factures Hunter.io via Chrome CDP.")
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--cdp", default=CDP_URL)
    parser.add_argument("--dry-run", action="store_true")
    # tolère les flags communs (--headed/--init-session/--session) sans s'en servir.
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--init-session", action="store_true")
    parser.add_argument("--session", default=None)
    args = parser.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else _ROOT / "reports/portals/hunter/input"
    out_dir.mkdir(parents=True, exist_ok=True)

    pw, browser = _connect(args.cdp)
    if browser is None:
        return 0  # poller : sortie propre si Chrome debug absent
    try:
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if "hunter" in p.url), None) or \
            (ctx.pages[0] if ctx.pages else ctx.new_page())
        invoices = _open_billing(page)
        print(f"  {len(invoices)} facture(s) listée(s).", file=sys.stderr)
        rows = []
        for date_iso, amount, url in invoices:
            if args.since and date_iso < args.since:
                continue
            out_path = out_dir / f"hunter_{date_iso}_{amount:.2f}.pdf"
            if args.dry_run:
                print(f"  [dry-run] {date_iso}  {amount:.2f}€", file=sys.stderr)
                continue
            entry = {"vendor": VENDOR, "invoice_id": f"{date_iso}_{amount:.2f}", "amount": amount,
                     "currency": "EUR", "date": date_iso, "pdf_path": str(out_path)}
            if out_path.exists() or _download(page, url, out_path):
                print(f"  ✓ {out_path.name}", file=sys.stderr)
                rows.append(entry)
            else:
                print(f"  ✗ {date_iso} {amount:.2f}", file=sys.stderr)
    finally:
        pw.stop()   # détache CDP ; NE FERME PAS le Chrome de l'utilisateur

    if not args.dry_run and rows:
        write_manifest(rows, out_dir.parent / "manifest.json")
        print(f"\n{len(rows)} facture(s) → {out_dir.parent / 'manifest.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
