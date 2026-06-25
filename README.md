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
# UBEREATS_GMAIL=naelkodmani@gmail.com
# UBEREATS_GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx   ← myaccount.google.com/security → Mots de passe des applications
# QONTO_ORGANIZATION_SLUG=...
# QONTO_SECRET_KEY=...

# Flux complet
python3 scripts/run_ubereats.py --since 2026-04-01
```

### Automatisation hebdomadaire (launchd)

Le script tourne automatiquement chaque **lundi à 9h** via launchd macOS :

```bash
# Activer (une seule fois)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.maisondarwish.ubereats-recon.plist

# Vérifier
launchctl list | grep ubereats

# Log de chaque exécution
cat reports/ubereats/weekly_run.log
```

Le plist est dans `~/Library/LaunchAgents/com.maisondarwish.ubereats-recon.plist`.

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

## Flux 3 — Factures fournisseurs (Google Drive)

Les factures d'achat sont stockées dans le Drive « Factures fournisseurs ». Ce flux les scanne, trouve la transaction Qonto correspondante (carte EUR, virement perso « Nael Darwish », ou paiement en devise via `local_amount`/`local_currency`), et attache le PDF à la transaction.

### Workflow (orchestré par `scripts/run_fournisseurs.py`)

1. **Drive** — télécharge les PDF non encore traités (`processed.json`) → `reports/fournisseurs/input/`
2. **Qonto** — `QontoClient.fetch_all_debits` → `qonto_debits.json` (avec `local_amount`/`local_currency`)
3. **Rapprocher** — `reconcile_fournisseurs.py` : OCR (reçus scannés) + 4 stratégies → `matches.json` + rapport `.md`
   - **EUR carte** : montant ± 0,01 €, date ± 7 j
   - **EUR virement perso** : `operation_type=transfer` + libellé « Nael »/« Darwish »
   - **Devise carte Qonto** : pas de conversion — match sur `local_currency` + `local_amount` (ex. facture USD ↔ tx Qonto `local_currency=USD`)
   - **Devise payée perso → remboursée EUR** (passe 3, `foreign_perso`) : reçu en devise (ex. hôtel NOK) payé carte perso, remboursé par virement EUR. Le virement ne peut précéder la dépense ; on prend le plus proche (même jour = signal fort) dont le taux FX implicite est plausible. Marqué `warn` → **confirmation manuelle** (non auto-attaché).

Montant retenu = **plus grand montant du reçu** (= total débité, pourboire inclus). OCR via Apple Vision (`scripts/ocr/`), cache dans `reports/fournisseurs/.ocr_cache/`. Seuls les rapprochements `exact` sont attachés automatiquement.
4. **Attacher** — `upload_attachment` (skip si la transaction a déjà une PJ)
5. **Marquer** — inscrit les Drive file IDs rapprochés dans `processed.json`

### Setup (une seule fois)

```bash
pip3 install google-api-python-client google-auth-oauthlib google-auth-httplib2
brew install poppler   # pdftotext (déjà installé pour UberEats)

# Google Cloud Console : activer Drive API → Credentials → OAuth 2.0 Desktop App
# Télécharger le JSON → credentials.json à la racine du projet
# .env : DRIVE_FOLDER_NAME=Factures fournisseurs

# Auth initiale (ouvre le navigateur une fois)
python3 scripts/run_fournisseurs.py --auth --dry-run
```

### Lancer

```bash
python3 scripts/run_fournisseurs.py --since 2026-04-01 --dry-run   # test, n'attache rien
python3 scripts/run_fournisseurs.py --since 2026-04-01             # flux complet
```

### Automatisation hebdomadaire (launchd)

Tourne chaque **mercredi à 9h**. `--since` fixe au 01/04/2026 : une facture ajoutée tardivement au Drive est quand même rapprochée (`processed.json` évite le doublon).

```bash
cp scripts/com.maisondarwish.fournisseurs-recon.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.maisondarwish.fournisseurs-recon.plist
launchctl list | grep fournisseurs
```

### Scripts

| Script | Rôle |
|--------|------|
| `src/recon/drive_client.py` | Client Google Drive (OAuth, liste + télécharge PDF) |
| `scripts/reconcile_fournisseurs.py` | Parse PDF, rapproche (3 stratégies) |
| `scripts/run_fournisseurs.py` | Orchestrateur 5 étapes |

### Notes

- Pas de fabrication : seuls les vrais PDF du Drive sont attachés.
- Démarre au **01/04/2026** : rien avant cette date n'est scanné.

---

## Notes générales

- Pas de fabrication : un justificatif n'est attaché que si un **vrai reçu** existe.
- Les justificatifs Qonto ne sont pas des copies probantes (`probative_attachment: unavailable`) ; les originaux restent dans Gmail.
