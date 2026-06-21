"""Normalisation des libellés bancaires et regroupement fuzzy par fournisseur.

Module volontairement SANS dépendance obligatoire : si `rapidfuzz` est installé
il est utilisé (plus rapide et meilleur sur les tokens réordonnés), sinon on
retombe sur `difflib` de la stdlib. Tout est déterministe et testable hors ligne.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Sequence

try:  # rapidfuzz est optionnel (cf. pyproject : dépendance best-effort)
    from rapidfuzz.fuzz import token_sort_ratio as _rapid_token_sort_ratio

    _HAVE_RAPIDFUZZ = True
except Exception:  # pragma: no cover - dépend de l'environnement
    _HAVE_RAPIDFUZZ = False


# Préfixes/jetons « bruit bancaire » qui ne désignent pas le fournisseur.
_NOISE_TOKENS = {
    "CB", "CARTE", "PAIEMENT", "PAIMENT", "ACHAT", "ACHATS",
    "PRLV", "PRELEVEMENT", "PRELVT", "PREL",
    "VIR", "VIREMENT", "VRST", "VERSEMENT",
    "SEPA", "EUROPEEN", "EUROPEENNE",
    "FACTURE", "FACT", "FACTU",
    "RETRAIT", "DAB", "DEPOT",
    "REMISE", "ECH", "ECHEANCE",
}

# Suffixes de forme juridique à retirer en fin de nom.
_LEGAL_SUFFIXES = {
    "SAS", "SASU", "SARL", "SA", "EURL", "SCI", "SNC",
    "LTD", "LIMITED", "INC", "LLC", "CORP", "CO", "GMBH", "AG", "BV", "PLC",
}

# Motifs purgés du libellé avant tokenisation.
_DATE_RE = re.compile(r"\b\d{1,4}[-/.]\d{1,2}([-/.]\d{1,4})?\b")  # 12/03, 2025-01-04
_CARD_MASK_RE = re.compile(r"\b[X*]{2,}\d{2,}\b", re.IGNORECASE)   # ****1234, XXXX12
_LONG_NUM_RE = re.compile(r"\b\d{3,}\b")                           # références numériques
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9\s]")


def strip_accents(text: str) -> str:
    """Retire les accents (é -> e) pour une comparaison stable."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_label(label: str) -> str:
    """Nettoie un libellé de transaction pour ne garder que le nom du fournisseur.

    Majuscules, sans accents, sans dates/numéros/masques de carte, sans préfixes
    bancaires ni suffixes juridiques. Renvoie une chaîne (potentiellement vide).
    """
    if not label:
        return ""
    text = strip_accents(label).upper()
    text = _DATE_RE.sub(" ", text)
    text = _CARD_MASK_RE.sub(" ", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    text = _LONG_NUM_RE.sub(" ", text)

    tokens = [t for t in text.split() if t]
    # Retirer les préfixes bruit tant qu'ils apparaissent en tête.
    while tokens and tokens[0] in _NOISE_TOKENS:
        tokens.pop(0)
    # Retirer un éventuel suffixe juridique en fin.
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    # Retirer le bruit résiduel et les jetons d'un seul caractère.
    tokens = [t for t in tokens if t not in _NOISE_TOKENS and len(t) > 1]
    return " ".join(tokens)


def _token_sort(text: str) -> str:
    return " ".join(sorted(text.split()))


def ratio(a: str, b: str) -> float:
    """Similarité 0..100, insensible à l'ordre des mots.

    Utilise rapidfuzz si disponible, sinon difflib sur des chaînes token-triées.
    """
    if not a and not b:
        return 100.0
    if not a or not b:
        return 0.0
    if _HAVE_RAPIDFUZZ:
        return float(_rapid_token_sort_ratio(a, b))
    from difflib import SequenceMatcher

    return SequenceMatcher(None, _token_sort(a), _token_sort(b)).ratio() * 100.0


@dataclass
class SupplierGroup:
    """Un fournisseur déduit d'un ensemble de libellés similaires."""

    canonical: str
    members: List[str] = field(default_factory=list)  # libellés normalisés
    indices: List[int] = field(default_factory=list)  # indices d'origine


def group_labels(labels: Sequence[str], threshold: float = 85.0) -> List[SupplierGroup]:
    """Regroupe des libellés (bruts) par fournisseur via clustering glouton.

    Chaque libellé est normalisé puis rattaché au premier cluster dont le
    représentant dépasse `threshold`, sinon il ouvre un nouveau cluster.
    Le `canonical` d'un cluster est le libellé normalisé le plus fréquent.
    """
    groups: List[SupplierGroup] = []
    for idx, raw in enumerate(labels):
        norm = normalize_label(raw)
        if not norm:
            norm = "(INCONNU)"
        best: SupplierGroup = None  # type: ignore[assignment]
        best_score = 0.0
        for group in groups:
            score = ratio(norm, group.canonical)
            if score > best_score:
                best_score = score
                best = group
        if best is not None and best_score >= threshold:
            best.members.append(norm)
            best.indices.append(idx)
            best.canonical = _most_common(best.members)
        else:
            groups.append(SupplierGroup(canonical=norm, members=[norm], indices=[idx]))
    return groups


def _most_common(values: Sequence[str]) -> str:
    counts: Dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    # Tri stable : fréquence décroissante puis ordre alphabétique pour le déterminisme.
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
