"""Briques partagées du flux « saas » (v2 — balayage large).

On balaie TOUS les emails avec PJ PDF depuis une date, on parse chaque PDF
(montant/devise/date) et on déduit un fournisseur (table d'alias + nom d'expéditeur).
Garde-fous pour le rapprochement : `looks_like_invoice` (vraie facture/reçu) et
`has_company_id` (facturé à Maison Darwish).

Aucune dépendance externe (stdlib + binaire `pdftotext` de poppler).
"""
from __future__ import annotations

import csv
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

VENDORS_PATH = Path(__file__).resolve().parent / "saas_senders.csv"

# Identité société (facturé à Maison Darwish). Normalisé (majuscules, sans accents).
_COMPANY_IDS = ("MAISON DARWISH", "231 RUE SAINT HONORE", "75001 PARIS", "75001")

# Mots-clés « ceci est une facture/reçu » (élimine riders, billets non facturés, contrats).
_INVOICE_WORDS = ("FACTURE", "INVOICE", "RECU", "RECEIPT", "TOTAL", "TTC", "TVA",
                  "MONTANT", "AMOUNT", "NET A PAYER", "SOUS-TOTAL", "SUBTOTAL",
                  "BILLED", "PAYMENT", "PRELEVEMENT", "REGLEMENT")

# Mots ignorés quand on déduit un alias depuis le nom/domaine d'expéditeur.
_SENDER_STOP = {"MAIL", "EMAIL", "NOREPLY", "NO", "REPLY", "BILLING", "INVOICE",
                "INVOICES", "PAYMENTS", "PAYMENT", "STATEMENTS", "SYSTEM", "SENT",
                "VIA", "NETSUITE", "NOTIFICATION", "NOTIFICATIONS", "INFO", "CONTACT",
                "SUPPORT", "SERVICE", "MESSENGER", "APP", "PBC", "INC", "SAS", "SARL",
                "LTD", "GMBH", "THE", "COM", "FR", "EU", "IO", "ORG", "NET"}


@dataclass(frozen=True)
class SaasVendor:
    """Fournisseur connu : aide à relier nom marchand ↔ libellé Qonto."""

    name: str
    domains: List[str]      # sous-chaînes à chercher dans l'expéditeur
    aliases: List[str]      # motifs attendus dans le libellé Qonto (normalisés)
    trusted: bool
    note: str


def load_vendors(path: Optional[Path] = None) -> List[SaasVendor]:
    csv_path = path or VENDORS_PATH
    if not csv_path.exists():
        return []
    out: List[SaasVendor] = []
    with csv_path.open(encoding="utf-8") as fh:
        rows = (line for line in fh if not line.lstrip().startswith("#"))
        for row in csv.DictReader(rows):
            if not row.get("name"):
                continue
            out.append(SaasVendor(
                name=row["name"].strip(),
                domains=[d.strip().lower() for d in (row.get("domains") or "").split("|") if d.strip()],
                aliases=[a.strip().upper() for a in (row.get("label_aliases") or "").split("|") if a.strip()],
                trusted=(row.get("trusted") or "").strip() == "1",
                note=(row.get("note") or "").strip(),
            ))
    return out


def match_vendor(sender: str, subject: str, vendors: List[SaasVendor]) -> Optional[SaasVendor]:
    """Identifie le fournisseur par domaine d'expéditeur OU alias présent dans le sujet."""
    s = (sender or "").lower()
    subj = strip_accents(subject or "").upper()
    for v in vendors:
        if any(d in s for d in v.domains):
            return v
        if any(a in subj for a in v.aliases):
            return v
    return None


# --- Normalisation / texte -------------------------------------------------- #

def strip_accents(text: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", text)
                   if unicodedata.category(c) != "Mn")


def sender_aliases(from_header: str) -> List[str]:
    """Alias déduits de l'en-tête From : mots du nom affiché + cœur du domaine.

    Ex. '"Aircall Billing" <no-reply@billing.aircall.io>' -> ['AIRCALL']
        'Agence Montparnasse <agence@agencemontparnasse.fr>' -> ['AGENCE','MONTPARNASSE','AGENCEMONTPARNASSE']
    """
    out: List[str] = []
    display = re.sub(r"<[^>]*>", "", from_header or "")
    display = strip_accents(display).upper()
    for tok in re.split(r"[^A-Z0-9]+", display):
        if len(tok) >= 4 and tok not in _SENDER_STOP and not tok.isdigit():
            out.append(tok)
    m = re.search(r"@([^>\s]+)", from_header or "")
    if m:
        parts = strip_accents(m.group(1)).upper().split(".")
        for tok in parts:
            if len(tok) >= 4 and tok not in _SENDER_STOP and not tok.isdigit():
                out.append(tok)
    # dédoublonne en gardant l'ordre
    seen = set()
    return [a for a in out if not (a in seen or seen.add(a))]


def looks_like_invoice(text: str) -> bool:
    t = strip_accents(text or "").upper()
    return any(w in t for w in _INVOICE_WORDS)


