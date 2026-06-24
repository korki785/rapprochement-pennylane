#!/usr/bin/env python3
"""Télécharge les factures UberEats depuis le portail web (Playwright).

Login email + mot de passe (UBEREATS_EMAIL / UBEREATS_PASSWORD dans .env).
La session (cookies) est sauvegardée pour éviter de se reconnecter à chaque fois.

Usage :
    python3 scripts/fetch_ubereats_invoices.py \\
        --out-dir reports/ubereats/input \\
        [--since YYYY-MM-DD] [--headed] [--dry-run] \\
        [--session .ubereats_session.json]

Dépendance : pip install -e ".[portal]" && playwright install chromium
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recon.config import load_dotenv  # noqa: E402

DEFAULT_SINCE = "2026-04-01"
DEFAULT_SESSION = ".ubereats_session.json"
ORDERS_URL = "https://www.ubereats.com/orders"
LOGIN_URL = "https://auth.uber.com/v2/"
TIMEOUT_MS = 20000


def load_credentials() -> tuple[str, str]:
    load_dotenv()
    email = os.environ.get("UBEREATS_EMAIL", "").strip()
    password = os.environ.get("UBEREATS_PASSWORD", "").strip()
    if not email or not password:
        raise SystemExit(
            "UBEREATS_EMAIL / UBEREATS_PASSWORD manquants. Les ajouter au .env."
        )
    return email, password


def _dismiss_popups(page) -> None:
    """Ferme les bandeaux cookies et popups connus avant d'interagir avec la page."""
    for sel in [
        'button:has-text("Accept")',
        'button:has-text("Accepter")',
        'button:has-text("Got it")',
        'button:has-text("OK")',
        'button:has-text("Not now")',
        'button[aria-label="Close"]',
        'button[data-testid="modal-close-btn"]',
    ]:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(800)
        except Exception:
            pass


def _is_logged_in(page) -> bool:
    """Vrai si la page commandes s'affiche (pas de redirection vers le login)."""
    return "auth.uber.com" not in page.url and "/login" not in page.url


def login(page, email: str, password: str) -> None:
    """Ouvre la page de connexion et attend que l'utilisateur se connecte manuellement.

    Supporte tous les flux (email+password, Google, Apple, OTP) car c'est l'utilisateur
    qui effectue la connexion dans le navigateur affiché.
    Timeout : 3 minutes pour compléter.
    """
    page.goto(LOGIN_URL, timeout=TIMEOUT_MS)
    print(
        "\n>>> Connectez-vous dans le navigateur qui vient de s'ouvrir.\n"
        "    (Google, email, ou autre méthode — peu importe.)\n"
        "    Une fois connecté et sur la page UberEats, revenez ici\n"
        "    et appuyez sur ENTRÉE pour continuer.\n",
        file=sys.stderr,
    )
    input()  # attend que l'utilisateur appuie sur Entrée


def collect_orders(page, since: str) -> List[dict]:
    """Liste les commandes {id, url} depuis la page historique."""
    page.goto(ORDERS_URL, timeout=TIMEOUT_MS)
    page.wait_for_load_state("load", timeout=TIMEOUT_MS)
    # Laisser le JS React rendre les commandes.
    page.wait_for_timeout(3000)
    _dismiss_popups(page)
    print(f"  [debug] URL après navigation : {page.url}", file=sys.stderr)
    page.screenshot(path="reports/ubereats/debug_orders.png")
    print("  [debug] Capture : reports/ubereats/debug_orders.png", file=sys.stderr)

    # Charger plus de commandes : scroll infini + bouton « Voir plus ».
    prev_height = 0
    for _ in range(20):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
        more = page.query_selector(
            'button:has-text("Voir plus"), button:has-text("Show more"), '
            'button:has-text("See more"), button:has-text("Load more")'
        )
        if more and more.is_visible():
            more.click()
            page.wait_for_timeout(2000)
        cur_height = page.evaluate("document.body.scrollHeight")
        if cur_height == prev_height:
            break
        prev_height = cur_height

    orders: List[dict] = []
    seen: set[str] = set()
    import urllib.parse as _up

    # Extraire chaque commande : UUID (via lien "View receipt") + montant + date depuis la carte.
    for link in page.query_selector_all('a[href*="mod=orderReceipt"]'):
        href = link.get_attribute("href") or ""
        try:
            qs = _up.parse_qs(_up.urlparse(href).query)
            oid = qs.get("modctx", [""])[0]
        except Exception:
            continue
        if len(oid) < 8 or oid in seen:
            continue
        seen.add(oid)

        # Texte de la carte parente — remonter le DOM jusqu'à trouver un € .
        try:
            card_text = link.evaluate("""el => {
                let p = el;
                for (let i = 0; i < 12; i++) {
                    if (!p || !p.parentElement) break;
                    p = p.parentElement;
                    const t = p.innerText || '';
                    if (t.includes('€') && t.length < 2000) return t;
                }
                return '';
            }""") or ""
        except Exception:
            card_text = ""

        # Montant : "N items for €XX.XX" ou "€XX.XX"
        amt_m = re.search(r'for\s+€\s*(\d+[.,]\d{2})', card_text) \
             or re.search(r'€\s*(\d+[.,]\d{2})', card_text)
        amount = float(amt_m.group(1).replace(",", ".")) if amt_m else 0.0

        # Date : "Jun 24 at 12:09 PM" ou "24 juin 12h09"
        date_iso = ""
        # Format anglais : "Jun 24 at …" ou "Jun 24, 2026"
        dm = re.search(
            r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})(?:[,\s]+(\d{4}))?\b',
            card_text
        )
        if dm:
            month_abbr = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
                          "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
            mo = month_abbr.get(dm.group(1), 0)
            day = int(dm.group(2))
            if dm.group(3):
                yr = int(dm.group(3))
            else:
                # Pas d'année → déduire : si mois > mois courant, c'est l'année précédente.
                import datetime as _dt
                today = _dt.date.today()
                yr = today.year if mo <= today.month else today.year - 1
            if mo:
                date_iso = f"{yr:04d}-{mo:02d}-{day:02d}"
        # Format français : "24 juin YYYY"
        if not date_iso:
            fr_months = {"janvier":1,"février":2,"fevrier":2,"mars":3,"avril":4,
                         "mai":5,"juin":6,"juillet":7,"août":8,"aout":8,
                         "septembre":9,"octobre":10,"novembre":11,"décembre":12,"decembre":12}
            fm = re.search(r'\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})\b', card_text)
            if fm:
                mo = fr_months.get(fm.group(2).lower())
                if mo:
                    date_iso = f"{int(fm.group(3)):04d}-{mo:02d}-{int(fm.group(1)):02d}"

        # Filtrer par date si connue.
        if date_iso and since and date_iso < since:
            continue

        url = "https://www.ubereats.com" + href if href.startswith("/") else href
        orders.append({"id": oid, "url": url, "amount": amount, "date": date_iso})

    return orders


