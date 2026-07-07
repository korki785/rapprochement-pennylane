"""État de santé du cockpit (Flux 5 + récap) — helpers PURS stdlib, testables.

Aucune dépendance Playwright/réseau ici : on ne fait que LIRE des fichiers déjà sur disque
(session storage_state, rapports .md, confirmed/suspects.json, CSV vendeurs) et en dériver
un statut. La sonde LIVE (navigateur headless) vit dans scripts/dashboard.py, pas ici.

- session_status(vendor)  : 🟢/🟡/🔴 depuis expiration cookie + mtime du fichier.
- flux_counts()           : compteurs rapproché/en attente par flux (en-tête des reconciliation_*.md).
- load_recap()            : confirmed.json (non justifié) + suspects.json (trouvé, non attaché).
- list_portal_vendors()   : roster depuis portal_vendors.csv.
"""
from __future__ import annotations

import csv
import json
import re
import socket
import time
from pathlib import Path
from typing import Dict, List, Optional

_ROOT = Path(__file__).resolve().parents[2]
_SRC = Path(__file__).parent

# Vendeurs pilotés par un vrai navigateur attaché (CDP port 9222) — PAS de storage_state.
# Leur santé = le port CDP est-il joignable (Chrome réel lancé par launch_chatgpt_chrome.sh).
_CDP_VENDORS = {"openai", "hunter"}
_CDP_PORT = 9222

# Seuils
STALE_DAYS = 7                      # mtime plus vieux que ça → 🟡 (à rafraîchir)
_SOON_SECS = 3 * 24 * 3600          # cookie expirant sous 3 j → 🟡

# Flux → dossier de rapport (pour les compteurs).
FLUX_DIRS = {
    "fournisseurs": "reports/fournisseurs",   # Flux 3
    "saas": "reports/saas",                    # Flux 4
    "portals": "reports/portals",              # Flux 5
    "recettes": "reports/recettes",            # Flux 7
    "ubereats": "reports/ubereats",            # Flux 2
}


def _now() -> float:
    return time.time()


def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# --- Santé de session ------------------------------------------------------

def session_file(handler_key: str) -> Path:
    return _ROOT / f".{handler_key}_session.json"


