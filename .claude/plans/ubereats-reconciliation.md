# Plan: UberEats Reconciliation Automation

## Context

User pays UberEats with personal card → company reimburses via Qonto wire transfer to "Nael Darwish". UberEats invoices are manually downloaded as PDFs from the UberEats portal. Goal: match each invoice to its Qonto reimbursement wire transfer, attach the PDF to the Qonto transaction, and produce a reconciliation report.

This follows the same pattern as existing USD receipt reconciliation (`scripts/gen_justificatifs_usd_apr.py`) — standalone stdlib-only Python script + Claude-mediated MCP calls for Qonto.

---

## Files to Create

### `scripts/reconcile_ubereats.py` (new)
Primary deliverable. Standalone Python 3, stdlib only.

**Boilerplate**: follow `scripts/backlog_report.py` (`sys.path.insert`, `argparse`, `main() -> int`, `raise SystemExit(main())`).

**Data structures** (`@dataclass`):
```python
UberEatsInvoice  # path, invoice_id, date (ISO), amount (float EUR), raw_text
QontoTransfer    # id, label, amount (float, positive), date (ISO)
MatchResult      # invoice, transfer, date_gap_days, confidence ("exact"|"amount_only")
ReconciliationReport  # matched, unmatched_invoices, unmatched_transfers, run_date
```

**Functions**:
```
main()
├── scan_input_dir(dir) → list[Path]           # all *.pdf in dir
├── parse_invoice(pdf_path) → UberEatsInvoice  # pdftotext → regex
│   ├── _run_pdftotext(path) → str             # subprocess pdftotext -layout
│   └── _fallback_csv_manifest(dir)            # reads manifest.csv if pdftotext absent
├── load_qonto_transfers(json_path) → list[QontoTransfer]
├── match(invoices, transfers) → ReconciliationReport
├── write_matches_json(report, path)
├── write_reconciliation_report(report, path)  # .md, French, follow report.py style
└── print_summary(report)
```

**CLI**:
```
python3 scripts/reconcile_ubereats.py
  --input   reports/ubereats/input     # PDFs
  --transfers reports/ubereats/qonto_transfers.json
  --out-dir reports/ubereats
  --dry-run
  --since   YYYY-MM-DD     # default: 2026-04-01
  --warn-days N (default: 7)
```

### `reports/ubereats/input/` (directory, gitignored)
User drops UberEats PDFs here. Already covered by `reports/` gitignore.

### `reports/ubereats/manifest.csv` (optional, user-created fallback)
```
filename,date,amount,invoice_id
ubereats_2026-05-15.pdf,2026-05-15,47.80,INV-12345
```
Used when `pdftotext` unavailable or a PDF is malformed.

---

## Key Implementation Details

### PDF Parsing (`pdftotext` — same philosophy as Chrome headless)

```python
subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, encoding="utf-8")
```

Check via `shutil.which("pdftotext")` at startup. If absent and no `manifest.csv`: exit with `"Installer poppler : brew install poppler"`.

Regex extraction (first match wins, top-to-bottom):
- **Invoice ID**: `r'(?:facture\s*n[°o]?|invoice\s*#?)\s*:?\s*([A-Z0-9][A-Z0-9\-]{4,})'` — fallback to `path.stem`
- **Date** (try in order): ISO `YYYY-MM-DD` → French numeric `DD/MM/YYYY` → French long `15 mai 2026`
- **Amount**: `r'(?:total[^\n\d]*)(\d{1,6}[.,]\d{2})\s*(?:€|EUR)'` — normalize comma→dot, parse float
- **No fabrication**: if date or amount missing → `ValueError` → add to `unmatched_invoices` with error note

### Qonto Transfers JSON (written by Claude in Step A)

```json
[{"id": "uuid", "label": "Virement Nael Darwish...", "amount": "47.80", "currency": "EUR", "settled_at": "2026-05-16"}]
```
Amounts are positive (script takes `abs(float(amount))`).

### Matching Algorithm

Greedy bipartite assignment (cardinality small, ~< 30):
1. Sort invoices by date ascending
2. For each invoice: find unmatched transfers where `abs(round(t.amount,2) - round(i.amount,2)) <= 0.01`
3. Among candidates: rank by `abs(date_gap_days)` ascending
4. `date_gap_days <= warn_days` → `confidence="exact"` ; otherwise → `confidence="amount_only"` (warning in report)
5. No candidates → `unmatched_invoices` (silent — no error, no crash; just noted in report)
   - If input dir is empty or no PDFs parsed successfully → script exits cleanly with "Aucune facture UberEats trouvée" and writes no output files
6. Remaining transfers → `unmatched_transfers`

### `matches.json` (read by Claude in Step C)

```json
[{
  "invoice_path": "/abs/path/to/file.pdf",
  "invoice_id": "INV-12345",
  "invoice_date": "2026-05-15",
  "invoice_amount": 47.80,
  "qonto_transaction_id": "uuid",
  "qonto_date": "2026-05-16",
  "confidence": "exact"
}]
```

### Report Format (`reconciliation_YYYY-MM-DD.md`)

French, tables, follows `src/recon/report.py` style. Sections:
1. Summary line (counts)
2. Table: matched (invoice ID, amount, dates, gap, status)
3. Table: unmatched invoices (with reason)
4. Table: unmatched Qonto transfers (with reason)
5. Footer: script name, run datetime, pointer to `matches.json`

---

## Full Workflow

**Step 0 — Setup (User, once)**
```bash
brew install poppler          # if not installed
mkdir -p reports/ubereats/input
```

**Step A — Fetch Qonto transfers (Claude via MCP)**
- `list_transactions` filtered: side=debit, label contains "Nael" or "Darwish", `settled_at >= 2026-04-01`
- Claude saves to `reports/ubereats/qonto_transfers.json`

**Step B — Drop PDFs + run script (User)**
1. Download invoices from UberEats portal → `reports/ubereats/input/`
2. `python3 scripts/reconcile_ubereats.py`
3. Review `reports/ubereats/reconciliation_YYYY-MM-DD.md`

**Step C — Attach PDFs to Qonto (Claude via MCP)**
- Claude reads `matches.json`
- For each match: `request_attachment_upload` → PUT binary → `upload_attachment` targeting `qonto_transaction_id`
- Reports success/failure per attachment

**Step D — Review (User)**
- Check `⚠️ amount_only` matches (date gap > 7 days) before confirming
- Investigate unmatched items manually

---

## Reference Files (reuse patterns)

| File | What to reuse |
|---|---|
| `scripts/backlog_report.py` | Script boilerplate, argparse, `main()` |
| `src/recon/report.py` | Markdown table rendering style |
| `src/recon/transactions.py` | `@dataclass` conventions, `_to_float` |
| `src/recon/config.py` | `.env` parser pattern (if needed) |
| `README.md` | Qonto MCP attachment flow description |

---

## Verification

1. `brew install poppler && pdftotext -v` — confirm tool available
2. Drop 1–2 real UberEats PDFs in `reports/ubereats/input/`
3. Run `python3 scripts/reconcile_ubereats.py --dry-run` — confirm parsing output (invoice ID, date, amount) in console
4. Run Step A (Claude fetches Qonto transfers) — confirm `qonto_transfers.json` has correct shape
5. Run script without `--dry-run` — verify `matches.json` and `.md` report look correct
6. Run Step C (Claude attaches) — verify in Qonto UI that the PDF is attached to the transaction
