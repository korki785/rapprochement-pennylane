"""Client minimal de l'API Pennylane v2 (stdlib uniquement : urllib).

Couvre les besoins du rapprochement : lister les transactions, savoir si une
transaction a une facture rapprochée, uploader un fichier et importer une facture.
Gère la pagination cursor et un backoff simple sur 429 / 5xx.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Iterator, List, Optional

_MAX_RETRIES = 8
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class PennylaneError(RuntimeError):
    pass


def _parse_retry_after(err: "urllib.error.HTTPError") -> Optional[float]:
    """Lit l'en-tête Retry-After (en secondes) d'une réponse 429, si présent."""
    value = err.headers.get("Retry-After") if err.headers else None
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class _RateLimiter:
    """Garantit un intervalle minimum entre deux départs de requête (thread-safe)."""

    def __init__(self, min_interval: float):
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = self._next_allowed - now
            if sleep_for > 0:
                time.sleep(sleep_for)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval

    def backoff_until(self, seconds: float) -> None:
        """Repousse la prochaine requête autorisée d'au moins `seconds` (après un 429)."""
        with self._lock:
            self._next_allowed = max(self._next_allowed, time.monotonic() + seconds)


class PennylaneClient:
    def __init__(self, token: str, base_url: str, timeout: float = 30.0,
                 requests_per_second: float = 3.0):
        self._token = token
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._limiter = _RateLimiter(1.0 / max(requests_per_second, 0.5))

    # -- HTTP bas niveau ----------------------------------------------------
    def _request(self, method: str, url: str, body: Optional[bytes] = None,
                 content_type: Optional[str] = None) -> Dict:
        if url.startswith("/"):
            url = self._base + url
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        last_err: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            self._limiter.wait()
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                    retry_after = _parse_retry_after(e)
                    self._limiter.backoff_until(retry_after if retry_after else min(2 ** attempt, 10))
                    last_err = e
                    continue
                detail = e.read().decode("utf-8", "replace")[:500]
                raise PennylaneError(f"{method} {url} -> HTTP {e.code}: {detail}") from e
            except urllib.error.URLError as e:
                last_err = e
                self._limiter.backoff_until(min(2 ** attempt, 10))
        raise PennylaneError(f"{method} {url} a échoué après {_MAX_RETRIES} tentatives: {last_err}")

    def get(self, url: str) -> Dict:
        return self._request("GET", url)

    # -- Transactions -------------------------------------------------------
    def iter_transactions(self, page_size: int = 100) -> Iterator[Dict]:
        """Itère toutes les transactions (pagination cursor).

        Le filtrage par date se fait côté client (cf. transactions.find_unmatched),
        la syntaxe du filtre serveur v2 n'étant pas garantie.
        """
        params: Dict[str, str] = {"limit": str(page_size)}
        cursor: Optional[str] = None
        while True:
            q = dict(params)
            if cursor:
                q["cursor"] = cursor
            data = self.get("/transactions?" + urllib.parse.urlencode(q))
            for item in data.get("items", []):
                yield item
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")
            if not cursor:
                return

    def iter_supplier_invoices(self, page_size: int = 100) -> Iterator[Dict]:
        """Itère toutes les factures fournisseurs (pagination cursor)."""
        params: Dict[str, str] = {"limit": str(page_size)}
        cursor: Optional[str] = None
        while True:
            q = dict(params)
            if cursor:
                q["cursor"] = cursor
            data = self.get("/supplier_invoices?" + urllib.parse.urlencode(q))
            for item in data.get("items", []):
                yield item
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")
            if not cursor:
                return

    def delete_supplier_invoice(self, invoice_id) -> None:
        """Supprime une facture fournisseur (DELETE /supplier_invoices/{id})."""
        self._request("DELETE", "/supplier_invoices/" + str(invoice_id))

    def matched_invoices(self, transaction: Dict) -> List[Dict]:
        """Renvoie la liste des factures rapprochées à une transaction (vide = aucune)."""
        link = (transaction.get("matched_invoices") or {}).get("url")
        if not link:
            return []
        items: List[Dict] = []
        url: Optional[str] = link
        while url:
            data = self.get(url)
            items.extend(data.get("items", []))
            url = data.get("next_cursor") and (link + ("&" if "?" in link else "?") +
                                               "cursor=" + urllib.parse.quote(data["next_cursor"]))
            if not data.get("has_more"):
                break
        return items

    def has_matched_invoice(self, transaction: Dict) -> bool:
        return bool(self.matched_invoices(transaction))

    # -- Import de factures (Phase A, écriture) -----------------------------
    def upload_file_attachment(self, filename: str, content: bytes) -> Dict:
        """POST /file_attachments en multipart. Renvoie l'objet créé (avec son id)."""
        boundary = "----reconpennylaneboundary"
        parts = [
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode(),
            b"Content-Type: application/octet-stream",
            b"",
            content,
            f"--{boundary}--".encode(),
            b"",
        ]
        body = b"\r\n".join(parts)
        return self._request("POST", "/file_attachments", body=body,
                             content_type=f"multipart/form-data; boundary={boundary}")

    def import_supplier_invoice(self, payload: Dict) -> Dict:
        """POST /supplier_invoices/import. `payload` suit le schéma documenté
        (file_attachment_id + éventuel transaction_reference pour l'auto-matching)."""
        return self._request("POST", "/supplier_invoices/import",
                             body=json.dumps(payload).encode("utf-8"),
                             content_type="application/json")


def build_invoice_payload(file_attachment_id, txn, amount_foreign: Optional[float] = None) -> Dict:
    """Construit le payload d'import en gérant le multi-devises.

    `txn` est une `transactions.UnmatchedTransaction`. Pour une transaction réglée
    dans une devise étrangère (ex. USD facturé, EUR débité), on transmet la devise
    d'origine + le taux de change réel calculé depuis Qonto, ce qui permet à
    Pennylane de rapprocher la facture USD avec la transaction EUR.

    `amount_foreign` : montant de la facture dans sa devise d'origine (extrait de
    l'email/PDF). Si omis, on retombe sur `txn.local_amount` (montant Qonto).
    """
    payload: Dict = {
        "file_attachment_id": file_attachment_id,
        "transaction_reference": txn.id,
    }
    if txn.is_foreign_currency:
        amount = amount_foreign if amount_foreign is not None else abs(txn.local_amount)
        payload["currency"] = txn.local_currency.upper()
        payload["amount"] = round(abs(amount), 2)
        payload["exchange_rate"] = round(txn.exchange_rate, 6)
    return payload