def vendor_aliases_from_text(text: str, vendors: List[SaasVendor]) -> List[str]:
    """Alias des fournisseurs connus DÉTECTÉS dans le corps d'un justificatif.

    Permet de rapprocher un reçu auto-transféré (expéditeur = soi-même, nom de fichier
    quelconque) : le marchand (« Airbnb », « Uber »…) est lu DANS le PDF → alias → croisé
    au libellé Qonto. Sans ça, un justificatif qu'on s'envoie à soi n'a aucun alias marchand.
    """
    if not text:
        return []
    t = strip_accents(text).upper()
    out: List[str] = []
    for v in vendors:
        name_u = strip_accents(v.name).upper()
        domain_core = [strip_accents(d.split(".")[0]).upper() for d in v.domains
                       if len(d.split(".")[0]) >= 4]
        if name_u in t or any(c in t for c in domain_core) or any(a in t for a in v.aliases):
            out.extend(v.aliases or [name_u])
    seen: set = set()
    return [a for a in out if not (a in seen or seen.add(a))]


# --- Reçus HTML (Square/Sunday/Toast/Clover : pas de PDF joint) -------------- #

# Domaines d'expéditeurs qui envoient le reçu en HTML dans le corps (zéro PJ).
HTML_RECEIPT_DOMAINS = ("squareup.com", "messaging.squareup.com", "sundayapp.io",
                        "toasttab.com", "clover.com", "sumup.com",
                        "bolt.eu", "receipts-france@bolt.eu")
_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def is_receipt_sender(sender: str) -> bool:
    s = (sender or "").lower()
    return any(d in s for d in HTML_RECEIPT_DOMAINS)


@dataclass
class ParsedReceipt:
    amount: Optional[float]
    date: Optional[str]
    vendor: str            # nom marchand lu dans le reçu (ex. « CERTIFIED CAFE »)


_FR_MONTHS_FULL = {"janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
                   "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
                   "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
                   "decembre": 12}


def parse_html_receipt(text: str, subject: str) -> ParsedReceipt:
    """Extrait montant + date + marchand d'un reçu HTML (corps texte + sujet).

    Gère Square FR (« Vous avez payé 9,00 € à CERTIFIED CAFE … le 10/6/2026 »), anglais
    (« You paid … at … on … ») et Bolt (« Montant facturé 69,90 € », « 11 mai 2026 »).
    """
    # Si on reçoit du HTML brut (pas de partie texte), on retire les balises.
    if "<" in text and ">" in text:
        import html as _html
        text = _html.unescape(re.sub(r"<[^>]+>", " ", text))
        text = re.sub(r"\s+", " ", text)

    amount: Optional[float] = None
    for pat in (r"montant\s+factur[ée]\s+([\d  ]{1,12}[.,]\d{2})\s*[€$]",  # Bolt : net débité
                r"pay[ée]\w*\s+([\d  ]{1,12}[.,]\d{2})\s*[€$]",
                r"re[çc]u\s+de\s+([\d  ]{1,12}[.,]\d{2})\s*[€$]",
                r"\bpaid\s+[€$]?\s*([\d,]{1,12}\.\d{2})\b",
                r"\btotal\s*:?\s*([\d  ]{1,12}[.,]\d{2})\s*[€$]"):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            amount = _to_float(m.group(1))
            break
    if amount is None:
        amount = extract_amount(text)

    date = None
    m = re.search(r"\ble\s+(\d{1,2})/(\d{1,2})/(\d{4})", text, re.IGNORECASE)
    if m:
        date = f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    if date is None:
        # Date FR en toutes lettres (Bolt : « lundi, 11 mai 2026 »).
        m = re.search(r"(\d{1,2})\s+([A-Za-zéûàç]+)\s+(\d{4})", text)
        if m and m.group(2).lower() in _FR_MONTHS_FULL:
            date = f"{int(m.group(3)):04d}-{_FR_MONTHS_FULL[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    if date is None:
        date = extract_date(text)

    vendor = ""
    m = re.search(r"re[çc]u\s+de\s+(.+?)\s*(?:#|$)", subject, re.IGNORECASE)
    if m:
        vendor = m.group(1).strip()
    if not vendor:
        m = re.search(r"[àa]\s+([^\n,]{2,40}?)\s+avec\s+votre\s+carte", text, re.IGNORECASE)
        if m:
            vendor = m.group(1).strip()
    return ParsedReceipt(amount=amount, date=date, vendor=vendor)


def receipt_vendor_aliases(vendor: str) -> List[str]:
    """Alias Qonto à partir du nom marchand lu dans le reçu (ex. CERTIFIED CAFE)."""
    out = []
    for tok in re.split(r"[^A-Za-z0-9]+", strip_accents(vendor or "").upper()):
        if len(tok) >= 3 and tok not in _SENDER_STOP:
            out.append(tok)
    return out


