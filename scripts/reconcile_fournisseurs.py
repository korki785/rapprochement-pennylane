#!/usr/bin/env python3
"""Rapprochement des factures fournisseurs (Google Drive) avec les transactions Qonto.

Pour chaque facture PDF téléchargée depuis le Drive « Factures fournisseurs »,
ce script trouve la transaction Qonto correspondante et produit `matches.json`
(consommé par run_fournisseurs.py pour attacher les PDF) + un rapport markdown.

Les factures sont souvent des reçus SCANNÉS (image, sans couche texte) :
pdftotext renvoie vide -> repli OCR via Vision macOS (scripts/ocr/ocrbin).

Montant retenu = le plus grand montant du reçu = total réellement débité
(« Total Tender » / « CARTE BANCAIRE » / TTC, pourboire inclus). Le « Total »
hors pourboire ne correspond pas au débit Qonto.

Rapprochement agnostique à la devise : le montant débité est comparé à la fois
au montant EUR (`amount`) et au montant en devise d'origine (`local_amount`) de
chaque transaction Qonto. Inutile de convertir : Qonto stocke déjà les deux.

Usage :
    python3 scripts/reconcile_fournisseurs.py \\
        --input        reports/fournisseurs/input \\
        --transactions reports/fournisseurs/qonto_debits.json \\
        --out-dir      reports/fournisseurs \\
        [--since YYYY-MM-DD] [--warn-days 7] [--dry-run]

Repli si OCR indisponible : reports/fournisseurs/manifest.csv
(filename,date,amount,currency,drive_file_id).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# Permet `python3 scripts/reconcile_fournisseurs.py` sans installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

DEFAULT_SINCE = "2026-04-01"
DEFAULT_WARN_DAYS = 7
AMOUNT_TOLERANCE = 0.01
PERSO_TERMS = ("nael", "darwish")

# --- Remboursements perso en devise étrangère (passe 3) -------------------- #
# Un reçu en devise (ex. hôtel norvégien NOK) payé avec une carte PERSO n'a pas
# de transaction Qonto dans cette devise ; il est remboursé par un virement EUR
# à « Nael Darwish ». On ne peut donc pas matcher sur le montant (NOK ≠ EUR).
# Heuristique (validée sur N0560 Thon Hotel, NOK 278 -> virement 25.33€ du 17/06) :
#   1. Le virement ne peut PAS précéder la dépense  -> settled_at >= date du reçu.
#   2. Fenêtre courte après la dépense (REIMBURSE_WINDOW_DAYS).
#   3. Taux implicite (montant_devise / montant_virement_EUR) dans une bande
#      plausible pour la devise (_FX_BANDS).
#   4. À égalité, le virement le plus proche gagne (le MÊME JOUR est le signal fort).
# Résultat marqué confidence="warn" (FX approximatif -> à vérifier avant d'attacher).
REIMBURSE_WINDOW_DAYS = 10
_FX_BANDS = {            # montant en devise par 1 EUR (bandes approximatives)
    "USD": (1.00, 1.20),
    "GBP": (0.80, 0.92),
    "CHF": (0.88, 1.06),
    "NOK": (10.0, 12.2),
    "SEK": (10.5, 12.3),
    "DKK": (7.2, 7.7),
    "MAD": (10.0, 11.3),  # dirham marocain (taux carte observé ~10.6)
}

OCR_DIR = Path(__file__).resolve().parent / "ocr"
OCR_BIN = OCR_DIR / "ocrbin"
OCR_SRC = OCR_DIR / "ocr_helper.swift"

# Lignes à ignorer pour la détection du montant (versions, codes, n° internes).
_NON_MONEY_LINE = re.compile(
    r"version|num[ée]ro|production|auth|rcs|code|tel\b|t[ée]l\b|siret|tva\s*intra",
    re.IGNORECASE,
)

_FR_MONTHS = {
    # Français + allemand (factures FR/DE fréquentes).
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
    "januar": 1, "februar": 2, "märz": 3, "marz": 3, "juni": 6, "juli": 7,
    "oktober": 10, "dezember": 12,
}
_EN_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# Abréviations 3 lettres EN/FR/DE pour le format compact « 22APR26 », « 17 Jun'26 ».
_MONTH_ABBR = {
    "jan": 1, "feb": 2, "fév": 2, "mar": 3, "mär": 3, "apr": 4, "avr": 4,
    "may": 5, "mai": 5, "jun": 6, "jui": 6, "jul": 7, "aug": 8, "aou": 8,
    "sep": 9, "oct": 10, "okt": 10, "nov": 11, "dec": 12, "déc": 12, "dez": 12,
}


# --------------------------------------------------------------------------- #
# Structures de données
# --------------------------------------------------------------------------- #
@dataclass
class SupplierInvoice:
    path: Path
    drive_file_id: str
    drive_name: str
    date: str           # ISO YYYY-MM-DD ("" si introuvable)
    amount: float       # meilleur candidat = total débité (0.0 si introuvable)
    currency: str       # ISO 4217, détecté pour l'affichage (EUR par défaut)
    raw_text: str = ""
    error: str = ""
    # Plusieurs interprétations du montant (total labellisé, max, …) : le match en
    # essaie PLUSIEURS avant d'abandonner (cf. facture à remise Hostinger).
    amount_candidates: List[float] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.error and bool(self.date) and self.amount > 0


@dataclass
class QontoDebit:
    id: str
    label: str
    amount: float           # EUR, positif
    currency: str           # devise du compte (EUR)
    local_amount: float     # montant en devise d'origine (0.0 si EUR)
    local_currency: str     # code devise d'origine ("" si EUR)
    settled_at: str         # ISO YYYY-MM-DD
    operation_type: str     # "card" | "transfer" | ...


@dataclass
class MatchResult:
    invoice: SupplierInvoice
    transaction: QontoDebit
    date_gap_days: int
    match_strategy: str     # "eur_card" | "eur_transfer" | "foreign_currency"
    confidence: str         # "exact" | "warn"


@dataclass
class ReconciliationReport:
    matched: List[MatchResult] = field(default_factory=list)
    unmatched_invoices: List[SupplierInvoice] = field(default_factory=list)
    unmatched_transactions: List[QontoDebit] = field(default_factory=list)
    run_date: str = ""


# --------------------------------------------------------------------------- #
# Extraction de texte : pdftotext, repli OCR Vision (macOS)
# --------------------------------------------------------------------------- #
def scan_input_dir(input_dir: Path) -> List[Path]:
    return sorted(input_dir.glob("*.pdf"))


def _pdftotext_bin() -> Optional[str]:
    found = shutil.which("pdftotext")
    if found:
        return found
    for c in ["/opt/homebrew/bin/pdftotext", "/usr/local/bin/pdftotext"]:
        if Path(c).exists():
            return c
    return None


def _pdftoppm_bin() -> Optional[str]:
    found = shutil.which("pdftoppm")
    if found:
        return found
    for c in ["/opt/homebrew/bin/pdftoppm", "/usr/local/bin/pdftoppm"]:
        if Path(c).exists():
            return c
    return None


def _run_pdftotext(path: Path) -> str:
    bin_path = _pdftotext_bin() or "pdftotext"
    proc = subprocess.run(
        [bin_path, "-layout", str(path), "-"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        raise ValueError(f"pdftotext a échoué : {proc.stderr.strip()}")
    return proc.stdout


def _ensure_ocr_bin() -> Optional[str]:
    """Renvoie le chemin du binaire OCR, le compilant au besoin (swiftc)."""
    if OCR_BIN.exists():
        return str(OCR_BIN)
    if not OCR_SRC.exists() or not shutil.which("swiftc"):
        return None
    try:
        subprocess.run(
            ["swiftc", "-O", str(OCR_SRC), "-o", str(OCR_BIN)],
            check=True, capture_output=True,
        )
        return str(OCR_BIN)
    except Exception:
        return None


def _ocr_pdf(path: Path) -> str:
    """OCR d'un PDF scanné : pdftoppm (PNG) -> Vision (ocrbin). "" si indisponible."""
    binp = _ensure_ocr_bin()
    ppm = _pdftoppm_bin()
    if not binp or not ppm:
        return ""
    chunks: List[str] = []
    with tempfile.TemporaryDirectory() as td:
        prefix = str(Path(td) / "pg")
        try:
            subprocess.run([ppm, "-png", "-r", "200", "-f", "1", "-l", "3", str(path), prefix],
                           check=True, capture_output=True)
        except Exception:
            return ""
        for png in sorted(Path(td).glob("pg*.png")):
            try:
                r = subprocess.run([binp, str(png)], capture_output=True, text=True)
                chunks.append(r.stdout)
            except Exception:
                pass
    return "\n".join(chunks)


