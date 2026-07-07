#!/usr/bin/env python3
"""Pilote le SITE web Qonto (app.qonto.com) pour marquer une tx « justificatif non requis ».

L'API third-party ne l'expose pas (PATCH /transactions → 404). Seul le web le permet.
On réutilise l'infra portails : storage_state Playwright + login manuel (2FA/SCA).

    python3 scripts/qonto_web.py --init-session          # capture le login (headed, 2FA)
    python3 scripts/qonto_web.py --inspect --amount 80.99 --date 2026-06-05
    python3 scripts/qonto_web.py --mark-optional --amount 80.99 --date 2026-06-05 --label "AIR FRANCE"

Fragile par nature (UI Qonto = SPA Ember susceptible de changer).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[0]
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_SCRIPTS))

from recon.portals import session_path  # noqa: E402
from _portal_base import launch_browser, save_session, dismiss_popups, wait_for_manual_login  # noqa: E402

VENDOR = "qonto_web"
# La racine redirige vers la vraie page de connexion (/login est un 404 chez Qonto).
LOGIN_URL = "https://app.qonto.com/"
TX_URL = "https://app.qonto.com/"

# Libellés du bouton « non requis » (FR/EN, à affiner après inspection réelle).
OPTIONAL_TEXTS = [
    "Justificatif non requis", "Reçu non requis", "Marquer comme non requis",
    "Non requis", "No receipt required", "Mark as not required", "Not required",
]


def _is_logged_in(page) -> bool:
    try:
        return "app.qonto.com" in page.url and "/login" not in page.url and "/signin" not in page.url
    except Exception:
        return False


def _ensure_login(page, init_session: bool) -> bool:
    page.goto(LOGIN_URL, timeout=60_000)
    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2500)
    dismiss_popups(page)
    if _is_logged_in(page):
        return True
    if init_session:
        return wait_for_manual_login(page, "Qonto (app.qonto.com — email + 2FA/SCA)",
                                     lambda: _is_logged_in(page), timeout_s=600)
    return _is_logged_in(page)


def _open_transaction(page, amount: float, label: str) -> bool:
    """Ouvre le panneau de la transaction. Best-effort : recherche par montant/libellé.

    À FIABILISER après inspection : URL des transactions, champ de recherche, sélecteur de ligne.
    """
    page.goto(TX_URL, timeout=60_000)
    page.wait_for_load_state("networkidle", timeout=45_000)
    page.wait_for_timeout(3000)
    dismiss_popups(page)
    # Tente une recherche par libellé si un champ existe.
    for sel in ['input[type="search"]', 'input[placeholder*="echerch"]', 'input[placeholder*="earch"]']:
        box = page.query_selector(sel)
        if box:
            try:
                box.fill(label or f"{amount:.2f}")
                page.wait_for_timeout(2500)
            except Exception:
                pass
            break
    # Clique la 1re ligne dont le texte contient le montant.
    amt_txt = f"{amount:.2f}".replace(".", ",")
    rows = page.query_selector_all('[data-test-transaction], tr, [role="row"], li')
    for r in rows:
        try:
            t = (r.inner_text() or "")
        except Exception:
            continue
        if amt_txt in t or f"{amount:.2f}" in t:
            try:
                r.click()
                page.wait_for_timeout(2500)
                return True
            except Exception:
                continue
    return False


def _click_optional(page) -> bool:
    """Clique « justificatif non requis » dans le panneau ouvert (parfois via un menu ⋯)."""
    # Ouvre un éventuel menu d'actions.
    for menu in ['button[aria-label*="ption"]', 'button[aria-label*="ction"]',
                 'button:has-text("⋯")', '[data-test-menu-trigger]']:
        b = page.query_selector(menu)
        if b:
            try:
                b.click(); page.wait_for_timeout(1200)
            except Exception:
                pass
    for txt in OPTIONAL_TEXTS:
        try:
            el = page.query_selector(f'button:has-text("{txt}"), [role="menuitem"]:has-text("{txt}"), a:has-text("{txt}")')
            if el and el.is_visible():
                el.click(); page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


def _inspect(page, amount: float, label: str) -> None:
    """Dump la structure pour construire les bons sélecteurs (dev)."""
    ok = _open_transaction(page, amount, label)
    print(f"transaction ouverte ? {ok}  url={page.url}", file=sys.stderr)
    body = (page.inner_text("body") or "")
    hits = [t for t in OPTIONAL_TEXTS if t.lower() in body.lower()]
    print(f"textes 'non requis' présents dans la page : {hits}", file=sys.stderr)
    btns = page.evaluate("""() => [...document.querySelectorAll('button,[role=menuitem],a')]
        .map(e=>(e.innerText||'').trim()).filter(t=>t && t.length<40).slice(0,60)""")
    print("boutons/menus visibles :", file=sys.stderr)
    for b in btns:
        print("   ", b, file=sys.stderr)


def mark_optional(amount: float, date: str, label: str, headed: bool,
                  init_session: bool, inspect: bool) -> int:
    sess = session_path(VENDOR)
    pw, browser, ctx, page = launch_browser(sess if not init_session else None, headed or init_session)
    try:
        if not _ensure_login(page, init_session):
            print("  ⚠ non connecté — relancer avec --init-session.", file=sys.stderr)
            return 2
        save_session(ctx, sess)
        if init_session:
            print("  ✓ session Qonto web sauvegardée.", file=sys.stderr)
            return 0
        if inspect:
            _inspect(page, amount, label)
            return 0
        if not _open_transaction(page, amount, label):
            print("  ✗ transaction introuvable dans l'UI.", file=sys.stderr)
            return 3
        if _click_optional(page):
            print("  ✓ marquée « justificatif non requis ».", file=sys.stderr)
            return 0
        print("  ✗ bouton « non requis » introuvable (UI à ré-inspecter).", file=sys.stderr)
        return 4
    finally:
        ctx.close(); browser.close(); pw.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description="Marque une tx Qonto « justificatif non requis » (web).")
    ap.add_argument("--init-session", action="store_true")
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--mark-optional", action="store_true")
    ap.add_argument("--amount", type=float, default=0.0)
    ap.add_argument("--date", default="")
    ap.add_argument("--label", default="")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    return mark_optional(args.amount, args.date, args.label,
                         args.headed, args.init_session, args.inspect)


if __name__ == "__main__":
    raise SystemExit(main())
