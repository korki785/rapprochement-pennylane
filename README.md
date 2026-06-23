# Rapprochement justificatifs USD → Qonto

Automatisation : pour chaque dépense par carte en **USD**, retrouver le **reçu reçu par
email**, générer un **justificatif PDF**, et l'**attacher à la transaction Qonto**
correspondante.

## Workflow

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

## Scripts

| Script | Justificatifs générés |
|--------|------------------------|
| `scripts/gen_justificatifs_usd_apr.py` | Crema Brentwood, Hands and Rose (avril 2026) |
| `scripts/gen_justificatifs_toast_may.py` | La La Land, Thai Thai (mai 2026) |

Chaque script est autonome (stdlib uniquement), avec les données réelles extraites des
reçus email.

### Lancer

```bash
python3 scripts/gen_justificatifs_usd_apr.py
python3 scripts/gen_justificatifs_toast_may.py
```

Les PDF sont écrits dans `reports/` (ignoré par git — régénérable, et déjà attaché côté
Qonto). Rendu HTML → PDF via Chrome headless :

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --disable-gpu --no-pdf-header-footer \
  --print-to-pdf=reports/receipts/<nom>.pdf "file://$PWD/reports/receipts/<nom>.html"
```

## Notes

- Pas de fabrication : un justificatif n'est généré que si un **vrai reçu email** existe.
- Les justificatifs générés ne sont pas des copies probantes Qonto
  (`probative_attachment: unavailable`) ; les originaux restent dans Gmail.
