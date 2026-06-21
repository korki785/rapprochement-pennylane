#!/usr/bin/env python3
"""Phase A — Génère la todo-list des transactions sans facture rapprochée.

Usage :
    python3 scripts/backlog_report.py [--all] [--since YYYY-MM-DD] [--out reports/backlog.md]

Par défaut : ne considère que les dépenses (sorties d'argent). --all inclut tout.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Permet `python3 scripts/backlog_report.py` sans installation (ajoute src/ au path).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recon.config import load_settings              # noqa: E402
from recon.pennylane_client import PennylaneClient   # noqa: E402
from recon.report import build_report, render_console, render_markdown  # noqa: E402
from recon.transactions import find_unmatched        # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Todo-list des transactions sans facture.")
    parser.add_argument("--all", action="store_true",
                        help="Inclure encaissements et virements (pas seulement les dépenses).")
    parser.add_argument("--since", help="Ne considérer que les transactions depuis cette date (YYYY-MM-DD).")
    parser.add_argument("--out", help="Écrire le rapport Markdown dans ce fichier.")
    args = parser.parse_args()

    settings = load_settings()
    client = PennylaneClient(settings.api_token, settings.base_url)
    since = args.since or settings.backlog_since

    print("Lecture des transactions et vérification des rapprochements…", file=sys.stderr)
    unmatched = find_unmatched(client, since=since, only_expenses=not args.all)
    lines = build_report(unmatched, settings.supplier_rules)

    print(render_console(lines))

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_markdown(lines), encoding="utf-8")
        print(f"\nRapport détaillé écrit dans : {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
