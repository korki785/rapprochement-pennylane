"""Utilitaires Playwright partagés par les fetchers de portails (Flux 5).

Chaque fetcher inclut :
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
    from _portal_base import launch_browser, dismiss_popups, download_or_screenshot, write_manifest
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from recon.config import load_dotenv  # noqa: E402

TIMEOUT_MS = 30_000
NAV_TIMEOUT_MS = 60_000


def load_credentials(env_prefix: str) -> tuple:
    load_dotenv()
    email = os.environ.get(f"{env_prefix}_EMAIL", "").strip()
    password = os.environ.get(f"{env_prefix}_PASSWORD", "").strip()
    phone = os.environ.get(f"{env_prefix}_PHONE", "").strip()
    if not email and not phone:
        print(
            f"⚠  {env_prefix}_EMAIL (ou {env_prefix}_PHONE) absent du .env.\n"
            "   Utilisez --init-session pour vous connecter manuellement.",
            file=sys.stderr,
        )
    return email, password, phone


def launch_browser(session_file: Optional[Path], headed: bool):
    """Lance Playwright et retourne (playwright, browser, context, page)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            'Playwright absent. Installer : pip install -e ".[portal]" && playwright install chromium'
        )
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=not headed)
    ctx_kwargs: Dict = {"accept_downloads": True}
    if session_file and session_file.exists():
        ctx_kwargs["storage_state"] = str(session_file)
    ctx = browser.new_context(**ctx_kwargs)
    page = ctx.new_page()
    page.set_default_timeout(TIMEOUT_MS)
    page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    return pw, browser, ctx, page


def save_session(ctx, session_file: Path) -> None:
    ctx.storage_state(path=str(session_file))
    print(f"  Session sauvegardée → {session_file.name}", file=sys.stderr)


def dismiss_popups(page) -> None:
    for sel in [
        'button:has-text("Accept all")',
        'button:has-text("Accept")',
        'button:has-text("Accepter")',
        'button:has-text("Got it")',
        'button:has-text("OK")',
        'button:has-text("Agree")',
        'button:has-text("I agree")',
        'button:has-text("Not now")',
        'button:has-text("Close")',
        'button:has-text("Fermer")',
        '[aria-label="Close"]',
        '[aria-label="Fermer"]',
        '[data-testid="modal-close-btn"]',
        '[data-testid="close-button"]',
    ]:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(600)
        except Exception:
            pass


def wait_for_manual_login(page, vendor_name: str, logged_in_check=None,
                          timeout_s: int = 300) -> bool:
    """Attend une connexion MANUELLE dans le navigateur headed, SANS input() stdin.

    Indispensable : lancé via `!` (Claude Code) ou un shell pipé, il n'y a pas de stdin
    interactif → un input() lèverait EOFError et fermerait le navigateur aussitôt. On
    SCRUTE donc la page : dès que `logged_in_check()` est vrai et stable, on continue.

    `logged_in_check` : callable() -> bool (closure sur la page). None = attente passive
    (timeout complet). Renvoie True si connexion détectée, False si timeout.
    """
    print(
        f"\n>>> Connectez-vous à {vendor_name} dans le navigateur ouvert.\n"
        f"    Reprise AUTOMATIQUE dès la connexion détectée (≤ {timeout_s//60} min, "
        "aucune touche à presser).\n",
        file=sys.stderr,
    )
    deadline = time.monotonic() + timeout_s
    stable = 0
    ticks = 0
    last_url = ""
    while time.monotonic() < deadline:
        page.wait_for_timeout(2000)
        ticks += 1
        # Trace l'URL courante (toutes ~10 s ou à chaque changement) pour diagnostiquer
        # un mauvais onglet / une redirection inattendue.
        try:
            cur = page.url
        except Exception:
            cur = "(page fermée ?)"
        if cur != last_url or ticks % 5 == 0:
            print(f"    … page actuelle : {cur}", file=sys.stderr, flush=True)
            last_url = cur
        ok = False
        if logged_in_check is not None:
            try:
                ok = bool(logged_in_check())
            except Exception:
                ok = False
        if ok:
            stable += 1
            if stable >= 2:  # ~4 s stable -> redirection post-login terminée
                print("    ✓ Connexion détectée, reprise.", file=sys.stderr, flush=True)
                return True
        else:
            stable = 0
    print("    ⚠ Délai dépassé sans connexion détectée.", file=sys.stderr, flush=True)
    return False