def render_html_to_pdf(html: str, out_path: Path) -> bool:
    """Rend un corps HTML en PDF via Chrome headless. True si le PDF est créé."""
    import subprocess, tempfile
    if not Path(_CHROME).exists():
        return False
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "receipt.html"
        src.write_text(html, encoding="utf-8")
        # Chrome headless échoue parfois sous charge (lancements rapprochés) -> 1 retry.
        for attempt in range(2):
            try:
                subprocess.run(
                    [_CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                     f"--print-to-pdf={out_path}", str(src)],
                    capture_output=True, timeout=60,
                )
            except (subprocess.SubprocessError, OSError):
                pass
            if out_path.exists() and out_path.stat().st_size > 800:
                return True
    return out_path.exists() and out_path.stat().st_size > 800


def has_company_id(text: str) -> bool:
    t = strip_accents(text or "").upper()
    t = re.sub(r"\s+", " ", t)
    return any(cid in t for cid in _COMPANY_IDS)


# --- Extraction du contenu PDF --------------------------------------------- #

# Gère les séparateurs de milliers : espace (FR « 1 156,41 »), virgule (US « 1,156.41 »),
# point (EU « 1.156,41 »). _to_float lève ensuite l'ambiguïté virgule/point.
_THSEP = r"   .,"
_MONEY = (r"(?<![\d.,])("
          r"\d{1,3}(?:[" + _THSEP + r"]\d{3})+[.,]\d{2}"   # avec séparateurs de milliers
          r"|\d+[.,]\d{2}"                                  # simple
          r")(?![\d])")
# Du plus spécifique (montant réellement dû) au plus générique. « T.T.C » toléré pointé.
_TOTAL_LABELS = (
    r"amount\s+paid", r"montant\s+(?:total\s+)?(?:pay[ée]|pr[ée]lev[ée])",
    r"net\s+[àa]\s+payer", r"total\s+t\.?\s*t\.?\s*c", r"total\s+t\.?v\.?a\.?\s+incl",
    r"montant\s+total(?:\s+de\s+la\s+facture)?", r"total\s+amount",
    r"net\s+amount\s+paid", r"total\s+due", r"amount\s+due", r"grand\s+total", r"\btotal\b",
)
# Largeur max entre un label de total et son montant (mise en page -layout = espaces).
_LABEL_GAP = 200


def _to_float(raw: str) -> Optional[float]:
    s = raw.replace(" ", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def extract_text(pdf_path: Path) -> str:
    try:
        res = subprocess.run(
            ["pdftotext", "-layout", "-q", str(pdf_path), "-"],
            capture_output=True, timeout=30,
        )
        return res.stdout.decode("utf-8", errors="replace")
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""


def detect_currency(text: str) -> str:
    has_eur = "€" in text or re.search(r"\bEUR\b", text)
    has_usd = "$" in text or re.search(r"\bUSD\b", text)
    if has_usd and not has_eur:
        return "USD"
    if re.search(r"\bGBP\b|£", text):
        return "GBP"
    return "EUR"


def extract_amount(text: str) -> Optional[float]:
    best_labelled: Optional[float] = None
    for label in _TOTAL_LABELS:
        pat = label + r"[^\d\n]{0," + str(_LABEL_GAP) + r"}?" + _MONEY
        for m in re.finditer(pat, text, re.IGNORECASE):
            val = _to_float(m.group(1))
            if val is not None and (best_labelled is None or val > best_labelled):
                best_labelled = val
        if best_labelled is not None:
            return best_labelled
    vals = [v for v in (_to_float(m.group(1)) for m in re.finditer(_MONEY, text)) if v is not None]
    return max(vals) if vals else None


_FR_MONTHS = {"janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
              "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
              "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12}
_EN_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def extract_date(text: str) -> Optional[str]:
    m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"\b([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(20\d{2})\b", text)
    if m:
        mo = _EN_MONTHS.get(m.group(1).lower()[:3])
        if mo:
            return f"{int(m.group(3)):04d}-{mo:02d}-{int(m.group(2)):02d}"
    m = re.search(r"\b(\d{1,2})\s+([A-Za-zÀ-ÿ]{3,10})\.?\s+(20\d{2})\b", text)
    if m:
        # « 25 avril 2026 » (FR) ou « 25 April 2026 » (EN, mois complet ou abrégé).
        mo = _FR_MONTHS.get(m.group(2).lower()) or _EN_MONTHS.get(m.group(2).lower()[:3])
        if mo:
            return f"{int(m.group(3)):04d}-{mo:02d}-{int(m.group(1)):02d}"
    m = re.search(r"\b(\d{1,2})[/.](\d{1,2})[/.](20\d{2})\b", text)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


@dataclass
class ParsedPdf:
    amount: Optional[float]
    currency: str
    date: Optional[str]
    is_invoice: bool
    company_id: bool
    text_len: int


def parse_pdf(pdf_path: Path) -> ParsedPdf:
    text = extract_text(pdf_path)
    return ParsedPdf(
        amount=extract_amount(text),
        currency=detect_currency(text),
        date=extract_date(text),
        is_invoice=looks_like_invoice(text),
        company_id=has_company_id(text),
        text_len=len(text),
    )
