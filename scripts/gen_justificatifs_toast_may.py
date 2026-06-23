#!/usr/bin/env python3
"""Justificatifs Toast (La La Land, Thai Thai) — données réelles des reçus email.
Même style que gen_justificatifs_batch.py."""
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "reports" / "receipts"
OUT.mkdir(parents=True, exist_ok=True)

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
Maison Darwish SASU — mai 2026</div></body></html>"""

lala = page("Justificatif — La La Land Kind Cafe", """
<h1>☕ La La Land Kind Cafe — Reçu Toast</h1>
<div class="meta">
  <strong>Fournisseur :</strong> La La Land, 5600 West Lovers Lane, Dallas, TX 75209<br>
  <strong>Date :</strong> 3 mai 2026, 17h10 — Check #445<br>
  <strong>Auth code :</strong> QCWXN3 — Payment ID YJHdbkszxpmM<br>
  <strong>Carte :</strong> Debit Mastercard •••• 5121 (EMV Chip)<br>
  <strong>Source :</strong> email no-reply@toasttab.com (03/05/2026)
</div>
<table>
  <tr><th>Description</th><th class="amount">USD</th></tr>
  <tr><td>Medium Americano (+1 shot)</td><td class="amount">$ 5.55</td></tr>
  <tr><td>Medium Latte / Matcha + Coconut Milk</td><td class="amount">$ 6.35</td></tr>
  <tr><td>Sous-total</td><td class="amount">$ 11.90</td></tr>
  <tr><td>Tax</td><td class="amount">$ 0.98</td></tr>
  <tr><td>Tip</td><td class="amount">$ 1.78</td></tr>
  <tr class="total-row"><td>Total débité (Qonto : 12,51 €)</td><td class="amount">$ 14.66</td></tr>
</table>
""")

thai = page("Justificatif — Thai Thai", """
<h1>🍜 Thai Thai — Reçu Toast</h1>
<div class="meta">
  <strong>Fournisseur :</strong> Thai Thai, 1731 Greenville Avenue, Dallas, TX 75206<br>
  <strong>Date :</strong> 7 mai 2026, 20h38 — Check #178 (Table B1, 3 couverts)<br>
  <strong>Auth code :</strong> Q0EBAW — Payment ID bHm9Jg7qbR7C<br>
  <strong>Carte :</strong> Mastercard •••• 4119 (EMV Chip)<br>
  <strong>Source :</strong> email no-reply@toasttab.com (07/05/2026)
</div>
<table>
  <tr><th>Description</th><th class="amount">USD</th></tr>
  <tr><td>Fresh Salad Rolls</td><td class="amount">$ 6.95</td></tr>
  <tr><td>Dumpling (Steamed)</td><td class="amount">$ 8.95</td></tr>
  <tr><td>Pad See Ew (Beef, Spicy)</td><td class="amount">$ 16.95</td></tr>
  <tr><td>Seafood Soup (Bowl)</td><td class="amount">$ 23.95</td></tr>
  <tr><td>Pad Thai (Chicken)</td><td class="amount">$ 14.95</td></tr>
  <tr><td>Crispy Spring Rolls</td><td class="amount">$ 6.95</td></tr>
  <tr><td>3 × Ginger Tea</td><td class="amount">$ 8.85</td></tr>
  <tr><td>Sous-total</td><td class="amount">$ 87.55</td></tr>
  <tr><td>Tax</td><td class="amount">$ 7.22</td></tr>
  <tr><td>Tip</td><td class="amount">$ 17.51</td></tr>
  <tr class="total-row"><td>Total débité (Qonto : 95,64 €)</td><td class="amount">$ 112.28</td></tr>
</table>
""")

for name, content in {"receipt_lalaland-14_66.html": lala, "receipt_thaithai-112_28.html": thai}.items():
    (OUT / name).write_text(content, encoding="utf-8")
    print("écrit:", name)
