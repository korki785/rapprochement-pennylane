#!/usr/bin/env python3
"""Génère un justificatif HTML par fournisseur pour le batch de test (avril 2026).

Un fichier par fournisseur ; le même justificatif est ensuite attaché à toutes les
transactions Qonto de ce fournisseur (cf. mapping dans upload_justificatifs_batch.sh).
"""
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "reports"
OUT.mkdir(exist_ok=True)

CSS = """
  body { font-family: Arial, sans-serif; max-width: 680px; margin: 40px auto; color: #222; font-size: 14px; }
  h1 { color: #304CB2; border-bottom: 3px solid #304CB2; padding-bottom: 8px; }
  .meta { background: #f5f7ff; border-left: 4px solid #304CB2; padding: 12px 16px; margin: 20px 0; }
  table { width: 100%; border-collapse: collapse; margin: 16px 0; }
  th { background: #304CB2; color: white; padding: 8px 12px; text-align: left; }
  td { padding: 8px 12px; border-bottom: 1px solid #e0e0e0; }
  .amount { text-align: right; font-family: monospace; }
  .total-row td { font-weight: bold; background: #f0f4ff; border-top: 2px solid #304CB2; }
  .footer { font-size: 11px; color: #888; margin-top: 32px; border-top: 1px solid #ddd; padding-top: 12px; }
"""

def page(title, body):
    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<title>{title}</title><style>{CSS}</style></head><body>{body}
<div class="footer">Justificatif généré automatiquement depuis l'email du fournisseur —
Maison Darwish SASU — 21 juin 2026</div></body></html>"""


# 1. GOOGLE WORKSPACE
google = page("Justificatif — Google Workspace", """
<h1>Google Workspace — Facture mensuelle</h1>
<div class="meta">
  <strong>Fournisseur :</strong> Google Cloud France SARL, 8 Rue de Londres, 75009 Paris<br>
  <strong>N° de facture :</strong> GCFRD0012388020<br>
  <strong>Domaine :</strong> maisondarwish.fr | <strong>Compte :</strong> Maison Darwish<br>
  <strong>ID profil de paiement :</strong> 5513-6540-9819<br>
  <strong>Date :</strong> 1er avril 2026<br>
  <strong>Source :</strong> email payments-noreply@google.com (01/04/2026)
</div>
<table>
  <tr><th>Description</th><th class="amount">Montant</th></tr>
  <tr><td>Abonnement Google Workspace — avril 2026</td><td class="amount">19,44 €</td></tr>
  <tr class="total-row"><td>Total débité (Qonto)</td><td class="amount">19,44 €</td></tr>
</table>
<p style="font-size:12px;color:#666;">La facture PDF officielle (GCFRD0012388020) est jointe à l'email Google d'origine.</p>
""")

# 2. CLAUDE.AI / ANTHROPIC
claude = page("Justificatif — Claude.ai (Anthropic)", """
<h1>Claude.ai — Anthropic, PBC</h1>
<div class="meta">
  <strong>Fournisseur :</strong> Anthropic, PBC<br>
  <strong>Client :</strong> Naël Darwish — hello@maisondarwish.com<br>
  <strong>Source :</strong> emails invoice+statements@mail.anthropic.com
</div>
<table>
  <tr><th>Date</th><th>N° de reçu</th><th>Description</th><th class="amount">Montant</th></tr>
  <tr><td>04/04/2026</td><td>#2039-2296-2596</td><td>Abonnement Claude</td><td class="amount">18,00 €</td></tr>
  <tr><td>07/04/2026</td><td>#2811-2524-3478</td><td>Abonnement Claude (Pro)</td><td class="amount">73,80 €</td></tr>
  <tr class="total-row"><td colspan="3">Total débité (Qonto)</td><td class="amount">91,80 €</td></tr>
</table>
""")

# 3. AMERICAN AIRLINES
aa = page("Justificatif — American Airlines", """
<h1>✈️ American Airlines — Sièges vol DFW → CDG</h1>
<div class="meta">
  <strong>Vol :</strong> Dallas/Fort Worth (DFW) → Paris Charles de Gaulle (CDG)<br>
  <strong>Date du vol :</strong> dimanche 10 mai 2026, départ 17h (Economy)<br>
  <strong>Émis le :</strong> 9 avril 2026<br>
  <strong>Carte :</strong> Mastercard •••• 4119<br>
  <strong>Source :</strong> emails no-reply@info.email.aa.com (09/04/2026)
</div>
<table>
  <tr><th>Date</th><th>Confirmation</th><th>Description</th><th class="amount">USD</th><th class="amount">EUR débité</th></tr>
  <tr><td>09/04/2026</td><td>SDHJIR</td><td>Sélection de siège</td><td class="amount">$ 23.00</td><td class="amount">19,89 €</td></tr>
  <tr><td>09/04/2026</td><td>FLLQLH</td><td>Sélection de siège</td><td class="amount">$ 43.30</td><td class="amount">37,44 €</td></tr>
  <tr class="total-row"><td colspan="3">Total débité (Qonto)</td><td class="amount">$ 66.30</td><td class="amount">57,33 €</td></tr>
</table>
""")

files = {
    "justificatif_google-workspace_2026-04.html": google,
    "justificatif_claude-ai_2026-04.html": claude,
    "justificatif_american-airlines_2026-04.html": aa,
}
for name, content in files.items():
    (OUT / name).write_text(content, encoding="utf-8")
    print("écrit:", name)
