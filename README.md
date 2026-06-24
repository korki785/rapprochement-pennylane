# Rapprochement justificatifs → Qonto

Deux flux automatisés de rapprochement : dépenses **USD** par carte (reçus Gmail pro) et remboursements **UberEats** (reçus Gmail perso → virements Qonto).

---

## Flux 1 — Dépenses USD (carte pro)

Automatisation : pour chaque dépense par carte en **USD**, retrouver le **reçu reçu par
email**, générer un **justificatif PDF**, et l'**attacher à la transaction Qonto**
correspondante.

### Workflow

1. **Lister** les transactions Qonto en devise étrangère (`local_currency == "USD"`)
   sans justificatif (via l'API/MCP Qonto).
2. **Retrouver le reçu** dans Gmail (reçu présent dans le corps de l'email) :
   - **Square** — `messenger@messaging.squareup.com` (libellés `SQ *…`)
   - **Toast** — `no-reply@toasttab.com` (libellés `TST*…`)
   - **Sunday** — `noreply-receipt@sundayapp.io`
3. **Apparier** reçu ↔ transaction par marchand + montant (USD).
4. **Générer** le justificatif PDF (scripts ci-dessous → dossier `reports/`).
5. **Attacher** le PDF à la transaction Qonto (flux d'upload des pièces jointes Qonto MCP :
   `request_attachment_upload` → PUT → `upload_attachment` cible `transaction`).

### Scripts

| Script | Justificatifs générés |
|--------|------------------------|
| `scripts/gen_justificatifs_usd_apr.py` | Crema Brentwood, Hands and Rose (avril 2026) |
| `scripts/gen_justificatifs_toast_may.py` | La La Land, Thai Thai (mai 2026) |

```bash
python3 scripts/gen_justificatifs_usd_apr.py
python3 scripts/gen_justificatifs_toast_may.py
```

Les PDF sont écrits dans `reports/` (ignoré par git). Rendu HTML → PDF via Chrome headless :

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --disable-gpu --no-pdf-header-footer \
  --print-to-pdf=reports/receipts/<nom>.pdf "file://$PWD/reports/receipts/<nom>.html"
```

---

## Flux 2 — Remboursements UberEats

Nael paie UberEats avec carte perso → l'entreprise rembourse par virement Qonto à « Nael Darwish ». Ce flux associe chaque reçu UberEats à son virement de remboursement et attache le PDF à la transaction Qonto.

### Workflow

1. **Télécharger les reçus PDF** depuis la boîte Gmail personnelle : UberEats envoie automatiquement un email après chaque commande contenant un lien « Téléchargez ce PDF ». Playwright se connecte à Gmail perso, trouve les emails UberEats depuis la date cible, et télécharge les PDFs. *(script à venir : `scripts/fetch_ubereats_gmail.py`)*
2. **Récupérer les virements Qonto** (`scripts/run_ubereats.py` via `src/recon/qonto_client.py`) — filtre les débits Qonto dont le libellé contient « Nael Darwish » depuis la date cible → `reports/ubereats/qonto_transfers.json`.
3. **Rapprocher** (`scripts/reconcile_ubereats.py`) — associe chaque PDF (montant + date extraits par `pdftotext`) au virement de même montant à ±7 jours → `matches.json`.
4. **Attacher** les PDFs aux transactions Qonto (`scripts/run_ubereats.py` étape 4).

### Lancer

```bash
# Prérequis (une seule fois)
pip3 install playwright && python3 -m playwright install chromium
brew install poppler   # pour pdftotext

# Remplir .env (voir .env.example) :
# UBEREATS_GMAIL=...  UBEREATS_GMAIL_PASSWORD=...
# QONTO_ORGANIZATION_SLUG=...  QONTO_SECRET_KEY=...

# Flux complet
python3 scripts/run_ubereats.py --headed --since 2026-04-01
```

Premier lancement : `--headed` requis pour la connexion manuelle à Gmail perso (session sauvegardée ensuite dans `.ubereats_gmail_session.json`).

### Scripts

| Script | Rôle |
|--------|------|
| `scripts/fetch_ubereats_gmail.py` | Playwright → Gmail perso → télécharge PDFs reçus UberEats *(à venir)* |
| `scripts/reconcile_ubereats.py` | Parse PDFs, rapproche avec virements Qonto |
| `scripts/run_ubereats.py` | Orchestrateur bout-en-bout |
| `src/recon/qonto_client.py` | Client REST Qonto (curl, contourne Cloudflare) |

### Notes

- Les reçus PDF UberEats sont les originaux envoyés par email — pas de fabrication.
- Qonto `probative_attachment: unavailable` : les originaux restent dans Gmail.
- `reports/ubereats/` est gitignore (régénérable).

---

## Notes générales

- Pas de fabrication : un justificatif n'est attaché que si un **vrai reçu** existe.
- Les justificatifs Qonto ne sont pas des copies probantes (`probative_attachment: unavailable`) ; les originaux restent dans Gmail.
