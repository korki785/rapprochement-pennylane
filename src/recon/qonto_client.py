"""Client minimal de l'API Qonto third-party.

Auth : Authorization: {organization_slug}:{secret_key}
Base : https://thirdparty.qonto.com/v2

Utilise curl (subprocess) au lieu de urllib pour contourner la détection
TLS de Cloudflare qui bloque les requêtes Python (erreur 1010).
"""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from .config import load_dotenv

DEFAULT_BASE_URL = "https://thirdparty.qonto.com/v2"
_MAX_RETRIES = 4
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class QontoError(RuntimeError):
    pass


def load_qonto_credentials() -> tuple[str, str]:
    """Renvoie (organization_slug, secret_key) depuis le .env."""
    load_dotenv()
    slug = os.environ.get("QONTO_ORGANIZATION_SLUG", "").strip()
    key = os.environ.get("QONTO_SECRET_KEY", "").strip()
    if not slug or not key:
        raise SystemExit(
            "QONTO_ORGANIZATION_SLUG / QONTO_SECRET_KEY manquants. "
            "Les récupérer dans Qonto : Paramètres > Intégrations > API, "
            "puis les ajouter au fichier .env."
        )
    return slug, key


class QontoClient:
    def __init__(self, slug: str, secret_key: str, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = 30.0):
        self._auth = f"{slug}:{secret_key}"
        self._base = base_url.rstrip("/")
        self._timeout = int(timeout)
        if not shutil.which("curl"):
            raise SystemExit("curl introuvable. Installer via brew install curl.")

    # -- HTTP bas niveau (curl) ---------------------------------------------
    def _request(self, method: str, url: str, body: Optional[bytes] = None,
                 content_type: Optional[str] = None, accept_json: bool = True) -> Dict:
        if url.startswith("/"):
            url = self._base + url
        for attempt in range(_MAX_RETRIES):
            cmd = [
                "curl", "-s", "-w", "\n__STATUS__%{http_code}",
                "-X", method,
                "-H", f"Authorization: {self._auth}",
                "--max-time", str(self._timeout),
            ]
            if accept_json:
                cmd += ["-H", "Accept: application/json"]
            if content_type:
                cmd += ["-H", f"Content-Type: {content_type}"]
            if body is not None:
                tmp = None
                try:
                    with tempfile.NamedTemporaryFile(delete=False) as f:
                        f.write(body)
                        tmp = f.name
                    cmd += ["--data-binary", f"@{tmp}"]
                    result = subprocess.run(cmd + [url], capture_output=True, text=True)
                finally:
                    if tmp:
                        Path(tmp).unlink(missing_ok=True)
            else:
                result = subprocess.run(cmd + [url], capture_output=True, text=True)

            output = result.stdout
            status = 200
            if "\n__STATUS__" in output:
                body_part, status_part = output.rsplit("\n__STATUS__", 1)
                try:
                    status = int(status_part.strip())
                except ValueError:
                    pass
                output = body_part
            else:
                output = output

            if status in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                time.sleep(min(2 ** attempt, 10))
                continue
            if status >= 400:
                raise QontoError(f"{method} {url} -> HTTP {status}: {output[:500]}")
            return json.loads(output) if output.strip() else {}
        raise QontoError(f"{method} {url} a échoué après {_MAX_RETRIES} tentatives")

    def get(self, url: str) -> Dict:
        return self._request("GET", url)

    # -- Comptes ------------------------------------------------------------
    def list_bank_account_ids(self) -> List[str]:
        """IDs de tous les comptes bancaires de l'organisation."""
        data = self.get("/organization")
        org = data.get("organization", data)
        return [str(a.get("id")) for a in org.get("bank_accounts", []) if a.get("id")]

    # -- Transactions -------------------------------------------------------
    def iter_transactions(self, bank_account_id: str,
                          page_size: int = 100) -> Iterator[Dict]:
        """Itère les transactions d'un compte (pagination Qonto `meta`)."""
        page = 1
        while True:
            q = urllib.parse.urlencode({
                "bank_account_id": bank_account_id,
                "per_page": str(page_size),
                "current_page": str(page),
            })
            data = self.get("/transactions?" + q)
            for tx in data.get("transactions", []):
                yield tx
            meta = data.get("meta", {})
            if not meta.get("next_page"):
                return
            page = meta["next_page"]

    def fetch_reimbursement_transfers(
        self, since: str, label_contains: Optional[List[str]] = None,
    ) -> List[Dict]:
        """Virements sortants (débits) dont le libellé contient un des termes.

        Renvoie des dicts {id, label, amount, settled_at} prêts pour
        reconcile_ubereats.load_qonto_transfers.
        """
        terms = [t.lower() for t in (label_contains or ["nael", "darwish"])]
        out: List[Dict] = []
        for acct in self.list_bank_account_ids():
            for tx in self.iter_transactions(acct):
                settled = str(tx.get("settled_at") or tx.get("emitted_at") or "")[:10]
                if settled and settled < since:
                    continue
                if tx.get("side") != "debit":
                    continue
                label = str(tx.get("label") or "")
                if terms and not any(t in label.lower() for t in terms):
                    continue
                out.append({
                    "id": str(tx.get("id") or tx.get("transaction_id") or ""),
                    "label": label,
                    "amount": tx.get("amount"),
                    "currency": tx.get("currency") or "EUR",
                    "settled_at": settled,
                })
        return out

    def fetch_all_debits(
        self, since: str, operation_types: Optional[List[str]] = None,
    ) -> List[Dict]:
        """Tous les débits depuis `since`, toutes opérations confondues.

        Contrairement à fetch_reimbursement_transfers (filtré sur le libellé),
        renvoie tous les débits avec les champs nécessaires au rapprochement
        fournisseurs, dont local_amount / local_currency pour les paiements en
        devise étrangère.

        `operation_types` : filtre optionnel, ex. ["card", "transfer"]. None = tout.
        """
        out: List[Dict] = []
        for acct in self.list_bank_account_ids():
            for tx in self.iter_transactions(acct):
                if tx.get("side") != "debit":
                    continue
                settled = str(tx.get("settled_at") or tx.get("emitted_at") or "")[:10]
                if settled and settled < since:
                    continue
                op_type = str(tx.get("operation_type") or "")
                if operation_types and op_type not in operation_types:
                    continue
                out.append({
                    "id": str(tx.get("id") or tx.get("transaction_id") or ""),
                    "label": str(tx.get("label") or ""),
                    "amount": tx.get("amount"),
                    "currency": tx.get("currency") or "EUR",
                    "local_amount": tx.get("local_amount"),
                    "local_currency": tx.get("local_currency") or "",
                    "settled_at": settled,
                    "operation_type": op_type,
                })
        return out

    # -- Pièces jointes -----------------------------------------------------
    def get_transaction_attachments(self, transaction_id: str) -> List[Dict]:
        """Liste les pièces jointes d'une transaction (vide si aucune).

        Permet d'éviter d'attacher un PDF déjà présent.
        """
        data = self.get(f"/transactions/{transaction_id}/attachments")
        return data.get("attachments", [])

    def upload_attachment(self, transaction_id: str, pdf_path: Path) -> Dict:
        """Attache un PDF à une transaction (POST .../attachments, multipart).

        Champ `file`, comme l'upload Pennylane.
        """
        content = pdf_path.read_bytes()
        mime = mimetypes.guess_type(pdf_path.name)[0] or "application/pdf"
        boundary = "----reconqontoboundary"
        parts = [
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{pdf_path.name}"'.encode(),
            f"Content-Type: {mime}".encode(),
            b"",
            content,
            f"--{boundary}--".encode(),
            b"",
        ]
        body = b"\r\n".join(parts)
        return self._request(
            "POST", f"/transactions/{transaction_id}/attachments", body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
