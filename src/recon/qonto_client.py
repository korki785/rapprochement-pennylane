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
                 timeout: float = 60.0):     # 30 s coupait des pages de 100 transactions
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
            truncated = False
            if "\n__STATUS__" in output:
                body_part, status_part = output.rsplit("\n__STATUS__", 1)
                try:
                    status = int(status_part.strip())
                except ValueError:
                    pass
                output = body_part
            else:
                # Le marqueur de fin manque => curl est mort AVANT d'écrire la fin (timeout
                # `--max-time`, connexion coupée) : le corps est TRONQUÉ. Sans ce test, le JSON
                # partiel partait au parseur et remontait en `JSONDecodeError` illisible au lieu
                # d'être re-tenté (constaté 2026-07-20 : « Unterminated string ... char 61805 »).
                truncated = True

            if (truncated or result.returncode != 0) and attempt < _MAX_RETRIES - 1:
                time.sleep(min(2 ** attempt, 10))
                continue
            if truncated or result.returncode != 0:
                raise QontoError(
                    f"{method} {url} : réponse TRONQUÉE (curl code {result.returncode}, "
                    f"{len(output)} octets reçus). Réseau lent ou timeout "
                    f"({self._timeout}s) — relancer, ou augmenter `timeout=`.")

            if status in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                time.sleep(min(2 ** attempt, 10))
                continue
            if status >= 400:
                raise QontoError(f"{method} {url} -> HTTP {status}: {output[:500]}")
            try:
                return json.loads(output) if output.strip() else {}
            except json.JSONDecodeError as exc:
                # Corps complet selon curl mais illisible : re-tenter plutôt que planter.
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                raise QontoError(
                    f"{method} {url} : réponse JSON illisible ({exc}). "
                    f"Début : {output[:200]}") from exc
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

    def fetch_all_credits(
        self, since: str, operation_types: Optional[List[str]] = None,
    ) -> List[Dict]:
        """Tous les crédits (recettes) depuis `since`, toutes opérations confondues.

        Symétrique de fetch_all_debits mais côté `side == "credit"` : virements
        entrants (income), virements SWIFT (swift_income), etc. Conserve
        local_amount / local_currency (virement en devise) et le statut PJ LIVE
        (attachment_ids / attachment_required) pour l'idempotence côté recettes.

        `operation_types` : filtre optionnel, ex. ["income", "swift_income"]. None = tout.
        """
        out: List[Dict] = []
        for acct in self.list_bank_account_ids():
            for tx in self.iter_transactions(acct):
                if tx.get("side") != "credit":
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
                    "attachment_required": bool(tx.get("attachment_required")),
                    "attachment_ids": tx.get("attachment_ids") or [],
                })
        return out

    def fetch_unreconciled_expenses(self, since: str) -> List[Dict]:
        """Débits « à justifier » depuis `since`, AVEC statut PJ LIVE.

        Périmètre (récap fiable) : CB + prélèvements + virements SAUF internes
        « MAISON DARWISH » ; exclut les frais Qonto. Inclut donc remboursements perso
        (Nael Darwish) et virements fournisseurs. Conserve `attachment_required` et
        `attachment_ids` bruts (le champ que `fetch_all_debits` laisse tomber) → vérité
        terrain, immunisé contre les snapshots périmés.
        """
        out: List[Dict] = []
        for acct in self.list_bank_account_ids():
            for tx in self.iter_transactions(acct):
                if tx.get("side") != "debit":
                    continue
                settled = str(tx.get("settled_at") or tx.get("emitted_at") or "")[:10]
                if settled and settled < since:
                    continue
                op = str(tx.get("operation_type") or "")
                label = str(tx.get("label") or "")
                if op == "qonto_fee":
                    continue
                if op == "transfer" and "maison darwish" in label.lower():
                    continue  # mouvement interne / dirigeant — pas un justificatif requis
                if op not in ("card", "direct_debit", "transfer"):
                    continue
                out.append({
                    "id": str(tx.get("id") or tx.get("transaction_id") or ""),
                    "label": label,
                    "amount": tx.get("amount"),
                    "currency": tx.get("currency") or "EUR",
                    "local_amount": tx.get("local_amount"),
                    "local_currency": tx.get("local_currency") or "",
                    "settled_at": settled,
                    "operation_type": op,
                    # Sources de nom marchand hors libellé (cf. normalize.tx_merchant_tokens) :
                    # le motif du virement et le nom nettoyé par Qonto.
                    "reference": str(tx.get("reference") or ""),
                    "clean_counterparty_name": str(tx.get("clean_counterparty_name") or ""),
                    "attachment_required": bool(tx.get("attachment_required")),
                    "attachment_ids": tx.get("attachment_ids") or [],
                })
        return out

    # -- Factures clients (recettes) ---------------------------------------
    def list_client_invoices(self, status: Optional[List[str]] = None,
                             page_size: int = 100) -> List[Dict]:
        """Toutes les factures clients émises (pagination Qonto `meta`).

        Renvoie les champs utiles au rapprochement recettes : id, number, statut,
        total (`total_amount.value`), devise, `attachment_id` (= PDF de la facture)
        et `client.name`. `status` : filtre optionnel (ex. ["unpaid", "paid"]).
        """
        out: List[Dict] = []
        page = 1
        while True:
            params = {"per_page": str(page_size), "page": str(page)}
            if status:
                params["filter[status]"] = ",".join(status)
            q = urllib.parse.urlencode(params)
            data = self.get("/client_invoices?" + q)
            for inv in data.get("client_invoices", []):
                total = inv.get("total_amount") or {}
                client = inv.get("client") or {}
                out.append({
                    "id": str(inv.get("id") or ""),
                    "number": str(inv.get("number") or ""),
                    "status": str(inv.get("status") or ""),
                    "client_name": str(client.get("name") or ""),
                    "total_amount": total.get("value"),
                    "currency": str(inv.get("currency") or total.get("currency") or "EUR"),
                    "attachment_id": str(inv.get("attachment_id") or ""),
                    "issue_date": str(inv.get("issue_date") or "")[:10],
                    "paid_at": str(inv.get("paid_at") or "")[:10],
                })
            meta = data.get("meta", {})
            if not meta.get("next_page"):
                return out
            page = meta["next_page"]

    def get_attachment_url(self, attachment_id: str) -> str:
        """URL présignée (S3, ~30 min) du fichier d'une pièce jointe / facture."""
        data = self.get(f"/attachments/{attachment_id}")
        return str((data.get("attachment") or {}).get("url") or "")

    def download_url(self, url: str, dest: Path) -> Path:
        """Télécharge une URL (ex. présignée S3, sans auth) vers `dest` via curl."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["curl", "-s", "-L", "--max-time", str(self._timeout), "-o", str(dest), url],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
            raise QontoError(f"Téléchargement échoué ({url[:80]}…) : {result.stderr[:200]}")
        return dest

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
