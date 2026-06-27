"""Utilitaires communs du Flux 5 — Portails.

- load_portal_vendors  : lit portal_vendors.csv
- detect_unreconciled  : débits Qonto sans PJ (attachment_required=True, attachment_ids=[])
- match_vendor         : retrouve le vendeur d'une transaction via le libellé
- session_path         : chemin du fichier storage_state Playwright
- credentials          : lit {PREFIX}_EMAIL et {PREFIX}_PASSWORD dans .env
"""
from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Dict, List, Optional

from .config import load_dotenv
from .qonto_client import QontoClient

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CSV = Path(__file__).parent / "portal_vendors.csv"


def load_portal_vendors(csv_path: Optional[Path] = None) -> List[Dict]:
    path = csv_path or _DEFAULT_CSV
    vendors = []
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(r for r in f if not r.startswith("#"))
        for row in reader:
            if not row.get("handler_key", "").strip():
                continue
            vendors.append({k: v.strip() for k, v in row.items()})
    return vendors


def detect_unreconciled(client: QontoClient, since: str) -> List[Dict]:
    """Retourne les débits depuis `since` avec attachment_required=True et attachment_ids=[]."""
    out = []
    for acct in client.list_bank_account_ids():
        for tx in client.iter_transactions(acct):
            if tx.get("side") != "debit":
                continue
            settled = str(tx.get("settled_at") or tx.get("emitted_at") or "")[:10]
            if settled and settled < since:
                continue
            if not tx.get("attachment_required"):
                continue
            if tx.get("attachment_ids"):
                continue
            out.append({
                "id": str(tx.get("id") or ""),
                "label": str(tx.get("label") or ""),
                "amount": tx.get("amount"),
                "currency": tx.get("currency") or "EUR",
                "local_amount": tx.get("local_amount"),
                "local_currency": tx.get("local_currency") or "",
                "settled_at": settled,
                "operation_type": str(tx.get("operation_type") or ""),
            })
    return out


def match_vendor(tx: Dict, vendors: List[Dict]) -> Optional[Dict]:
    """Retourne le premier vendeur dont un pattern correspond au libellé de la transaction."""
    label_up = (tx.get("label") or "").upper()
    for v in vendors:
        patterns = [
            p.strip().upper()
            for p in v.get("qonto_label_patterns", "").split("|")
            if p.strip()
        ]
        if any(pat in label_up for pat in patterns):
            return v
    return None


def session_path(vendor_key: str) -> Path:
    return _ROOT / f".{vendor_key}_session.json"


def credentials(env_prefix: str) -> tuple:
    """Retourne (email, password, phone) depuis .env. Ne lève pas d'exception si absents."""
    load_dotenv()
    email = os.environ.get(f"{env_prefix}_EMAIL", "").strip()
    password = os.environ.get(f"{env_prefix}_PASSWORD", "").strip()
    phone = os.environ.get(f"{env_prefix}_PHONE", "").strip()
    return email, password, phone