def fill_login_form(page, email: str, password: str,
                    email_sel: str, pass_sel: str,
                    submit_sel: str, between_timeout: int = 800) -> bool:
    """Remplit un formulaire email+password. Retourne True si réussi."""
    try:
        page.fill(email_sel, email)
        page.wait_for_timeout(between_timeout)
        page.click(submit_sel)
        page.wait_for_timeout(between_timeout)
        # Champ password peut apparaître après le champ email (flux en 2 étapes).
        pw_field = page.query_selector(pass_sel)
        if pw_field and pw_field.is_visible():
            pw_field.fill(password)
            page.wait_for_timeout(300)
            page.click(submit_sel)
        return True
    except Exception as exc:
        print(f"  ⚠ Formulaire de connexion : {exc}", file=sys.stderr)
        return False


def download_or_screenshot(
    ctx, page, trigger,
    out_path: Path, timeout: int = 30_000
) -> bool:
    """Télécharge un PDF via download event ou screenshot Playwright → PDF.

    `trigger` : sélecteur CSS ou callable() qui déclenche le téléchargement.
    Retourne True si le fichier a été produit.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Tentative 1 : download natif du navigateur.
    try:
        with ctx.expect_download(timeout=timeout) as dl_info:
            if callable(trigger):
                trigger()
            else:
                page.click(trigger)
        dl = dl_info.value
        dl.save_as(str(out_path))
        if out_path.exists() and out_path.stat().st_size > 500:
            return True
    except Exception:
        pass

    # Tentative 2 : page.pdf() (Chromium headless).
    try:
        page.pdf(path=str(out_path), format="A4", print_background=True)
        if out_path.exists() and out_path.stat().st_size > 500:
            return True
    except Exception as exc:
        print(f"  ⚠ page.pdf() : {exc}", file=sys.stderr)

    return False


def render_page_to_pdf(page, out_path: Path) -> bool:
    """Convertit la page courante en PDF via page.pdf(). Retry 1x si vide."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            page.pdf(path=str(out_path), format="A4", print_background=True)
            if out_path.exists() and out_path.stat().st_size > 500:
                return True
        except Exception as exc:
            if attempt == 0:
                page.wait_for_timeout(2000)
            else:
                print(f"  ⚠ render_page_to_pdf : {exc}", file=sys.stderr)
    return False


def write_manifest(entries: List[Dict], manifest_path: Path) -> None:
    """Écrit/fusionne les entrées dans manifest.json (déduplique par invoice_id)."""
    existing: Dict[str, Dict] = {}
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                existing = {e["invoice_id"]: e for e in data if e.get("invoice_id")}
            elif isinstance(data, dict):
                existing = {e["invoice_id"]: e for e in data.values() if e.get("invoice_id")}
        except Exception:
            pass
    for e in entries:
        iid = e.get("invoice_id")
        if iid:
            existing[iid] = e
    manifest_path.write_text(
        json.dumps(list(existing.values()), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def std_args(parser) -> None:
    """Ajoute les arguments communs à tous les fetchers de portail."""
    parser.add_argument("--out-dir", default=None,
                        help="Dossier de sortie pour les PDF (défaut : reports/portals/{vendor}/input).")
    parser.add_argument("--since", default="2026-04-01")
    parser.add_argument("--session", default=None,
                        help="Fichier storage_state Playwright (défaut : .{vendor}_session.json).")
    parser.add_argument("--headed", action="store_true", help="Affiche le navigateur.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Liste les factures, ne télécharge rien.")
    parser.add_argument("--init-session", action="store_true",
                        help="Ouvre le navigateur pour connexion manuelle (2FA / OTP).")
