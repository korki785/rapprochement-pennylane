# CLAUDE.md — notes pour agents

Automatisation de rapprochement : trouver le justificatif (reçu/facture) de chaque dépense
Qonto et l'**attacher à la transaction** via l'API Qonto. Codebase **en français** (commentaires,
docs, libellés). Python 3, **stdlib uniquement** dans le cœur (`urllib`, `json`, `csv`, `re`).

## Les 6 flux

| Flux | Source | Orchestrateur |
|------|--------|---------------|
| 1 — Dépenses USD carte | Reçus Gmail (Square/Toast) → PDF générés | `scripts/gen_justificatifs_*.py` |
| 2 — Remboursements UberEats | Gmail + portail (Playwright) | `scripts/run_ubereats.py` |
| 3 — Factures fournisseurs | Google Drive (OCR Apple Vision) | `scripts/run_fournisseurs.py` |
| 4 — Factures & reçus email | Gmail (PDF joints + reçus HTML→PDF Chrome) | `scripts/run_saas.py` |
| 5 — Portails vendeurs | Scraping Playwright (Wix/Notion/OpenAI…) | `scripts/run_portals.py` |
| 6 — Piloté par la transaction | Tx Qonto sans PJ → Gmail par nom+montant (sans liste blanche) | `scripts/reconcile_qonto.py` |

Détails d'install/lancement : **README.md**. Ce fichier = conventions + pièges.

## Règles à NE PAS violer

- **Toujours `--dry-run` d'abord.** Attacher écrit dans la compta Qonto (action sortante). On
  relit `reports/<flux>/reconciliation_<date>.md` avant un run réel.
- **Pas de fabrication.** Un justificatif n'est attaché que si un **vrai** reçu/facture existe.
- **Idempotent.** `upload_attachment` est sauté si la transaction a déjà une PJ
  (`get_transaction_attachments`). Ne jamais court-circuiter ce garde-fou → risque de doublon.
  Conséquence utile : **auto-réparation** — si un justificatif est supprimé, un re-run le recolle.
- **Tout démarre au 01/04/2026** (`--since`). Rien avant n'est traité.
- `reports/` et `.env` sont **gitignored** (PDF régénérables + secrets). Ne jamais les committer.

## Rapprochement (flux 3 & 4) — pièges

- **Montant = Total TTC / Net à payer**, JAMAIS le plus grand nombre du document. Le max attrape
  prix unitaire, sous-totaux, mentions légales (« indemnité de 40 € »). Gérer les labels pointés
  (`Total T.T.C`) et l'écart label→montant large en `-layout` (`_LABEL_GAP`).
- **Croiser nom marchand ↔ libellé Qonto.** Auto-attache (`exact`) seulement si un alias du
  vendeur est dans le libellé Qonto — sinon le bon montant sur un autre marchand (ex. 40 € → KLM)
  serait faussement rapproché. Sans lien de nom mais facture « Maison Darwish » → `warn` (manuel).
- **Devise** : comparer au montant EUR (`amount`) ET au montant d'origine (`local_amount` +
  `local_currency`). Qonto stocke les deux ; ne pas convertir soi-même.
- **Rendu HTML→PDF (Chrome headless)** échoue silencieusement en rafale → toujours **retry + vérif
  taille** du PDF.
- **Settlement Qonto** : une transaction carte reste `pending` quelques heures ; l'API ne renvoie
  que les `completed`. Une facture sans débit correspondant aujourd'hui peut matcher demain → les
  non-rapprochés restent en attente et sont re-tentés (ne pas les jeter).

## Idempotence / état (flux 4)

`reports/saas/manifest.json` = en attente (clé Message-ID), `processed.json` = déjà attachés
(retirés du manifest). Le balayage saute `processed ∪ manifest` AVANT de télécharger.

## Pratique

- Python : `/Users/naeldarwish/Library/Python/3.9/bin/python3` (repli `/usr/bin/python3`).
- Modules réutilisables : `recon.qonto_client` (API Qonto, curl + retries), `recon.normalize`
  (`normalize_label`/`ratio`), `recon.config` (`load_dotenv`), `recon.mailer` (SMTP Gmail).
- Pour ajouter un fournisseur au flux 4 : une ligne dans `src/recon/saas_senders.csv`
  (`name,domains,label_aliases,trusted,note`).
- Justificatifs Qonto non probants (`probative_attachment: unavailable`) — originaux dans Gmail.
