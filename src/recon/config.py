"""Configuration : chargement du .env, URL de base, et mapping fournisseur -> source.

Aucune dépendance externe : parseur .env minimal maison.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

# Racine du projet = deux niveaux au-dessus de ce fichier (src/recon/config.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE_URL = "https://app.pennylane.com/api/external/v2"
SUPPLIERS_MAP_PATH = Path(__file__).resolve().parent / "suppliers_map.csv"


def load_dotenv(path: Optional[Path] = None) -> None:
    """Charge les variables d'un fichier .env dans os.environ (sans écraser l'existant)."""
    env_path = path or (PROJECT_ROOT / ".env")
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class SupplierRule:
    """Une règle du suppliers_map.csv : où récupérer la facture d'un fournisseur."""

    pattern: str   # nom normalisé attendu dans le libellé
    source: str    # email | portal | manual
    url: str
    note: str


@dataclass
class Settings:
    api_token: str
    base_url: str
    backlog_since: Optional[str]  # ISO YYYY-MM-DD ou None
    supplier_rules: List[SupplierRule]


def load_settings() -> Settings:
    """Construit les Settings à partir du .env et du suppliers_map.csv."""
    load_dotenv()
    token = os.environ.get("PENNYLANE_API_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "PENNYLANE_API_TOKEN manquant. Crée un fichier .env à la racine du projet "
            "avec la ligne : PENNYLANE_API_TOKEN=ton_token"
        )
    return Settings(
        api_token=token,
        base_url=os.environ.get("PENNYLANE_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        backlog_since=os.environ.get("BACKLOG_SINCE") or None,
        supplier_rules=load_supplier_rules(),
    )


def load_supplier_rules(path: Optional[Path] = None) -> List[SupplierRule]:
    """Charge les règles fournisseur. Tolère l'absence du fichier (liste vide)."""
    csv_path = path or SUPPLIERS_MAP_PATH
    if not csv_path.exists():
        return []
    rules: List[SupplierRule] = []
    with csv_path.open(encoding="utf-8") as fh:
        # Ignorer les lignes de commentaire commençant par '#'.
        rows = (line for line in fh if not line.lstrip().startswith("#"))
        for row in csv.DictReader(rows):
            if not row.get("pattern"):
                continue
            rules.append(
                SupplierRule(
                    pattern=row["pattern"].strip().upper(),
                    source=(row.get("source") or "").strip() or "manual",
                    url=(row.get("url") or "").strip(),
                    note=(row.get("note") or "").strip(),
                )
            )
    return rules