def scrape_receipt(page, order: dict, out_dir: Path) -> Optional[dict]:
    """Utilise les données déjà extraites de la carte commande.
    Sauvegarde une screenshot de la page de reçu comme justificatif.
    """
    amount = order.get("amount", 0.0)
    date_iso = order.get("date", "")

    if not date_iso or amount <= 0:
        return None

    # Screenshot de la page de reçu comme justificatif visuel.
    try:
        page.goto(order["url"], timeout=TIMEOUT_MS)
        page.wait_for_load_state("load", timeout=TIMEOUT_MS)
        page.wait_for_timeout(2000)
        _dismiss_popups(page)
    except Exception:
        pass

    png_path = out_dir / f"ubereats_{order['id'][:12]}.png"
    try:
        page.screenshot(path=str(png_path), full_page=False)
    except Exception:
        pass

    return {
        "filename": png_path.name,
        "date": date_iso,
        "amount": round(amount, 2),
        "invoice_id": order["id"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Télécharge les factures UberEats (portail).")
    parser.add_argument("--out-dir", default="reports/ubereats/input")
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--session", default=DEFAULT_SESSION,
                        help="Fichier storage_state Playwright (réutilise l'auth).")
    parser.add_argument("--headed", action="store_true", help="Affiche le navigateur.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Liste les commandes trouvées, ne télécharge rien.")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            'Playwright absent. Installer : pip install -e ".[portal]" '
            "&& playwright install chromium"
        )

    email, password = load_credentials()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    Path("reports/ubereats").mkdir(parents=True, exist_ok=True)
    session_path = Path(args.session)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        ctx_kwargs = {"accept_downloads": True}
        if session_path.exists():
            ctx_kwargs["storage_state"] = str(session_path)
        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()

        # Naviguer vers la page commandes et vérifier la connexion.
        # On essaie d'abord avec la session sauvegardée (si elle existe).
        # Si la page est vide (non connecté), on force la reconnexion.
        page.goto(ORDERS_URL, timeout=TIMEOUT_MS)
        page.wait_for_load_state("load", timeout=TIMEOUT_MS)
        page.wait_for_timeout(4000)

        # Détecter si vraiment connecté : chercher un élément propre à la page connectée.
        is_authenticated = bool(
            page.query_selector('a[href*="/orders/"], [data-testid*="order"], h1')
        )
        if not is_authenticated or not _is_logged_in(page):
            print("Connexion à UberEats…", file=sys.stderr)
            login(page, email, password)
            # Après login, revenir à la page commandes.
            page.goto(ORDERS_URL, timeout=TIMEOUT_MS)
            page.wait_for_load_state("load", timeout=TIMEOUT_MS)
            page.wait_for_timeout(4000)

        context.storage_state(path=str(session_path))

        orders = collect_orders(page, args.since)
        print(f"{len(orders)} commande(s) trouvée(s).", file=sys.stderr)

        if args.dry_run:
            for o in orders:
                print(f"  {o['id']}  {o['url']}", file=sys.stderr)
            context.close()
            browser.close()
            return 0

        manifest_rows = []
        for o in orders:
            row = scrape_receipt(page, o, out_dir)
            if row:
                manifest_rows.append(row)
                print(f"  ✓ {row['filename']}  {row['date']}  {row['amount']} €",
                      file=sys.stderr)
            else:
                print(f"  ✗ extraction impossible : {o['id'][:8]}", file=sys.stderr)

        # Écrire manifest.csv (lu par reconcile_ubereats.py comme fallback).
        manifest_path = out_dir.parent / "manifest.csv"
        import csv as _csv
        with manifest_path.open("w", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=["filename", "date", "amount", "invoice_id"])
            w.writeheader()
            w.writerows(manifest_rows)

        context.close()
        browser.close()
        print(f"\n{len(manifest_rows)} reçu(s) extraits → {manifest_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
