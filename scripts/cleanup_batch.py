#!/usr/bin/env python3
"""Nettoyage : supprime les factures fournisseurs créées dans une fenêtre horaire.

Sert à retirer un lot importé par erreur (ex. run email trop large), SANS toucher
aux imports légitimes (Drive) faits à d'autres moments.

DRY-RUN PAR DÉFAUT : sans --delete, le script ne fait que LISTER ce qui serait supprimé.

Usage :
    # 1) Aperçu (ne supprime rien) :
    python3 scripts/cleanup_batch.py --since 2026-06-13T15:46:00 --until 2026-06-13T15:49:00
    # 2) Suppression réelle (après vérification de l'aperçu) :
    python3 scripts/cleanup_batch.py --since 2026-06-13T15:46:00 --until 2026-06-13T15:49:00 --delete
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recon.config import load_settings              # noqa: E402
from recon.pennylane_client import PennylaneClient   # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Supprime les factures fournisseurs d'une fenêtre horaire.")
    parser.add_argument("--since", required=True, help="Début de fenêtre, ISO (ex. 2026-06-13T15:46:00).")
    parser.add_argument("--until", required=True, help="Fin de fenêtre exclusive, ISO (ex. 2026-06-13T15:49:00).")
    parser.add_argument("--delete", action="store_true", help="Supprime réellement (sinon : aperçu seul).")
    args = parser.parse_args()

    settings = load_settings()
    client = PennylaneClient(settings.api_token, settings.base_url)

    print("Lecture des factures fournisseurs…", file=sys.stderr)
    targets = []
    for inv in client.iter_supplier_invoices():
        created = (inv.get("created_at") or "").replace("Z", "")
        if args.since <= created < args.until:
            targets.append(inv)

    if not targets:
        print("Aucune facture dans cette fenêtre. Rien à faire.")
        return 0

    print(f"\n{len(targets)} facture(s) dans la fenêtre [{args.since} → {args.until}[ :\n")
    for inv in targets:
        print(f"  {inv.get('created_at')}  id={inv.get('id')}  {inv.get('filename','')[:70]}")

    if not args.delete:
        print(f"\n[APERÇU] Rien supprimé. Relance avec --delete pour supprimer ces {len(targets)}.")
        return 0

    print(f"\nSuppression de {len(targets)} facture(s)…")
    ok = 0
    for inv in targets:
        try:
            client.delete_supplier_invoice(inv["id"])
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ÉCHEC id={inv.get('id')} : {e}", file=sys.stderr)
    print(f"Supprimées : {ok}/{len(targets)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