def _cache_path(path: Path) -> Path:
    """Sidecar texte : reports/fournisseurs/.ocr_cache/<nom>.txt."""
    return path.parent.parent / ".ocr_cache" / (path.stem + ".txt")


def _extract_text(path: Path) -> str:
    """Texte du PDF : cache, sinon pdftotext, sinon OCR. Met le cache à jour.

    L'OCR étant coûteux (~plusieurs s/page), le résultat est mis en cache : les
    relances et le run hebdo ne ré-OCR-isent que les nouveaux fichiers.
    """
    cache = _cache_path(path)
    try:
        if cache.exists() and cache.stat().st_mtime >= path.stat().st_mtime:
            return cache.read_text(encoding="utf-8")
    except OSError:
        pass

    try:
        txt = _run_pdftotext(path)
    except Exception:
        txt = ""
    if len(txt.strip()) < 20:  # PDF scanné -> OCR
        ocr = _ocr_pdf(path)
        if len(ocr.strip()) > len(txt.strip()):
            txt = ocr

    if txt.strip():
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(txt, encoding="utf-8")
        except OSError:
            pass
    return txt


# --------------------------------------------------------------------------- #
# Parsing : date, montant, devise
# --------------------------------------------------------------------------- #
def _parse_date(text: str, currency: str = "EUR") -> str:
    """Première date trouvée au format ISO, sinon "".

    `currency` départage l'ordre jour/mois ambigu des dates numériques :
    USD -> MM/DD/YYYY (US), sinon DD/MM/YYYY (FR/EU).
    """
    # ISO YYYY-MM-DD
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    # Numérique : DD/MM/YYYY, MM/DD/YYYY, DD-MM-YYYY, DD.MM.YYYY (année 2 ou 4 chiffres).
    us_first = currency.upper() == "USD"
    for mm in re.finditer(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\b", text):
        a, b, y = int(mm.group(1)), int(mm.group(2)), int(mm.group(3))
        if y < 100:
            y += 2000
        if a > 12 and b <= 12:        # a ne peut être qu'un jour
            d, mo = a, b
        elif b > 12 and a <= 12:      # b ne peut être qu'un jour
            d, mo = b, a
        elif a <= 12 and b <= 12:     # ambigu -> ordre selon la devise
            d, mo = (b, a) if us_first else (a, b)
        else:
            continue                  # les deux > 12 : invalide
        if 1 <= mo <= 12 and 1 <= d <= 31 and 2000 <= y <= 2099:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # Long « 09 JUIN 2026 » / « 15. Mai 2026 » (FR/DE, point optionnel après le jour)
    m = re.search(r"\b(\d{1,2})\.?\s+([A-Za-zÀ-ÿ]{3,})\s+(\d{4})\b", text)
    if m:
        mo = _FR_MONTHS.get(m.group(2).lower())
        if mo:
            return f"{int(m.group(3)):04d}-{mo:02d}-{int(m.group(1)):02d}"
    # Anglais « Apr 14, 2026 » / « May 11 2026 »
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b", text)
    if m:
        mo = _EN_MONTHS.get(m.group(1).lower()[:3])
        if mo:
            return f"{int(m.group(3)):04d}-{mo:02d}-{int(m.group(2)):02d}"
    # Compact « 22APR26 » / « 17 Jun'26 » : jour, abrév 3 lettres, année 2-4 chiffres
    m = re.search(r"\b(\d{1,2})\s*([A-Za-zÀ-ÿ]{3})['’.]?\s*(\d{2,4})\b", text)
    if m:
        mo = _MONTH_ABBR.get(m.group(2).lower())
        if mo:
            y = int(m.group(3))
            if y < 100:
                y += 2000
            if 2000 <= y <= 2099:
                return f"{y:04d}-{mo:02d}-{int(m.group(1)):02d}"
    return ""


def _detect_currency(text: str) -> str:
    """Devise du reçu. EUR par défaut.

    Pour les paiements carte Qonto en devise, le rapprochement passe par
    local_amount/local_currency (détection non critique). Mais pour les reçus
    en devise payés PERSO (passe 3, remboursement EUR), la devise détectée est
    indispensable pour choisir la bonne bande FX.
    """
    low = text.lower()
    scores = {
        "EUR": text.count("€") + len(re.findall(r"\beur\b|euro|carte bancaire", low)),
        "USD": text.count("$") + len(re.findall(r"\busd\b|salestax|total tender", low)),
        "GBP": text.count("£") + len(re.findall(r"\bgbp\b", low)),
        "NOK": len(re.findall(r"\bnok\b|\bkr\b", low)),
        "SEK": len(re.findall(r"\bsek\b", low)),
        "DKK": len(re.findall(r"\bdkk\b", low)),
        "CHF": len(re.findall(r"\bchf\b", low)),
        "MAD": len(re.findall(r"\bmad\b|\bdhs?\b|dirham", low)),
    }
    best = max(scores, key=lambda c: scores[c])
    if scores[best] == 0:
        return "EUR"
    # À égalité avec EUR, EUR l'emporte (devise par défaut des factures FR).
    if scores["EUR"] == scores[best]:
        return "EUR"
    return best


def _to_float(s: str) -> float:
    s = s.replace(" ", "").replace("\xa0", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


# Lookarounds : le nombre ne doit pas être collé à d'autres chiffres/points/virgules,
# sinon on capte des numéros de reçu / versions (« R922212.9512 » -> 212.95). Gère les
# milliers (« 1,156.41 » US, « 1 156,41 » FR). _to_float lève l'ambiguïté virgule/point.
_MONEY_PAT = re.compile(r"(?<![\d.,])\d{1,3}(?:[ \xa0,.]\d{3})*[.,]\d{2}(?![\d.,])")

# Labels d'un VRAI total payé, du plus spécifique au plus générique.
_TOTAL_LABELS_RANKED = (
    r"amount\s+paid", r"net\s+amount\s+paid", r"montant\s+(?:total\s+)?(?:pay[ée]|pr[ée]lev[ée])",
    # NET réellement débité sur CETTE carte quand une partie est déjà réglée (addition partagée /
    # acompte) ou après ajustement pourboire → « Total » brut ≠ débit. Ex. resto Le Pschill :
    # Total 30,70 ; déjà payé 7,00 ; pourboire -1,00 → « Reste à payer 24,70 » = le vrai débit.
    # Famille FR du solde restant, OCR-tolérante (accent « à/a » et espaces optionnels). Rang haut
    # (prioritaire sur « Total »). Absorbe l'ancien « net à payer ».
    r"reste\s*[àa]?\s*(?:payer|r[ée]gler)", r"reste\s*d[ûu]\b", r"restant\s*d[ûu]\b",
    r"solde\s*[àa]?\s*(?:payer|r[ée]gler)", r"net\s*[àa]?\s*(?:payer|r[ée]gler)",
    r"total\s+t\.?\s*t\.?\s*c", r"invoice\s+amount",
    r"montant\s+total(?:\s+de\s+la\s+facture)?", r"grand\s+total", r"\btotal\b",
)
# Lignes « total » à NE PAS prendre (montant brut / HT / déjà payé = 0).
_EXCL_TOTAL = re.compile(r"excl|hors\s*tax|\bh\.?t\.?\b|sous[\s-]?total|amount\s+due|\bdue\b",
                         re.IGNORECASE)


def _line_amounts(line: str) -> List[float]:
    out = []
    for m in _MONEY_PAT.finditer(line):
        v = _to_float(m.group(0))
        if 0 < v < 1_000_000:
            out.append(v)
    return out


def _amount_candidates(text: str) -> List[float]:
    """Tous les montants PLAUSIBLES pour le total payé, triés par fiabilité.

    Le match en essaie plusieurs (« plusieurs re-checks ») : d'abord les totaux LABELLISÉS
    (en excluant prix avant remise / HT / sous-total / « amount due » = 0), puis le max en
    repli (reçus sans label). Évite de prendre « €63.99 x 1 » au lieu de « Total €35.99 ».
    """
    labelled: List[tuple] = []     # (rang, valeur)
    all_vals: List[float] = []
    for line in text.splitlines():
        if _NON_MONEY_LINE.search(line):
            continue
        vals = _line_amounts(line)
        all_vals.extend(vals)
        if not vals or _EXCL_TOTAL.search(line):
            continue
        low = line.lower()
        for rank, lab in enumerate(_TOTAL_LABELS_RANKED):
            if re.search(lab, low):
                labelled.append((rank, vals[-1]))   # en -layout le total est à droite
                break
    out: List[float] = []
    for _, v in sorted(labelled, key=lambda x: x[0]):
        if v not in out:
            out.append(v)
    if all_vals:
        mx = max(all_vals)
        if mx not in out:
            out.append(mx)                          # repli : reçu sans label de total
    return out


def _parse_amount(text: str) -> float:
    """Meilleur montant (1er candidat). Compat : renvoie 0.0 si rien."""
    cands = _amount_candidates(text)
    return cands[0] if cands else 0.0


def parse_invoice(pdf_path: Path, drive_file_id: str = "", drive_name: str = "") -> SupplierInvoice:
    """Parse un PDF fournisseur (texte ou OCR). En cas d'échec : champ `error`."""
    drive_name = drive_name or pdf_path.name
    try:
        text = _extract_text(pdf_path)
    except Exception as exc:
        return SupplierInvoice(pdf_path, drive_file_id, drive_name, "", 0.0, "EUR", error=str(exc))

    if not text.strip():
        return SupplierInvoice(pdf_path, drive_file_id, drive_name, "", 0.0, "EUR",
                               error="texte illisible (ni pdftotext ni OCR)")

    currency = _detect_currency(text)
    date = _parse_date(text, currency)
    candidates = _amount_candidates(text)
    amount = candidates[0] if candidates else 0.0

    error = ""
    if not date:
        error = "date introuvable"
    elif amount <= 0:
        error = "montant introuvable"

    return SupplierInvoice(
        pdf_path, drive_file_id, drive_name, date, amount, currency,
        raw_text=text, error=error, amount_candidates=candidates,
    )


def _load_manifest(input_dir: Path) -> List[SupplierInvoice]:
    """Repli explicite : reports/fournisseurs/manifest.csv."""
    manifest = input_dir.parent / "manifest.csv"
    if not manifest.exists():
        return []
    invoices: List[SupplierInvoice] = []
    with manifest.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            path = input_dir / row["filename"]
            try:
                amount = float(str(row.get("amount", "")).replace(",", "."))
            except ValueError:
                amount = 0.0
            date = str(row.get("date", "")).strip()
            currency = (str(row.get("currency", "")).strip() or "EUR").upper()
            drive_id = str(row.get("drive_file_id", "")).strip()
            error = "" if (date and amount > 0) else "ligne manifest incomplète"
            invoices.append(SupplierInvoice(
                path, drive_id, path.name, date, amount, currency, error=error,
            ))
    return invoices


def load_invoices(input_dir: Path, id_map: Optional[Dict[str, str]] = None) -> List[SupplierInvoice]:
    """Charge les factures : manifest.csv si présent, sinon parsing (texte/OCR).

    id_map : {nom_fichier -> drive_file_id} pour rattacher l'ID Drive au PDF.
    """
    id_map = id_map or {}
    manifest_path = input_dir.parent / "manifest.csv"
    if manifest_path.exists():
        return _load_manifest(input_dir)
    pdfs = scan_input_dir(input_dir)
    if not pdfs:
        raise SystemExit("Aucune facture fournisseur trouvée dans le dossier input.")
    return [parse_invoice(p, drive_file_id=id_map.get(p.name, "")) for p in pdfs]


# --------------------------------------------------------------------------- #
# Lecture des transactions Qonto
# --------------------------------------------------------------------------- #
def load_qonto_debits(json_path: Path) -> List[QontoDebit]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    debits: List[QontoDebit] = []
    for t in data:
        def _f(key: str) -> float:
            try:
                return abs(float(str(t.get(key, "0")).replace(",", ".")))
            except (TypeError, ValueError):
                return 0.0
        debits.append(QontoDebit(
            id=str(t.get("id", "")),
            label=str(t.get("label", "")),
            amount=_f("amount"),
            currency=str(t.get("currency") or "EUR"),
            local_amount=_f("local_amount"),
            local_currency=str(t.get("local_currency") or "").upper(),
            settled_at=str(t.get("settled_at") or t.get("date") or "")[:10],
            operation_type=str(t.get("operation_type") or ""),
        ))
    return debits


# --------------------------------------------------------------------------- #
# Rapprochement (agnostique à la devise)
# --------------------------------------------------------------------------- #
def _days_between(d1: str, d2: str) -> int:
    from datetime import date
    try:
        return abs((date.fromisoformat(str(d1)[:10]) - date.fromisoformat(str(d2)[:10])).days)
    except (ValueError, TypeError):
        return 10 ** 6   # date illisible/None -> hors de toute fenêtre (jamais matché, pas de crash)


def _amount_close(a: float, b: float) -> bool:
    return abs(round(a, 2) - round(b, 2)) <= AMOUNT_TOLERANCE


def _strategy_for(inv: SupplierInvoice, t: QontoDebit) -> str:
    """Étiquette de stratégie d'après la transaction retenue."""
    if t.local_amount and _amount_close(t.local_amount, inv.amount) \
            and t.local_currency not in ("", "EUR"):
        return "foreign_currency"
    if t.operation_type == "transfer" and any(x in t.label.lower() for x in PERSO_TERMS):
        return "eur_transfer"
    return "eur_card"


def _days_signed(d_from: str, d_to: str) -> int:
    """Jours de d_from à d_to (positif si d_to est après d_from)."""
    from datetime import date
    return (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days


def _foreign_perso_candidate(inv: SupplierInvoice, debits: List[QontoDebit],
                             used: set) -> Optional[QontoDebit]:
    """Virement perso EUR remboursant un reçu en devise. None si rien de plausible."""
    band = _FX_BANDS.get(inv.currency.upper())
    if not band:
        return None
    lo, hi = band
    cands = []
    for t in debits:
        if t.id in used or not t.settled_at:
            continue
        if t.operation_type != "transfer":
            continue
        if not any(x in t.label.lower() for x in PERSO_TERMS):
            continue
        delta = _days_signed(inv.date, t.settled_at)   # 1. virement après la dépense
        if delta < 0 or delta > REIMBURSE_WINDOW_DAYS:  # 2. fenêtre courte
            continue
        if t.amount <= 0:
            continue
        fx = inv.amount / t.amount                       # 3. taux plausible
        if lo <= fx <= hi:
            cands.append((delta, t))
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])                       # 4. le plus proche (même jour gagne)
    return cands[0][1]


def match(invoices: List[SupplierInvoice], debits: List[QontoDebit],
          warn_days: int) -> ReconciliationReport:
    """Glouton en 3 passes.

    Passe 1-2 : montant débité (max du reçu) comparé à amount ET local_amount.
    Passe 3   : reçus en devise non matchés -> virement perso EUR via taux FX.
    """
    report = ReconciliationReport()
    report.unmatched_invoices.extend(i for i in invoices if not i.is_valid)

    valid = sorted((i for i in invoices if i.is_valid), key=lambda i: i.date or "")
    used: set[str] = set()
    unmatched: List[SupplierInvoice] = []

    # Passes 1-2 : match direct sur le montant (EUR ou devise via local_amount).
    # On essaie TOUS les candidats de montant (total labellisé, max…) avant d'abandonner.
    for inv in valid:
        inv_amounts = inv.amount_candidates or [inv.amount]
        cands = [
            t for t in debits
            if t.id not in used
            and t.settled_at
            and _days_between(inv.date, t.settled_at) <= warn_days
            and any(_amount_close(t.amount, a) or _amount_close(t.local_amount, a)
                    for a in inv_amounts)
        ]
        if not cands:
            unmatched.append(inv)
            continue
        best = min(cands, key=lambda t: _days_between(inv.date, t.settled_at))
        gap = _days_between(inv.date, best.settled_at)
        report.matched.append(MatchResult(
            inv, best, gap, _strategy_for(inv, best),
            "exact" if gap <= warn_days else "warn",
        ))
        used.add(best.id)

    # Passe 3 : reçus en devise étrangère payés perso -> virement EUR (FX approx).
    for inv in unmatched:
        if inv.currency.upper() not in _FX_BANDS:
            report.unmatched_invoices.append(inv)
            continue
        best = _foreign_perso_candidate(inv, debits, used)
        if best is None:
            report.unmatched_invoices.append(inv)
            continue
        gap = _days_between(inv.date, best.settled_at)
        report.matched.append(MatchResult(
            inv, best, gap, "foreign_perso", "warn",  # FX approximatif -> à vérifier
        ))
        used.add(best.id)

    report.unmatched_transactions = [t for t in debits if t.id not in used]
    return report


# --------------------------------------------------------------------------- #
# Sorties
# --------------------------------------------------------------------------- #
def write_matches_json(report: ReconciliationReport, path: Path) -> None:
    payload = [
        {
            "invoice_path": str(m.invoice.path.resolve()),
            "drive_file_id": m.invoice.drive_file_id,
            "invoice_name": m.invoice.drive_name,
            "invoice_date": m.invoice.date,
            "invoice_amount": round(m.invoice.amount, 2),
            "invoice_currency": m.invoice.currency,
            "qonto_transaction_id": m.transaction.id,
            "qonto_date": m.transaction.settled_at,
            "match_strategy": m.match_strategy,
            "confidence": m.confidence,
        }
        for m in report.matched
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _fmt_amount(amount: float, currency: str) -> str:
    sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency.upper(), currency)
    return f"{amount:.2f} {sym}"


_STRATEGY_LABEL = {
    "eur_card": "carte EUR",
    "eur_transfer": "virement perso",
    "foreign_currency": "carte devise",
    "foreign_perso": "remb. perso devise (FX)",
}


def write_reconciliation_report(report: ReconciliationReport, path: Path) -> None:
    n_ok = len(report.matched)
    n_exact = sum(1 for m in report.matched if m.confidence == "exact")
    n_warn = n_ok - n_exact
    out = ["# Rapprochement fournisseurs — factures ↔ transactions Qonto", ""]
    out.append(
        f"**{n_ok} facture(s) rapprochée(s)** "
        f"({n_exact} fiable(s), {n_warn} à vérifier), "
        f"**{len(report.unmatched_invoices)} facture(s)** sans transaction, "
        f"**{len(report.unmatched_transactions)} transaction(s)** sans facture."
    )
    out.append("")

    out.append("## Factures rapprochées")
    out.append("")
    out.append("| Facture | Montant | Stratégie | Date facture | Date Qonto | Écart (j) | Statut |")
    out.append("|---|---:|---|---|---|---:|---|")
    for m in sorted(report.matched, key=lambda m: m.invoice.date):
        status = "✅ exact" if m.confidence == "exact" else "⚠️ à vérifier"
        out.append(
            f"| {m.invoice.drive_name} | {_fmt_amount(m.invoice.amount, m.invoice.currency)} | "
            f"{_STRATEGY_LABEL.get(m.match_strategy, m.match_strategy)} | "
            f"{m.invoice.date} | {m.transaction.settled_at} | {m.date_gap_days} | {status} |"
        )
    out.append("")

    out.append("## Factures sans transaction")
    out.append("")
    out.append("| Facture | Montant | Date | Raison |")
    out.append("|---|---:|---|---|")
    for inv in sorted(report.unmatched_invoices, key=lambda i: (i.date, i.drive_name)):
        reason = inv.error or "aucune transaction correspondante"
        amount = _fmt_amount(inv.amount, inv.currency) if inv.amount > 0 else "—"
        out.append(f"| {inv.drive_name} | {amount} | {inv.date or '—'} | {reason} |")
    out.append("")

    out.append("## Transactions Qonto sans facture")
    out.append("")
    out.append("| Libellé | Montant | Devise | Date | ID transaction |")
    out.append("|---|---:|---|---|---|")
    for t in sorted(report.unmatched_transactions, key=lambda t: t.settled_at):
        dev = t.local_currency or "EUR"
        amt = _fmt_amount(t.local_amount if t.local_amount else t.amount, dev)
        out.append(f"| {t.label} | {amt} | {dev} | {t.settled_at} | {t.id} |")
    out.append("")

    out.append("---")
    out.append(
        f"_Généré par `scripts/reconcile_fournisseurs.py` le {report.run_date}. "
        f"Pièces jointes à attacher : voir `matches.json`._"
    )
    path.write_text("\n".join(out), encoding="utf-8")


def print_summary(report: ReconciliationReport) -> None:
    n_exact = sum(1 for m in report.matched if m.confidence == "exact")
    n_warn = len(report.matched) - n_exact
    print(
        f"{len(report.matched)} rapprochée(s) "
        f"({n_exact} fiable(s), {n_warn} à vérifier) · "
        f"{len(report.unmatched_invoices)} facture(s) orpheline(s) · "
        f"{len(report.unmatched_transactions)} transaction(s) orpheline(s)",
        file=sys.stderr,
    )
    for m in report.matched:
        flag = "  " if m.confidence == "exact" else "⚠ "
        print(
            f"  {flag}{m.invoice.drive_name[:28]:<28} "
            f"{_fmt_amount(m.invoice.amount, m.invoice.currency):>12} "
            f"[{_STRATEGY_LABEL.get(m.match_strategy, m.match_strategy)}] "
            f"↔ {m.transaction.settled_at} (écart {m.date_gap_days} j)",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rapproche les factures fournisseurs (Drive) avec les transactions Qonto."
    )
    parser.add_argument("--input", default="reports/fournisseurs/input")
    parser.add_argument("--transactions", default="reports/fournisseurs/qonto_debits.json")
    parser.add_argument("--out-dir", default="reports/fournisseurs")
    parser.add_argument("--since", default=DEFAULT_SINCE,
                        help=f"Ignore les factures avant cette date (défaut {DEFAULT_SINCE}).")
    parser.add_argument("--warn-days", type=int, default=DEFAULT_WARN_DAYS)
    parser.add_argument("--dry-run", action="store_true",
                        help="Affiche le parsing sans écrire de fichier.")
    args = parser.parse_args()

    input_dir = Path(args.input)
    out_dir = Path(args.out_dir)
    if not input_dir.exists():
        raise SystemExit(f"Dossier d'entrée introuvable : {input_dir}")

    invoices = load_invoices(input_dir)
    invoices = [i for i in invoices if not i.date or i.date >= args.since]

    if not invoices:
        print("Aucune facture fournisseur trouvée.", file=sys.stderr)
        return 0

    if args.dry_run:
        ok = sum(1 for i in invoices if i.is_valid)
        print(f"[dry-run] {len(invoices)} facture(s), {ok} lisible(s) :", file=sys.stderr)
        for inv in invoices:
            note = f"  ⚠ {inv.error}" if inv.error else ""
            print(f"  {inv.drive_name[:30]:<30} {inv.date or '—':<12} "
                  f"{_fmt_amount(inv.amount, inv.currency):>12}{note}", file=sys.stderr)
        return 0

    transactions_path = Path(args.transactions)
    if not transactions_path.exists():
        raise SystemExit(
            f"Transactions Qonto introuvables : {transactions_path}\n"
            "Lancer d'abord run_fournisseurs.py (étape 2) pour les récupérer."
        )
    debits = load_qonto_debits(transactions_path)

    report = match(invoices, debits, args.warn_days)

    from datetime import datetime
    report.run_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    today = report.run_date[:10]

    out_dir.mkdir(parents=True, exist_ok=True)
    write_matches_json(report, out_dir / "matches.json")
    write_reconciliation_report(report, out_dir / f"reconciliation_{today}.md")

    print_summary(report)
    print(f"\nRapport : {out_dir / f'reconciliation_{today}.md'}", file=sys.stderr)
    print(f"À attacher : {out_dir / 'matches.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
