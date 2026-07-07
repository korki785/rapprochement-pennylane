# Rapprochement justificatifs → Qonto

Sept flux automatisés de rapprochement justificatif → transaction Qonto. Les flux 1 à 6 couvrent les **dépenses** (attacher un reçu/facture à un **débit**) ; le flux 7 couvre les **recettes** (attacher la facture client émise à un **virement créditeur**) :

1. **Dépenses USD** par carte (reçus Gmail pro → justificatif PDF).
2. **UberEats** (reçus Gmail perso → virements de remboursement Qonto) — *continu, ≈ à chaque mail*.
3. **Factures fournisseurs** (Google Drive, OCR) → carte/virement/devise — *continu, ≈ à chaque upload, + email hebdo des non-rapprochés*.
4. **Factures & reçus par email** (SaaS : Anthropic, Aircall, Square, etc.) — *continu, ≈ à chaque mail*.
5. **Portails vendeurs** (Wix, Notion, OpenAI, Hunter, etc.) — *continu, ≈ toutes les 15 min*.
6. **Piloté par la transaction** (part d'une tx Qonto sans PJ → cherche le reçu dans Gmail par *nom du libellé + montant* → attache, **sans liste blanche de fournisseurs**) — *≈ horaire*.
7. **Recettes** (facture client payée → attache le PDF de la facture au virement créditeur) — *continu, ≈ toutes les 15 min*.

Les flux 2, 3, 4, 5 et 7 tournent en continu via des pollers launchd (toutes les 15 min) ; le flux 3 envoie un rapport email chaque lundi des justificatifs non rapprochés.

Un **cockpit web local** (`scripts/dashboard.py`, http://127.0.0.1:8787) donne la vue d'ensemble : santé des sessions portails, compteurs par flux, transactions Qonto sans justificatif en direct, avec **Retenter** (recherche multi-source à la demande), **Ignorer** et **Ajouter un abonnement**. Voir la section [Cockpit](#cockpit--tableau-de-bord-local).

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

1. **Télécharger les reçus PDF** depuis la boîte Gmail personnelle : UberEats envoie automatiquement un email après chaque commande contenant un lien « Téléchargez ce PDF ». Playwright se connecte à Gmail perso, trouve les emails UberEats depuis la date cible, et télécharge les PDFs (`scripts/fetch_ubereats_gmail.py`).
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

### Automatisation continue (≈ à chaque mail UberEats)

Un poller launchd tourne **toutes les 15 min** (`com.maisondarwish.ubereats-watch.plist`, `StartInterval=900`) :
- **Nouveau mail UberEats détecté** (pré-check IMAP léger) → télécharge le reçu (Playwright) + rapproche + attache.
- **Sinon** → retry léger reconcile+attach (rattrape les virements de remboursement arrivés depuis).

Note métier : le reçu arrive par mail tout de suite, mais le virement de remboursement Qonto (« Nael Darwish ») arrive plus tard → le reçu est téléchargé vite, le rapprochement se fait dès que le virement apparaît.

```bash
# Activer (une seule fois)
cp scripts/com.maisondarwish.ubereats-watch.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.maisondarwish.ubereats-watch.plist

# Forcer un passage immédiat (sans attendre 15 min)
launchctl kickstart -k gui/$(id -u)/com.maisondarwish.ubereats-watch

# Vérifier / suivre
launchctl list | grep ubereats        # "0" = dernier passage OK
tail -f reports/ubereats/watch.log
```

Prérequis : `.ubereats_session.json` (session Playwright UberEats, créée au 1er `--headed`) + `UBEREATS_GMAIL` / `UBEREATS_GMAIL_APP_PASSWORD` (app password 16 car. du Gmail perso).

### Scripts

| Script | Rôle |
|--------|------|
| `scripts/fetch_ubereats_gmail.py` | IMAP Gmail perso + Playwright → télécharge les reçus PDF UberEats |
| `scripts/ubereats_has_new_email.py` | Pré-check IMAP léger : y a-t-il un nouveau mail UberEats ? (sans Playwright) |
| `scripts/reconcile_ubereats.py` | Parse PDFs, rapproche avec virements Qonto |
| `scripts/run_ubereats.py` | Orchestrateur (`--skip-fetch`, `--fetch-since`, attach idempotent) |
| `scripts/run_ubereats_watch.sh` | Wrapper du poller 15 min |
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

### Automatisation continue + email hebdo (launchd)

Deux jobs launchd :

**1. Traitement continu (≈ à chaque upload Drive)** — poller toutes les 15 min (`com.maisondarwish.fournisseurs-watch.plist`, `StartInterval=900`) : ne traite que les nouveaux PDF Drive (sortie rapide sinon), `--since` fixe au 01/04/2026 (`processed.json` évite les doublons).

**2. Email lundi 9h** (`com.maisondarwish.fournisseurs-email.plist`, Weekday=1) : `email_weekly.sh` (dédup hebdo `.derniere_semaine_email`) lance un run forcé puis `email_unreconciled.py` → envoie à `EMAIL_TO` (`hello@maisondarwish.com`) la liste des justificatifs **non rapprochés** uploadés les 7 derniers jours.

```bash
# Activer les deux (une seule fois)
cp scripts/com.maisondarwish.fournisseurs-watch.plist ~/Library/LaunchAgents/
cp scripts/com.maisondarwish.fournisseurs-email.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.maisondarwish.fournisseurs-watch.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.maisondarwish.fournisseurs-email.plist

# Tester l'email à blanc / en vrai
python3 scripts/email_unreconciled.py --all --dry-run
python3 scripts/email_unreconciled.py --all
```

Config email dans `.env` : `GMAIL_USER`, `GMAIL_APP_PASSWORD` (app password 16 car.), `EMAIL_TO`. Envoi via SMTP_SSL Gmail (`src/recon/mailer.py`).

### Scripts

| Script | Rôle |
|--------|------|
| `src/recon/drive_client.py` | Client Google Drive (OAuth, liste + télécharge PDF, `createdTime`) |
| `scripts/reconcile_fournisseurs.py` | OCR + parse PDF, rapproche (4 stratégies) |
| `scripts/run_fournisseurs.py` | Orchestrateur (`--force`, écrit `unreconciled.json`) |
| `scripts/run_fournisseurs_watch.sh` | Wrapper du poller 15 min |
| `scripts/email_unreconciled.py` | Digest email des non-rapprochés de la semaine |
| `scripts/email_weekly.sh` | Wrapper email lundi (dédup hebdo) |
| `src/recon/mailer.py` | Envoi email SMTP Gmail |

### Notes

- Pas de fabrication : seuls les vrais PDF du Drive sont attachés.
- **Upload AVANT la transaction** : un justificatif sans transaction Qonto est gardé « en attente » et associé automatiquement quand la transaction arrive (re-tenté à chaque passage).
- **Montant = Total payé** : gère les factures à remise (essaie plusieurs candidats — total labellisé puis max — au lieu du plus grand nombre brut).
- Démarre au **01/04/2026** : rien avant cette date n'est scanné.

---

## Flux 4 — Factures & reçus par email (saas)

Balaye **tous les emails avec un justificatif** (Gmail `hello@maisondarwish.com`) depuis le 01/04/2026, sans liste blanche d'expéditeurs, et attache le PDF à la transaction Qonto correspondante. Trois sources :

- **Factures PDF jointes** — abonnements (Anthropic/Claude, Aircall, Alan, Google Workspace, Kandbaz) et fournisseurs one-off (Agence Montparnasse, transport…).
- **Reçus HTML** (Square, Sunday, Toast, Clover, SumUp) — pas de PDF joint : le corps HTML est rendu en PDF via **Chrome headless** (`--print-to-pdf`, avec retry).

### Workflow (orchestré par `scripts/run_saas.py`)

1. **Gmail** — `X-GM-RAW "has:attachment filename:pdf"` (factures) + recherche des expéditeurs de reçus (HTML). Télécharge / rend → `reports/saas/input/`. Balayage **incrémental** (en-têtes d'abord, saute `processed`/`manifest` sans télécharger). Identité par **Message-ID**.
2. **Qonto** — `fetch_all_debits` → `qonto_debits.json`.
3. **Rapprocher** — `reconcile_saas.py` :
   - montant ± 0,01 € sur `amount` (EUR) **OU** `local_amount` (devise, ex. Anthropic facturé en USD)
   - date dans une fenêtre (~15 j ; prélèvements/CB postérieurs à la facture)
   - **garde-fou** : le PDF doit ressembler à une facture (`looks_like_invoice`)
   - `confidence="exact"` (auto-attaché) **uniquement si un alias marchand ∈ libellé Qonto** (croisement nom ↔ libellé — évite le bon montant sur le mauvais marchand). L'alias vient des aliases connus (`saas_senders.csv`), du nom d'expéditeur, ou du marchand lu dans le reçu.
   - facture « Maison Darwish » sans lien de nom → `warn` (listée, jamais auto-attachée)
   - Montant lu sur la facture = **Total TTC / Net à payer** (jamais le plus grand nombre — évite prix unitaire / mentions légales).
4. **Attacher** — `upload_attachment` (skip si la transaction a déjà une PJ → **idempotent**). Une même facture + son reçu de paiement sont fusionnés sur la même transaction.
5. **Marquer** — Message-IDs attachés dans `processed.json` (retirés du `manifest.json` ; les non-rapprochés sont re-tentés).

**Auto-réparation** : si un justificatif est supprimé sur Qonto, un re-run le retrouve dans l'email et le recolle — sans toucher au reste.

### Setup (une seule fois)

```bash
brew install poppler   # pdftotext (déjà installé pour les autres flux)
# Chrome requis pour les reçus HTML : /Applications/Google Chrome.app

# .env : compte Gmail qui REÇOIT les factures (app password 16 car.)
# SAAS_GMAIL=hello@maisondarwish.com
# SAAS_GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx   ← myaccount.google.com/security → Mots de passe des applications
# (IMAP doit être activé : Gmail → Paramètres → Transfert et POP/IMAP)
```

### Lancer

```bash
python3 scripts/run_saas.py --since 2026-04-01 --dry-run   # test : parse + rapproche, n'attache rien
python3 scripts/run_saas.py --since 2026-04-01             # flux complet
python3 scripts/run_saas.py --since 2026-04-01 --skip-fetch # re-tente reconcile+attach sans re-télécharger
```

Revoir `reports/saas/reconciliation_<date>.md` (surtout les `warn`) avant un run réel.

### Automatisation continue (launchd)

Poller toutes les 15 min (`com.maisondarwish.saas-watch.plist`, `StartInterval=900`) : si un nouvel email justificatif est détecté (`saas_has_new_email.py`, pré-check IMAP léger) → run complet ; sinon → `--skip-fetch` (rattrape les débits réglés après réception de la facture).

```bash
cp scripts/com.maisondarwish.saas-watch.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.maisondarwish.saas-watch.plist
# suivre : tail -f reports/saas/watch.log
```

### Scripts

| Script | Rôle |
|--------|------|
| `src/recon/saas.py` | Parse PDF (Total TTC), reçus HTML, rendu Chrome, détection facture / société / vendeur |
| `src/recon/saas_senders.csv` | Fournisseurs connus : domaines + alias de libellé Qonto |
| `scripts/fetch_saas_gmail.py` | Balayage Gmail (PDF + reçus HTML), parse, dédup Message-ID |
| `scripts/reconcile_saas.py` | Rapproche (montant + croisement nom + garde-fous) |
| `scripts/run_saas.py` | Orchestrateur (`--since`, `--skip-fetch`, `--dry-run`) |
| `scripts/saas_has_new_email.py` | Pré-check IMAP léger pour le poller |
| `scripts/run_saas_watch.sh` | Wrapper du poller 15 min |

### Notes

- Card-SaaS **sans** facture email (Notion, OpenAI, Hostinger, Wix…) : factures sur portail uniquement → hors périmètre (sprint séparé).
- Balayage = **INBOX** seulement (un justificatif présent uniquement dans « Envoyés » via transfert auto n'est pas lu).
- **Formats reconnus** : montants `1 156,41` / `1,156.41` (US) / `1.156,41` ; dates FR + EN (`25 April 2026`).
- **Justificatif auto-envoyé** : le marchand est lu DANS le PDF (pas seulement l'expéditeur) → un reçu que tu te transfères à toi-même se rapproche quand même (montant + date + marchand croisé au libellé Qonto).
- Démarre au **01/04/2026**.

---

## Flux 5 — Portails vendeurs (Playwright)

Détecte les transactions Qonto sans PJ → scrape les portails de facturation (Wix, Notion, OpenAI, Hunter…) via Playwright → télécharge le PDF → attache à Qonto.

### Vendeurs supportés

| Vendeur | Endpoint | Login | Statut / Notes |
|---------|----------|-------|----------------|
| Wix | manage.wix.com/account/billing-history | email/password | ✅ Historique de facturation (data-hook invoice-number, download natif) |
| Notion | app.notion.com → Paramètres → Facturation | email/SSO | ✅ Modal Settings réelle, View invoice → render PDF |
| OpenAI/ChatGPT | chatgpt.com → Paramètres → Facturation | (real Chrome CDP) | ✅ Contourne Cloudflare via CDP port 9222 → Stripe |
| Uber Rides | riders.uber.com/trips | email/Google (= UberEats) | ✅ Course → Details → « Download Invoice ». Date sans année (infère ≤ today) |
| Hunter | hunter.io/**users/sign_in** → **/subscriptions** | (real Chrome CDP) | ✅ Cloudflare Turnstile → CDP ; factures **Stripe** (« Télécharger la facture »). Anciennes URLs `/sign-in`, `/account/billing` = **404** |
| Kandbaz | my.kandbaz.com → **/mes-factures** | email/password + **2FA email** | ✅ login auto, **code 2FA alphanum lu automatiquement dans Gmail** (hello@) ; factures = **mêmes que par email** (fallback) ; DL PDF bloqué par pop-up conformité LCB-FT |
| Hostinger | hpanel.hostinger.com/billing | email/password | ⏳ Abandonné (facturé par email) |
| QR-Code-Generator | qr-code-generator.com/account/ | email/password | ⏳ Abandonné |
| Bouygues | bouyguestelecom.fr/mon-compte | email/password | ⏳ À faire (portail FR, timeouts longs) |
| Airbnb | airbnb.com/trips | email/password | ✅ CDP printToPDF (page réservation, code = libellé Qonto) |
| Turo | turo.com/us/en/trips/ | email/password | ⏳ Abandonné (Cloudflare) |
| ~~Bolt~~ | — | — | ❌ PAS de portail web (mobile-only). Reçus HTML par email → **flux SAAS** (`bolt.eu`) |

> **Uber « PENDING »** : un débit `UBR* PENDING.UBER.COM` est une pré-autorisation. Tant qu'Uber n'a pas
> finalisé la course, aucune facture n'existe (ni portail, ni email) → non rapprochable jusqu'à finalisation.
> Le poller la rattrape automatiquement quand le reçu apparaît.

### Workflow (orchestré par `scripts/run_portals.py`)

1. **Détection** — `portals.detect_unreconciled()` → transactions Qonto `attachment_required=true`, `attachment_ids=[]`
2. **Scraping** — par vendeur :
   - Playwright headless + session persistence (`.{vendor}_session.json`)
   - Auto-detect login (scrute `_is_logged_in()`, pas de stdin)
   - Navigue → facturation → boucle factures → télécharge PDF
   - Sauvegarde session pour rapprochements futurs
3. **Rapprochement** — `reconcile_portals.py` :
   - Montant ± 0,01 EUR (ou `local_amount` devise)
   - Date ± 10 jours
   - Alias marchand ∈ libellé Qonto → `confidence=exact` (auto-attache)
4. **Attachement** — `upload_attachment` (skip si PJ déjà présente)
5. **Marquer** — `processed.json` (idempotent)

### Setup (une seule fois)

```bash
# .env : credentials par vendeur
OPENAI_EMAIL=...  OPENAI_PASSWORD=...
NOTION_EMAIL=...  NOTION_PASSWORD=...
WIX_EMAIL=...     WIX_PASSWORD=...
HUNTER_EMAIL=...  HUNTER_PASSWORD=...
HOSTINGER_EMAIL=... HOSTINGER_PASSWORD=...
BOLT_PHONE=+33... BOLT_PASSWORD=...
# ... etc

# Init sessions (headed, supervision 2FA/OTP)
python3 scripts/portals/fetch_openai.py --init-session
python3 scripts/portals/fetch_notion.py --init-session
python3 scripts/portals/fetch_wix.py --init-session
# ... repeat per vendor

# Cas special : OpenAI/ChatGPT (Cloudflare)
# Lancer Chrome debug AVANT les fetchers
open -na "Google Chrome" --args --remote-debugging-port=9222 "--remote-allow-origins=*" \
  --user-data-dir="$HOME/.chrome-recon-debug" https://chatgpt.com/
# Log in une seule fois dans cette fenêtre. Session persiste.
# La laissez vivant (launchd relance si mort).
```

### Lancer

```bash
python3 scripts/run_portals.py --since 2026-04-01 --dry-run   # test
python3 scripts/run_portals.py --since 2026-04-01             # flux complet
python3 scripts/run_portals.py --vendor openai --dry-run       # seul un vendeur
```

### Fallback manuel

Pour les portails CAPTCHA-bloqués (Cloudflare) ou trop instables :

1. Téléchargez le PDF à la main → déposez dans `reports/portals/_drop/{vendor}/`
2. `python3 scripts/ingest_drop.py` → parse, rapproche, attache

### Automatisation launchd

Poller toutes les 15 min (`com.maisondarwish.portals-watch.plist`, `StartInterval=900`) :
- `portals_has_new.py` → pré-check léger (y a-t-il du nouveau ?)
- Si oui → `run_portals.py` complet

```bash
cp scripts/com.maisondarwish.portals-watch.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.maisondarwish.portals-watch.plist
```

### Scripts

| Script | Rôle |
|--------|------|
| `src/recon/portals.py` | Core (detect, match, attach) |
| `scripts/portals/fetch_*.py` (11 files) | Vendeur scrapers (Playwright) |
| `scripts/reconcile_portals.py` | Rapproche invoices vs Qonto |
| `scripts/run_portals.py` | Orchestrateur (`--since`, `--vendor`, `--dry-run`) |
| `scripts/portals_has_new.py` | Pré-check IMAP-léger pour le poller |
| `scripts/ingest_drop.py` | Fallback manuel (drop-folder) |
| `scripts/run_portals_watch.sh` | Wrapper du poller 15 min |
| `src/recon/portal_vendors.csv` | Mapping vendor → qonto_label, env_prefix, billing_url |

### Notes

- **Session Playwright** : stockée dans `.{vendor}_session.json` (gitignored). À renouveler si expirée (`--init-session` à nouveau).
- **OpenAI/ChatGPT & Hunter** : **Chrome via CDP** (port 9222) pour contourner Cloudflare. Nécessite `launch_chatgpt_chrome.sh` qui relance le Chrome dédié s'il meurt.
- ⚠️ **Panne CDP Chrome 149** (2026-07-02) : `connect_over_cdp` échoue `Browser.setDownloadBehavior: context management not supported` → la méthode « vrai Chrome debug » (OpenAI/Hunter) est **cassée** tant que Playwright/Chrome ne sont pas réalignés. Les portails **sans Cloudflare** (Kandbaz…) restent OK en Playwright headless normal.
- **Kandbaz — 2FA email auto** : le code (alphanumérique 6 car., « Votre code : XXXXXX », valable 10 min) est lu **automatiquement** dans Gmail hello@ (IMAP) et saisi dans les 6 cases → connexion sans intervention. Session réutilisée ensuite.
- **Pas de fabrication** : seuls les vrais PDFs depuis les portails sont attachés.
- **Démarrage** : 01/04/2026 (configurable).

---

## Flux 6 — Rapprochement piloté par la transaction

Sens **inverse** du flux SaaS (qui balaie les emails puis cherche un débit). Ici on part de
**chaque transaction Qonto sans justificatif** et on utilise SON PROPRE libellé (= le nom du
marchand) + son montant pour retrouver le reçu dans Gmail, le vérifier, puis l'attacher. **Aucun
fournisseur n'a besoin d'être pré-enregistré** (`saas_senders.csv`) : le nom vient du libellé.

### Workflow (orchestré par `scripts/reconcile_qonto.py`)

1. **Périmètre** — `QontoClient.fetch_unreconciled_expenses` (CB + prélèvements + virements perso,
   hors frais Qonto et internes « Maison Darwish »). Saut idempotent si la tx a déjà une PJ.
2. **Nom marchand du libellé** — `normalize.label_merchant_tokens` : nettoie le libellé et retire
   les préfixes d'intermédiaire de paiement (`SQ *`, `TST*`, `SUMUP *`, `UBR*`, `GC RE`…).
   Ex. `UBR* PENDING.UBER.COM` → `UBER` ; `GC RE AIRCALL` → `AIRCALL`.
3. **Recherche Gmail** — X-GM-RAW `(montant) ET (nom marchand) ET fenêtre ±10 j`, sur toutes les
   boîtes (hello/perso/parishouse). **Qonto exclu** (`-from:qonto.com`) : ses notifs « paiement
   effectué » contiennent nom+montant → faux positifs sinon.
4. **Vérification** (double garde anti « mauvais marchand ») — le PDF n'est retenu que si (i) le
   montant de la tx (EUR **ou** devise) est un **vrai total** dans le document (`_pdf_has_total`,
   OCR + codes ISO EUR/USD/GBP + total éloigné du montant), **ET** (ii) un jeton du nom marchand
   apparaît dans le PDF / l'expéditeur / le sujet. Reçus **HTML** sans PJ (Square/Sunday/Bolt) :
   corps rendu en PDF (`saas.render_html_to_pdf`) puis vérifié.
5. **Décision** — ≥2 emails distincts vérifiés → **skip** (on ne devine jamais). Sinon attache
   (`upload_attachment`, idempotent). État `reports/qonto/processed.json` (clé = tx id).

Complément (pas remplacement) du flux SaaS : tourne **avant** `run_saas.py` ; le repli reste la
voie « exact_unique » (unicité montant+date) de `reconcile_saas.py`.

### Lancer

```bash
python3 scripts/reconcile_qonto.py --since 2026-04-01 --dry-run   # propose tx→PDF, n'attache rien
python3 scripts/reconcile_qonto.py --since 2026-04-01             # attache
```

Revoir `reports/qonto/proposals_<date>.md` (dry-run) avant un run réel.

### Automatisation (launchd, ~horaire)

```bash
cp scripts/com.maisondarwish.qonto-watch.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.maisondarwish.qonto-watch.plist
# suivre : tail -f reports/qonto/watch.log
```

Cadence horaire (`StartInterval=3600`) : parcourt toutes les tx non rapprochées × IMAP (pas de
pré-check email bon marché, le déclencheur est côté transactions). Aussi appelé en best-effort
dans `weekly_recap.sh`.

### Fichiers

| Fichier | Rôle |
|--------|------|
| `src/recon/normalize.py` | `label_merchant_tokens` (nom marchand tiré du libellé) |
| `scripts/reconcile_qonto.py` | Orchestrateur (recherche Gmail nom+montant, vérif, attache) |
| `scripts/run_qonto_watch.sh` + `com.maisondarwish.qonto-watch.plist` | Poller horaire |
| `tests/test_qonto_label.py` | Tokens + requête + garde de vérification |

### Notes

- **Sans liste blanche** : le nom vient du libellé Qonto → un fournisseur jamais vu se rapproche
  quand même. La ligne par fournisseur dans `saas_senders.csv` devient **optionnelle**.
- **Skip SÛR** (jamais de mauvais attach) : montant qui n'est un total nulle part, aucun lien de
  nom, ou ≥2 candidats → laissé non rapproché (repris par les autres flux / manuel).

---

## Récap hebdo FIABLE (lundi 9h)

Mail récap des transactions **sans justificatif** — refondu pour être DIGNE DE CONFIANCE :
ne déclare jamais un item « non rapproché » sans avoir (1) vérifié le statut PJ **en direct
sur Qonto** et (2) **cherché activement** le reçu. Remplace l'ancien `email_weekly.sh` (qui
ne couvrait que les fournisseurs et se basait sur des snapshots/matching → pouvait être faux).

### Déroulé (`scripts/weekly_recap.sh`, lundi 9h)

1. **Best-effort** : relance les 4 flux → attache tout justificatif auto-trouvable.
2. **Audit LIVE** (`audit_unreconciled.py`) : `QontoClient.fetch_unreconciled_expenses` lit le
   vrai statut PJ (≠ snapshots périmés). **Tout l'exercice comptable en cours** (1er avril →
   1er avril), pas juste 7 j → rattrape les vieilles transactions orphelines. Périmètre : CB +
   prélèvements + virements de remboursement perso ; **exclut** « MAISON DARWISH » + frais Qonto.
3. **Vérif adversariale** : pour chaque tx sans PJ, cherche un justificatif au même montant
   (±0,02 sur EUR ou devise, date ±10 j) dans les PDF locaux (montant = vrai total près d'un
   mot-clé « Total/payé/facturé », pas un taux de TVA) + Gmail (hello@ / perso / parishouse).
   → **CONFIRMED** (rien trouvé) vs **SUSPECT** (justificatif existe mais pas attaché = bug).
4. **Doute** (autonome + repli) : suspects → escalade `claude -p` headless qui attache le vrai
   justificatif ou confirme/`needs_review` ; **jamais deviner**. Repli (claude indispo ou doute
   non levé) → récap **bloqué** + alerte courte « à vérifier » (jamais de récap faux).
5. **Envoi** (`email_recap.py`) : récap transaction-centric (CONFIRMED) ou alerte (needs_review).

### Garantie

Ne peut PAS dire « X non rapproché » si un justificatif existe vraiment — il cherche d'abord.

### Vérifier que ça marche

```bash
python3 scripts/selftest_recap.py                          # prouve la détection (16,75 → SUSPECT, faux → CONFIRMED)
python3 scripts/audit_unreconciled.py --days 7 --dry-run   # audit live (lecture seule)
DRY=1 bash scripts/weekly_recap.sh                         # chaîne complète à blanc
```

### Activer

```bash
launchctl bootout gui/$(id -u)/com.maisondarwish.fournisseurs-email 2>/dev/null
cp scripts/com.maisondarwish.weekly-recap.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.maisondarwish.weekly-recap.plist
```

> **Escalade autonome** : le plist doit avoir `claude` dans son `PATH` (sinon → repli alerte).
> Le binaire est en `~/.local/bin/claude` → la clé `PATH` du plist inclut `/Users/naeldarwish/.local/bin`.
> Vérifier : `launchctl list | grep weekly-recap`. Recharger après toute modif du plist (bootout + bootstrap).

### Fichiers

| Fichier | Rôle |
|--------|------|
| `scripts/audit_unreconciled.py` | Audit live Qonto + recherche adversariale du justificatif |
| `scripts/email_recap.py` | Mail récap (confirmés) ou alerte (à vérifier) |
| `scripts/weekly_recap.sh` | Orchestrateur lundi (flux → audit → escalade → mail), dédup, `DRY=1` |
| `scripts/recap_escalation_prompt.txt` | Consignes Claude headless en cas de doute |
| `scripts/selftest_recap.py` | Auto-test rejouable |
| `com.maisondarwish.weekly-recap.plist` | Programmation lundi 9h |

> `claude -p` utilise le CLI Claude Code déjà authentifié (coût tokens seulement les semaines
> avec un doute). Si l'auth expire → bascule auto en repli (bloquer + alerter).

---

## Flux 7 — Recettes (factures clients ↔ virements créditeurs)

Sens **inverse** de tous les autres flux : côté **crédit**. Quand un client paie une facture émise
par virement entrant, on attache le **PDF de la facture** à la transaction créditrice Qonto
(justificatif de la recette). N'auto-attache que ; **ne change pas** le statut « payé » de la facture.

### Fait métier — virements SWIFT

Les clients étrangers (ex. Allegria, US) paient par **virement SWIFT** → le montant crédité ≠ total
de la facture (frais bancaires ~40 € déduits, ou petit écart d'arrondi FX). Le rapprochement ne peut
donc pas être exact au centime sur tous les virements. Souvent `local_amount` == total exact (les frais
sont déduits de `amount`) → match exact via `local_amount` ; sinon petit arrondi FX (ex. 1818,00 reçu
vs 1817,94 facturé) → tier `swift`.

### Workflow (orchestré par `scripts/run_recettes.py`)

1. **Virements** — `QontoClient.fetch_all_credits` → `qonto_credits.json` (côté `side=credit`).
2. **Factures** — `QontoClient.list_client_invoices` → `client_invoices.json` (chaque facture porte
   son PDF dans `attachment_id`).
3. **Rapprocher** — `reconcile_recettes.py` :
   - **Nom client EXIGÉ** : un jeton du nom de la facture (normalisé, suffixe `INC/LLC/…` retiré) doit
     être dans le libellé Qonto (garde anti-faux-positif « bon montant, mauvais client »).
   - **Montant** : reçu (`amount` **ou** `local_amount`) ∈ `[total − 45 €, total + 2 €]` (frais SWIFT / arrondi).
   - **Confiance** : `exact` (|reçu − total| ≤ 0,02) · `swift` (bande + `swift_income` + couple **unique**)
     · `warn` (bande non-exact ou ambigu → listé, jamais auto-attaché). Auto-attache = `exact` + `swift`.
   - Un paiement ne peut pas précéder l'émission de la facture (marge 5 j).
4. **Attacher** — `get_attachment_url` (URL présignée S3 ~30 min) → `download_url` → `upload_attachment`
   sur la transaction créditrice (skip si elle a déjà une PJ → **idempotent**, auto-réparation).
5. **Marquer** — `reports/recettes/processed.json` (clé = **tx_id**).

### Lancer

```bash
python3 scripts/run_recettes.py --since 2026-04-01 --dry-run   # test : rapproche, n'attache rien
python3 scripts/run_recettes.py --since 2026-04-01             # flux complet
```

### Automatisation continue (launchd)

Poller toutes les 15 min (`com.maisondarwish.recettes-watch.plist`, `StartInterval=900`) : le
déclencheur est côté Qonto (virement entrant), donc pas de pré-check — chaque passage relit les
virements et attache dès qu'une facture correspond.

```bash
cp scripts/com.maisondarwish.recettes-watch.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.maisondarwish.recettes-watch.plist
# suivre : tail -f reports/recettes/watch.log
```

### Fichiers

| Fichier | Rôle |
|--------|------|
| `src/recon/qonto_client.py` | `fetch_all_credits`, `list_client_invoices`, `get_attachment_url`, `download_url` |
| `scripts/reconcile_recettes.py` | Rapproche factures clients ↔ virements (nom + montant, tolérance SWIFT) |
| `scripts/run_recettes.py` | Orchestrateur (`--since`, `--dry-run`) |
| `scripts/run_recettes_watch.sh` + `com.maisondarwish.recettes-watch.plist` | Poller 15 min |
| `tests/test_recettes_match.py` | Règles de match (exact, SWIFT unique, faux positif, ambiguïté) |

### Notes

- **Ne touche pas le statut de la facture** (`mark_client_invoice_as_paid` non utilisé) — attache seulement le PDF.
- `upload_attachment` fonctionne côté **crédit** (Qonto génère même la version probante).
- Les factures « sans virement » du rapport = impayées ou payées par un autre moyen — normal.
- Démarre au **01/04/2026**.

---

## Cockpit — tableau de bord local

Vue unique de l'état des 7 flux + actions à la demande. Serveur Python **stdlib** (aucun pip), page web perso.

### Lancer

```bash
/usr/bin/python3 scripts/dashboard.py          # → http://127.0.0.1:8787
```

Service permanent (launchd) :

```bash
cp scripts/com.maisondarwish.dashboard.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.maisondarwish.dashboard.plist
# recharger après une maj du code :
launchctl kickstart -k gui/$(id -u)/com.maisondarwish.dashboard
```

Accès à distance : tunnel vers le Mac (Tailscale ou Cloudflare) — voir `scripts/tunnel/README.md`. Bind **127.0.0.1 uniquement**, jamais `0.0.0.0` (secrets restent sur le Mac).

### Panneaux

1. **Santé sessions portails** — 🟢🟡🔴 depuis l'horizon des cookies *valides* + mtime (openai/hunter = port CDP 9222). Bouton **Tester** = sonde live headless (vérité serveur, attrape les faux-verts type Bouygues).
2. **Vue rapprochement par flux** — compteurs rapprochées / à vérifier / en attente lus dans les `reconciliation_*.md`.
3. **Qonto sans justificatif** — **live** (`fetch_unreconciled_expenses`), pas de cache figé → parité avec l'app Qonto.

### Actions

- **Reconnecter** (session 🔴) → `fetch_<key>.py --init-session` headed (login manuel + 2FA).
- **Retenter** (par tx) → recherche en **cascade** : Gmail/HTML (flux 6) → Drive OCR (flux 3) → portail (flux 5). Propose le PDF, **Confirmer** attache (garde-fou idempotence live). Ambigu → liste de candidats à choisir (aperçu **Voir**).
- **Ignorer** (tx non rapprochable) → masque en local (`reports/cockpit/ignored.json`) **+ ouvre la tx dans Qonto web** (deep-link `?highlight=<id>`) pour marquer « justificatif non requis » en 1 clic (l'API third-party ne l'expose pas : `PATCH /transactions` → 404).
- **Ajouter un abonnement** → email (ligne dans `saas_senders.csv`) ou portail (ligne `portal_vendors.csv` + creds `.env` + capture login via `fetch_generic.py`).

### Endpoints (127.0.0.1:8787)

`GET /api/status`, `GET /api/unreconciled` (live), `GET /api/pdf` (aperçu, bridé à `reports/`), `POST /api/{session/test, session/relogin, retry, attach, ignore, unignore, subscription/add}`.

### Fichiers

- `scripts/dashboard.py` (serveur) + `scripts/dashboard.html` (UI) + `src/recon/health.py` (statut sessions + compteurs — pur stdlib, testable).
- `scripts/portals/fetch_generic.py` — scraper portail générique (repli dans `run_portals.py` si pas de `fetch_<key>.py`).
- `scripts/qonto_web.py` — exploration UI Qonto (non utilisé : le deep-link + clic manuel a remplacé le clic auto headless, trop fragile).
- `reports/cockpit/ignored.json` — tx masquées (gitignored).

### Notes

- **Bouygues** = SPA OAuth **non scrapable** (factures via API interne, jamais en lien de page) → dépôt manuel ou ignorer.
- Retenter Drive matche par **montant** dans l'OCR (factures type Air France : codes de réservation, pas de nom marchand lisible) ; l'humain confirme.
- `.qonto_web_session.json` et `reports/` restent **gitignored**.

---

## Notes générales

- Pas de fabrication : un justificatif n'est attaché que si un **vrai reçu** existe.
- Les justificatifs Qonto ne sont pas des copies probantes (`probative_attachment: unavailable`) ; les originaux restent dans Gmail.

---

## Historique récent

**v1.8 (2026-07-07)** — Cockpit (tableau de bord local)
- **`dashboard.py`** (serveur stdlib, bind 127.0.0.1) + **`dashboard.html`** + **`health.py`** : 3 panneaux
  (santé sessions, compteurs par flux, tx Qonto sans justif **live**). Service launchd + tunnel (Tailscale/Cloudflare).
- **Retenter** en cascade : Gmail/HTML (flux 6) → **Drive OCR** (flux 3, match par montant) → portail (flux 5).
  Ambigu → sélection de candidats (aperçu PDF). **Confirmer** attache (idempotence live).
- **Ignorer** : masque local + deep-link `?highlight=` vers Qonto web (l'API ne permet pas `attachment_required=false`,
  `PATCH /transactions` → 404). **Ajouter un abonnement** depuis l'UI (email ou portail générique).
- **Fix santé session** : l'horizon = expiration la plus *lointaine* des cookies valides (l'ancien `min()` mettait
  tout en rouge à cause des cookies éphémères consent/Cloudflare `__cf_bm`).
- Constat : **Bouygues** non scrapable (SPA OAuth) ; `fetch_generic.py` pour les portails simples ajoutés via l'UI.

**v1.7 (2026-07-04)** — Flux 7 : recettes (côté crédit)
- **`reconcile_recettes.py` + `run_recettes.py`** : factures clients ↔ virements créditeurs → attache le
  PDF de la facture à la transaction créditrice. Nom client exigé + tolérance frais SWIFT (`amount` ou
  `local_amount` ∈ [total−45, total+2]). Confiance `exact`/`swift`(unique)/`warn`. Poller 15 min.
- **`qonto_client.py`** +4 méthodes : `fetch_all_credits` (side=credit), `list_client_invoices`
  (`/client_invoices`), `get_attachment_url` (`/attachments/{id}` → URL présignée), `download_url`.
- **LIVE** : 4 rapprochées (Allegria ×3, See You Soon ×1), F-2026-010 (swift) + F-2026-003 (exact)
  attachées, idempotent confirmé. `tests/test_recettes_match.py` (9 cas).

**v1.6 (2026-07-02)** — Portails : Hunter + Kandbaz, poller réparé
- **Hunter** ✅ : URLs déplacées (`/sign-in`, `/account/billing` = 404) → `/users/sign_in` +
  `/subscriptions` ; Cloudflare Turnstile → **CDP** ; factures **Stripe**. 2 factures attachées.
- **Kandbaz** (`fetch_kandbaz.py`) : login portail + **2FA email lue automatiquement dans Gmail**
  (code alphanum 6 car.). Factures `/mes-factures` = mêmes que par email (fallback) ; DL bloqué par
  pop-up conformité LCB-FT. Kandbaz rapproché 5/6 (options 3€ = lignes de facture attachées à la main).
- **Poller portails réparé** : chemin `~/Library/Python/3.9` mort → repli `/usr/bin/python3` dans
  `run_portals_watch.sh` (le watcher mourait avant de lancer `run_portals`).
- ⚠️ **Panne CDP Chrome 149** : `connect_over_cdp` cassé (`setDownloadBehavior`) → méthode vrai-Chrome
  (OpenAI/Hunter) HS temporairement ; portails sans Cloudflare OK en headless.

**v1.5 (2026-07-02)** — Flux 6 + robustesse rapprochement
- **Flux 6 — piloté par la transaction** (`reconcile_qonto.py`) : part d'une tx Qonto sans PJ,
  cherche le reçu dans Gmail par *nom du libellé + montant*, vérifie (montant-total ET nom), attache.
  **Sans liste blanche** (`label_merchant_tokens` tire le nom du libellé, strip `SQ*/UBR*/GC RE`…).
  Reçus HTML rendus si pas de PJ. Poller horaire.
- **Montant NET débité, pas le brut** : `Facturé <moyen>` (Bolt, promo) dans `saas.py` ;
  `Reste/Solde/Net à payer|régler|dû` (paiement partagé, resto Le Pschill) dans
  `reconcile_fournisseurs.py`.
- **Dates 2 chiffres** `DD.MM.YY` (`02.07.26`, acompte Villa Duflot) ; vendeur Villa Duflot + Uber.
- **Filet récap (`audit_unreconciled.py`)** : OCR des justificatifs scannés (image, pdftotext vide)
  + détection du net sans symbole devise + `_pdf_has_total(gap=)` paramétrable + codes ISO EUR/USD/GBP.
- **`exact_unique`** (`reconcile_saas.py`) : auto-attache sans nom si montant+date **unique** et
  spécifique (non rond), facture identifiée société. Garde-fous : unicité, fenêtre 5 j, débit non
  antérieur, l'identité EXIGE `company_id/trusted` (les alias incidents ne comptent pas).
- Tests : `tests/test_net_amount.py`, `test_unique_gate.py`, `test_qonto_label.py`.

**v1.4 (2026-06-27)** — Récap sur tout l'exercice comptable (≠ 7 j) ; parseur multi-candidats (factures à remise : Total, pas le prix avant remise) ; recherche montant insensible à la position du symbole (`€35.99`). Self-test parsing figé.

**v1.3 (2026-06-27)** — Robustesse parsing : formats nombre US/FR/EU + dates EN ; marchand lu dans le PDF (justificatif auto-envoyé rapproché).

**v1.2 (2026-06-27)** — Récap hebdo fiable
- Récap transaction-centric basé sur la vérité **live Qonto** + vérification adversariale avant envoi
  (cherche le justificatif avant de déclarer « non rapproché »). Escalade Claude autonome + repli.
- Corrections UberEats (trouvées en fiabilisant) : `find_pdf_url` (bon lien), download direct
  (`expect_download`), montant `Total` (pas `Sous-total`).
- `selftest_recap.py` : preuve rejouable.

**v1.1 (2026-06-27)** — Uber Rides + Bolt
- Uber Rides : scraper riders.uber.com (Details → Download Invoice, date sans année inférée). 1 attaché.
- Bolt : pas de portail web (mobile-only) → reçus HTML par email routés vers le flux SAAS. 4 attachés.
- `parse_html_receipt` étendu (Bolt : « Montant facturé X € », date FR « 11 mai 2026 »).
- **17 justificatifs attachés au total** (Wix 6, Notion 3, ChatGPT 3, Uber 1, Bolt 4).

**v1.0 (commit 67449fb, 2026-06-27)** — Flux 5 (portails vendeurs)
- Scrapers Playwright (Wix, Notion, OpenAI, Hunter, Hostinger, QR-Code-Gen, Bouygues, Airbnb, Turo, Uber Rides)
- Contournement CAPTCHA Cloudflare via CDP Chrome (port 9222) pour OpenAI/ChatGPT
- Fallback manuel (ingest_drop.py) pour les portails CAPTCHA-bloqués
- Orchestrateur + launchd poller 15 min

**Fluxes antérieurs**
- Flux 1 : Dépenses USD carte (reçus Gmail Square/Toast → PDF générés)
- Flux 2 : Remboursements UberEats (Gmail + portail → virements Qonto)
- Flux 3 : Factures fournisseurs (Google Drive + OCR Vision)
- Flux 4 : Factures SaaS par email (Gmail PDF + reçus HTML→PDF Chrome)
