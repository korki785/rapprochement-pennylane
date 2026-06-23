#!/usr/bin/env python3
"""Génère 2 justificatifs HTML (Crema Brentwood, Hands and Rose) — données réelles
extraites des reçus email (Square / Toast), pour rapprochement Qonto avril 2026.

Même style que gen_justificatifs_batch.py.
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
<div class="footer">Justificatif généré automatiquement depuis le reçu email du fournisseur —
Maison Darwish SASU — avril 2026</div></body></html>"""


# 1. CREMA BRENTWOOD (reçu Square)
crema = page("Justificatif — Crema Brentwood", """
<h1>☕ Crema Brentwood — Reçu Square</h1>
<div class="meta">
  <strong>Fournisseur :</strong> Crema Brentwood, 330 Franklin Rd Ste 904D, Brentwood, TN 37027-3280<br>
  <strong>Date :</strong> 20 avril 2026, 10h09<br>
  <strong>Reçu n° :</strong> #deCx — Auth code Q7AA99<br>
  <strong>Carte :</strong> Mastercard •••• 5121 (sans contact)<br>
  <strong>Source :</strong> email messenger@messaging.squareup.com (20/04/2026)
</div>
<table>
  <tr><th>Description</th><th class="amount">USD</th></tr>
  <tr><td>Americano — HOT 12oz (2 shot)</td><td class="amount">$ 4.48</td></tr>
  <tr><td>Cappuccino / Flat White — HOT 6oz (2 shot) + Oat</td><td class="amount">$ 6.46</td></tr>
  <tr><td>Sous-total</td><td class="amount">$ 10.94</td></tr>
  <tr><td>Brentwood (TN) Sales Tax (9,75 %)</td><td class="amount">$ 1.07</td></tr>
  <tr><td>Pourboire</td><td class="amount">$ 1.80</td></tr>
  <tr class="total-row"><td>Total débité (Qonto : 11,74 €)</td><td class="amount">$ 13.81</td></tr>
</table>
""")

# 2. HANDS AND ROSE (reçu Toast)
hands = page("Justificatif — Hands and Rose", """
<h1>☕ Hands and Rose — Reçu Toast</h1>
<div class="meta">
  <strong>Fournisseur :</strong> Hands and Rose, 1911 Wall St., Dallas, TX 75215<br>
  <strong>Date :</strong> 27 avril 2026, 11h01 — Check #304<br>
  <strong>Auth code :</strong> QG98WO — Payment ID xnCsNWHqhqbm<br>
  <strong>Carte :</strong> Mastercard •••• 4119 (EMV Chip)<br>
  <strong>Source :</strong> email no-reply@toasttab.com (27/04/2026)
</div>
<table>
  <tr><th>Description</th><th class="amount">USD</th></tr>
  <tr><td>12oz Latte + Almond (hot, regular)</td><td class="amount">$ 6.00</td></tr>
  <tr><td>12oz Base Drip</td><td class="amount">$ 2.50</td></tr>
  <tr><td>Cinnamon Roll</td><td class="amount">$ 3.75</td></tr>
  <tr><td>Sous-total</td><td class="amount">$ 12.25</td></tr>
  <tr><td>Pourboire</td><td class="amount">$ 1.00</td></tr>
  <tr class="total-row"><td>Total débité (Qonto : 11,36 €)</td><td class="amount">$ 13.25</td></tr>
</table>
""")

files = {
    "justificatif_crema-brentwood_2026-04.html": crema,
    "justificatif_hands-and-rose_2026-04.html": hands,
}
for name, content in files.items():
    (OUT / name).write_text(content, encoding="utf-8")
    print("écrit:", name)