def _cdp_reachable(port: int = _CDP_PORT, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _session_horizon(state: dict, now: float) -> Optional[float]:
    """Horizon de session = expiration la plus LOINTAINE parmi les cookies encore valides.

    On IGNORE les cookies déjà expirés : ils sont bénins (consent, A/B test, anti-bot
    Cloudflare `__cf_bm`, `deployment_group`…), le navigateur les jette sans casser l'auth.
    Prendre le min de TOUS les cookies (ancien bug) mettait tout en rouge à tort. Le vrai
    cookie d'auth est long ; son expiration = l'horizon utile.
    """
    valid = [float(c["expires"]) for c in (state or {}).get("cookies", []) or []
             if isinstance(c.get("expires"), (int, float)) and c["expires"] > now]
    return max(valid) if valid else None


def _has_session_cookies(state: dict) -> bool:
    """Cookies de session (expires ≤ 0) : persistés dans storage_state, rejoués au chargement."""
    return any(not (isinstance(c.get("expires"), (int, float)) and c["expires"] > 0)
               for c in (state or {}).get("cookies", []) or [])


def session_status(handler_key: str, now: Optional[float] = None) -> Dict:
    """Statut RAPIDE (sans navigateur) : {handler_key, color, reason, mtime, expires_at}.

    DEVINETTE volontairement optimiste : on ne met ROUGE que si aucune session n'existe.
    Un cookie valide ne prouve pas que le serveur accepte encore la session (cf. Bouygues) —
    d'où le bouton « Tester » (sonde live) qui, lui, fait foi.
    """
    now = _now() if now is None else now

    if handler_key in _CDP_VENDORS:
        up = _cdp_reachable()
        return {
            "handler_key": handler_key,
            "color": "green" if up else "red",
            "reason": "Chrome CDP 9222 joignable" if up else "Chrome CDP 9222 injoignable — lancer launch_chatgpt_chrome.sh",
            "mtime": None,
            "expires_at": None,
            "cdp": True,
        }

    path = session_file(handler_key)
    if not path.exists():
        return {"handler_key": handler_key, "color": "red",
                "reason": "aucune session — --init-session requis",
                "mtime": None, "expires_at": None, "cdp": False}

    mtime = path.stat().st_mtime
    state = load_json(path) or {}
    horizon = _session_horizon(state, now)

    # Aucun cookie valide ET aucun cookie de session → vraiment vide.
    if horizon is None and not _has_session_cookies(state):
        return {"handler_key": handler_key, "color": "red",
                "reason": "aucun cookie valide — reconnexion requise",
                "mtime": mtime, "expires_at": None, "cdp": False}

    age_days = (now - mtime) / 86400.0
    # Jaune seulement si MÊME le cookie le plus lointain expire sous 3 j (tout expire bientôt).
    if horizon is not None and (horizon - now) < _SOON_SECS:
        return {"handler_key": handler_key, "color": "yellow",
                "reason": "session expire bientôt", "mtime": mtime, "expires_at": horizon, "cdp": False}
    if age_days > STALE_DAYS:
        return {"handler_key": handler_key, "color": "yellow",
                "reason": f"session vieille de {age_days:.0f} j (à tester)", "mtime": mtime,
                "expires_at": horizon, "cdp": False}

    return {"handler_key": handler_key, "color": "green",
            "reason": "session présente", "mtime": mtime, "expires_at": horizon, "cdp": False}


# --- Roster vendeurs -------------------------------------------------------

def list_portal_vendors(csv_path: Optional[Path] = None) -> List[Dict]:
    """Lit portal_vendors.csv (name, handler_key, env_prefix, billing_url, note)."""
    path = csv_path or (_SRC / "portal_vendors.csv")
    out = []
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(r for r in f if not r.startswith("#"))
        for row in reader:
            key = (row.get("handler_key") or "").strip()
            if not key:
                continue
            out.append({k: (v or "").strip() for k, v in row.items()})
    return out


# --- Compteurs par flux ----------------------------------------------------

_NUM = r"(\d+)"


def _latest_report(flux_dir: Path) -> Optional[Path]:
    files = sorted(flux_dir.glob("reconciliation_*.md"))
    return files[-1] if files else None


def parse_report_header(text: str) -> Dict:
    """Extrait les compteurs de l'en-tête d'un reconciliation_*.md.

    Formats variés selon le flux (gras ou non, terminologie différente) :
      fournisseurs : **49 facture(s) rapprochée(s)** (46 fiable(s), 3 à vérifier) … **231 transaction(s)** sans facture
      saas         : 1 facture(s) rapprochée(s) (0 fiable(s), 1 à vérifier), 71 non rapprochée(s).
      portals      : 0 rapprochée(s) (0 exacte(s), 0 warn), 20 non rapprochée(s).
    Tolérant : gras optionnel, chaque champ absent → None.
    """
    def grab(pattern: str) -> Optional[int]:
        m = re.search(pattern, text, re.IGNORECASE)
        return int(m.group(1)) if m else None

    matched = grab(rf"{_NUM}\s+(?:facture\(s\)\s+)?rapproch")
    to_verify = grab(rf"{_NUM}\s+à\s+vérifier")
    if to_verify is None:
        to_verify = grab(rf"{_NUM}\s+warn")
    # « en attente » = non rapprochée(s), sinon transaction(s) sans facture (fournisseurs).
    pending = grab(rf"{_NUM}\s+non\s+rapproch")
    if pending is None:
        pending = grab(rf"{_NUM}\s+transaction\(s\)\*{{0,2}}\s+sans\s+facture")
    return {
        "matched": matched,
        "reliable": grab(rf"\(\s*{_NUM}\s+(?:fiable|exacte)"),
        "to_verify": to_verify,
        "pending": pending,
    }


def flux_counts() -> Dict[str, Dict]:
    """Compteurs par flux depuis le dernier reconciliation_*.md de chaque dossier."""
    out = {}
    for flux, rel in FLUX_DIRS.items():
        d = _ROOT / rel
        entry = {"report": None, "date": None, "counts": {}}
        if d.is_dir():
            rep = _latest_report(d)
            if rep:
                entry["report"] = str(rep.relative_to(_ROOT))
                m = re.search(r"reconciliation_(\d{4}-\d{2}-\d{2})", rep.name)
                entry["date"] = m.group(1) if m else None
                entry["counts"] = parse_report_header(rep.read_text(encoding="utf-8"))
        out[flux] = entry
    return out


# --- Récap non-rapprochés (autoritatif) ------------------------------------

def load_recap() -> Dict:
    """confirmed.json (non justifié) + suspects.json (trouvé mais non attaché) + backlog."""
    recap = _ROOT / "reports" / "recap"
    confirmed = load_json(recap / "confirmed.json") or []
    suspects = load_json(recap / "suspects.json") or []
    backlog = load_json(_ROOT / "reports" / "portals" / "backlog.json") or []
    return {
        "confirmed": confirmed if isinstance(confirmed, list) else [],
        "suspects": suspects if isinstance(suspects, list) else [],
        "backlog": backlog if isinstance(backlog, list) else [],
    }
