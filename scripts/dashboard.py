#!/usr/bin/env python3
"""Cockpit local de rapprochement — serveur stdlib (aucune dépendance pip).

    python3 scripts/dashboard.py           # → http://127.0.0.1:8787
    python3 scripts/dashboard.py --port 9000

3 panneaux : santé des sessions portails, vue rapprochement par flux, récap Qonto
non justifié avec bouton « retenter ». Actions : re-login headed, sonde live, retenter
(dry-run → proposition → confirmer → attacher), ajouter un abonnement.

Sécurité : bind 127.0.0.1 UNIQUEMENT. L'exposition internet passe par un tunnel
authentifié (Cloudflare Tunnel + Access), jamais 0.0.0.0. Voir scripts/tunnel/README.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from recon import health  # noqa: E402
from recon.config import load_dotenv  # noqa: E402

PY = "/Users/naeldarwish/Library/Python/3.9/bin/python3"
if not Path(PY).exists():
    PY = "/usr/bin/python3"

ENV_PATH = ROOT / ".env"
PORTAL_CSV = ROOT / "src" / "recon" / "portal_vendors.csv"
SAAS_CSV = ROOT / "src" / "recon" / "saas_senders.csv"
QONTO_PROCESSED = ROOT / "reports" / "qonto" / "processed.json"
SINCE = "2026-04-01"
COCKPIT_DIR = ROOT / "reports" / "cockpit"
IGNORED_PATH = COCKPIT_DIR / "ignored.json"   # tx masquées localement (non rapprochables)

# Redirection vers une page de login → session morte.
_LOGIN_PAT = re.compile(r"/(login|sign[_-]?in|identification|connexion|auth|users/sign)", re.I)

_qonto = None
_qonto_lock = threading.Lock()


def qonto():
    """QontoClient paresseux (partagé)."""
    global _qonto
    with _qonto_lock:
        if _qonto is None:
            from recon.qonto_client import QontoClient, load_qonto_credentials
            _qonto = QontoClient(*load_qonto_credentials())
        return _qonto


# --- .env upsert -----------------------------------------------------------

def env_set(pairs: dict) -> None:
    """Écrit/met à jour des clés dans .env (préserve le reste). Plaintext, local Mac."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    keys = dict(pairs)
    out = []
    for ln in lines:
        m = re.match(r"\s*([A-Z0-9_]+)\s*=", ln)
        if m and m.group(1) in keys:
            out.append(f"{m.group(1)}={keys.pop(m.group(1))}")
        else:
            out.append(ln)
    for k, v in keys.items():
        out.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


# --- Sonde live (navigateur headless) --------------------------------------

def probe_session(handler_key: str) -> dict:
    """Vérité terrain : charge la session, ouvre billing_url, détecte une redirection login."""
    if handler_key in health._CDP_VENDORS:
        up = health._cdp_reachable()
        return {"handler_key": handler_key, "alive": up,
                "detail": "CDP 9222 " + ("ok" if up else "injoignable")}

    vendors = {v["handler_key"]: v for v in health.list_portal_vendors()}
    v = vendors.get(handler_key)
    if not v:
        return {"handler_key": handler_key, "alive": False, "detail": "vendeur inconnu"}
    billing = v.get("billing_url") or ""
    sess = health.session_file(handler_key)
    if not sess.exists():
        return {"handler_key": handler_key, "alive": False, "detail": "aucune session"}

    from _portal_base import launch_browser
    pw = browser = ctx = None
    try:
        pw, browser, ctx, page = launch_browser(sess, headed=False)
        page.goto(billing, timeout=60_000)
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2500)
        final = page.url
        alive = not bool(_LOGIN_PAT.search(urlparse(final).path or ""))
        return {"handler_key": handler_key, "alive": alive, "detail": f"url={final}"}
    except Exception as exc:
        return {"handler_key": handler_key, "alive": False, "detail": f"erreur sonde : {exc}"}
    finally:
        for c in (ctx, browser):
            try:
                c and c.close()
            except Exception:
                pass
        try:
            pw and pw.stop()
        except Exception:
            pass


# --- Re-login headed (subprocess détaché) ----------------------------------

def relogin(handler_key: str) -> dict:
    """Lance fetch_<key>.py --init-session (headed) : une fenêtre s'ouvre sur le Mac."""
    fetcher = ROOT / "scripts" / "portals" / f"fetch_{handler_key}.py"
    if not fetcher.exists():
        fetcher = ROOT / "scripts" / "portals" / "fetch_generic.py"
        args = [PY, str(fetcher), "--vendor", handler_key, "--init-session"]
    else:
        args = [PY, str(fetcher), "--init-session"]
    if not fetcher.exists():
        return {"ok": False, "detail": "aucun fetcher (ni spécifique ni générique)"}
    subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "detail": "navigateur lancé sur le Mac — connectez-vous, la session sera sauvegardée."}


# --- Retenter (dry-run) : réutilise Flux 6 pour UNE transaction -------------

def retry_tx(tx: dict) -> dict:
    """Retente le rapprochement d'UNE tx. N'ATTACHE RIEN.

    Routage par type de vendeur : portail (Bouygues/Wix/Notion…) → scrape flux 5 ;
    sinon → flux 6 (Gmail + HTML→PDF) puis repli Drive (flux 3).
    """
    load_dotenv()
    tx = dict(tx)
    tx.setdefault("settled_at", tx.get("date") or "")

    # Vendeur portail ? (libellé Qonto ↔ portal_vendors.csv) → chemin flux 5.
    from recon.portals import match_vendor, load_portal_vendors
    vendor = match_vendor(tx, load_portal_vendors())
    if vendor:
        return retry_portal(tx, vendor)

    import reconcile_qonto as rq
    from recon.normalize import tx_merchant_tokens

    tokens = tx_merchant_tokens(tx)
    if not tokens:
        return {"status": "no_token", "detail": "aucun jeton marchand dans le libellé"}
    boxes = rq._connect_boxes()
    if not boxes:
        return {"status": "error", "detail": "aucune boîte Gmail configurée"}
    try:
        cands = rq.collect_verified_receipts(tx, boxes, tokens)
    finally:
        for box in boxes:
            box.logout()

    if cands:
        n_emails = len({c["msgid"] for c in cands})
        if n_emails == 1:
            best = next((c for c in cands
                         if rq.saas.looks_like_invoice(rq.audit._pdftotext(c["pdf"]))), cands[0])
            return {"status": "found", "pdf_path": str(best["pdf"]),
                    "source": best["tag"], "pdf_name": Path(best["pdf"]).name}
        # ≥2 emails distincts → on présente le choix au lieu d'abandonner.
        return {"status": "choose", "candidates": [_candidate(c) for c in cands]}

    drive = _retry_drive(tx)
    if drive:
        return drive
    return {"status": "none", "detail": "aucun justificatif trouvé (email/HTML/Drive)"}


def _candidate(c: dict) -> dict:
    """Fiche candidat pour la sélection manuelle (aperçu court + méta email)."""
    import reconcile_qonto as rq
    try:
        snippet = " ".join(rq.audit._pdftotext(c["pdf"]).split())[:160]
    except Exception:
        snippet = ""
    return {"pdf_path": str(c["pdf"]), "pdf_name": Path(c["pdf"]).name,
            "source": c["tag"], "sender": c.get("sender", ""),
            "subject": c.get("subject", ""), "snippet": snippet}


FOURN_INPUT = ROOT / "reports" / "fournisseurs" / "input"


def _amount_regexes(tx: dict):
    """Regex du/des montant(s) de la tx (EUR + devise), formats '80.99' et '80,99'."""
    vals = []
    for v in (tx.get("amount"), tx.get("local_amount")):
        try:
            f = round(float(v), 2)
            if f > 0:
                vals.append(f)
        except (TypeError, ValueError):
            pass
    pats = []
    for f in set(vals):
        s = f"{f:.2f}"                       # 80.99
        body = re.escape(s).replace("\\.", "[.,]")
        pats.append(re.compile(rf"(?<!\d){body}(?!\d)"))
    return pats


def _drive_snippet(pdf) -> str:
    import reconcile_fournisseurs as rf
    try:
        return " ".join(rf._extract_text(pdf).split())[:160]
    except Exception:
        return ""


def _sync_drive_pdfs() -> list:
    """Télécharge les PDF Drive manquants (dossier fournisseurs). Renvoie les nouveaux chemins."""
    import os
    from recon.drive_client import DriveClient
    dc = DriveClient(ROOT / "credentials.json", ROOT / "token.json")
    dc.authenticate(headless=True)
    fid = dc.find_folder_id(os.environ.get("DRIVE_FOLDER_NAME", "Factures fournisseurs"))
    FOURN_INPUT.mkdir(parents=True, exist_ok=True)
    newly = []
    for f in dc.list_pdfs(fid):
        name = "".join(ch for ch in f["name"] if ch not in '/\\:*?"<>|') or f["id"]
        dest = FOURN_INPUT / name
        if not dest.exists():
            try:
                dc.download_pdf(f["id"], dest)
                newly.append(dest)
            except Exception:
                pass
    return newly


def _retry_drive(tx: dict) -> dict | None:
    """Cherche un justificatif dans Google Drive (dossier fournisseurs) au bon montant (Flux 3).

    Match par MONTANT (EUR ou devise) dans le texte OCR — pour les factures sans nom marchand
    lisible (ex. Air France : codes de réservation, pas « Air France » en clair). L'humain
    tranche via Confirmer/Voir. Plusieurs correspondances → sélection.

    Rapide : grep le cache OCR déjà calculé (instantané) + OCR SEULEMENT les fichiers
    fraîchement uploadés (pas tout le backlog).
    """
    pats = _amount_regexes(tx)
    if not pats:
        return None
    try:
        _sync_drive_pdfs()                  # récupère les uploads récents
    except Exception:
        pass                                # sans réseau Drive : on grep au moins le cache
    import reconcile_fournisseurs as rf
    cache_dir = ROOT / "reports" / "fournisseurs" / ".ocr_cache"
    cands = []
    seen = set()
    # 1. Cache OCR existant (lecture texte, instantané).
    for txt in cache_dir.glob("*.txt"):
        try:
            t = txt.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(p.search(t) for p in pats):
            pdf = FOURN_INPUT / (txt.stem + ".pdf")
            if pdf.exists() and pdf not in seen:
                seen.add(pdf); cands.append(pdf)
    # 2. Fichiers locaux SANS cache (uploads récents) → OCR ciblé (borné, rare).
    for pdf in sorted(FOURN_INPUT.glob("*.pdf")):
        if pdf in seen or (cache_dir / (pdf.stem + ".txt")).exists():
            continue
        try:
            t = rf._extract_text(pdf)       # écrit le cache
        except Exception:
            continue
        if any(p.search(t) for p in pats):
            seen.add(pdf); cands.append(pdf)
    if not cands:
        return None
    if len(cands) == 1:
        return {"status": "found", "pdf_path": str(cands[0]),
                "pdf_name": cands[0].name, "source": "Drive"}
    return {"status": "choose", "candidates": [
        {"pdf_path": str(p), "pdf_name": p.name, "source": "Drive",
         "sender": "", "subject": "Google Drive", "snippet": _drive_snippet(p)}
        for p in sorted(cands)]}


# --- Retenter PORTAIL (flux 5) : scrape à la demande + match ----------------

def _purge_garbage(key: str) -> None:
    """Vire les vieux PDF 0.00 + entrées manifest sans montant/date (scrapes ratés)."""
    vdir = ROOT / "reports" / "portals" / key
    for pdf in (vdir / "input").glob("*_0.00.pdf"):
        try:
            pdf.unlink()
        except OSError:
            pass
    manifest = vdir / "manifest.json"
    data = health.load_json(manifest)
    if isinstance(data, list):
        clean = [e for e in data if e.get("amount") and e.get("date")]
        if len(clean) != len(data):
            manifest.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")


def retry_portal(tx: dict, vendor: dict) -> dict:
    """Re-télécharge les factures du portail (login déjà frais) puis matche cette tx."""
    key = vendor["handler_key"]
    _purge_garbage(key)

    fetcher = ROOT / "scripts" / "portals" / f"fetch_{key}.py"
    if fetcher.exists():
        cmd = [PY, str(fetcher), "--since", SINCE]
    else:
        cmd = [PY, str(ROOT / "scripts" / "portals" / "fetch_generic.py"), "--vendor", key, "--since", SINCE]
    try:
        subprocess.run(cmd, cwd=str(ROOT), timeout=150,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"status": "none", "detail": "portail : scrape trop long (timeout) — session peut-être morte"}
    except Exception as exc:
        return {"status": "none", "detail": f"portail : scrape impossible ({exc})"}

    # Charge le manifest à jour, garde les factures valides.
    manifest = health.load_json(ROOT / "reports" / "portals" / key / "manifest.json") or []
    invoices = [e for e in manifest if e.get("amount") and e.get("date")]
    if not invoices:
        return {"status": "none",
                "detail": "portail : rien récupéré (session morte, ou extraction du site à corriger)"}

    # Match via le moteur flux 5.
    import reconcile_portals as rp
    from recon.portals import load_portal_vendors
    matches, _ = rp.reconcile(invoices, [tx], load_portal_vendors(),
                              rp.DEFAULT_WINDOW, rp.DEFAULT_WARN)
    for m in matches:
        if m.debit.get("id") == tx.get("id"):
            pdf = Path(m.invoice.get("pdf_path", ""))
            return {"status": "found", "pdf_path": str(pdf), "pdf_name": pdf.name,
                    "source": f"portail ({m.confidence})"}
    return {"status": "none", "detail": "portail : facture téléchargée mais aucune ne colle à cette tx"}


# --- Attacher (confirmé) ---------------------------------------------------

def attach(tx_id: str, pdf_path: str) -> dict:
    pdf = Path(pdf_path)
    if not pdf.exists():
        return {"ok": False, "detail": "PDF introuvable"}
    c = qonto()
    try:
        if c.get_transaction_attachments(tx_id):     # garde-fou idempotence LIVE
            return {"ok": False, "detail": "déjà documentée (pas de doublon)"}
        c.upload_attachment(tx_id, pdf)
    except Exception as exc:
        return {"ok": False, "detail": f"attache impossible : {exc}"}
    # Trace dans reports/qonto/processed.json (comme reconcile_qonto).
    proc = health.load_json(QONTO_PROCESSED) or {}
    proc[tx_id] = {"pdf": pdf.name, "via": "dashboard"}
    QONTO_PROCESSED.parent.mkdir(parents=True, exist_ok=True)
    QONTO_PROCESSED.write_text(json.dumps(proc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "detail": f"{pdf.name} attaché"}


# --- Ajouter un abonnement -------------------------------------------------

def _csv_append(path: Path, row: dict) -> None:
    with path.open(encoding="utf-8") as f:
        header = next(csv.reader(r for r in f if not r.startswith("#")))
    with path.open("a", encoding="utf-8", newline="") as f:
        csv.DictWriter(f, fieldnames=header).writerow({k: row.get(k, "") for k in header})


def add_subscription(data: dict) -> dict:
    typ = data.get("type")
    if typ == "email":
        row = {"name": data.get("name", ""), "domains": data.get("domains", ""),
               "label_aliases": data.get("label_aliases", ""), "trusted": "1",
               "note": data.get("note", "ajouté via dashboard")}
        if not row["name"] or not row["domains"]:
            return {"ok": False, "detail": "name + domains requis"}
        _csv_append(SAAS_CSV, row)
        return {"ok": True, "detail": f"{row['name']} ajouté (email) — sera balayé au prochain run SaaS."}

    if typ == "portal":
        key = (data.get("handler_key") or "").strip().lower()
        prefix = (data.get("env_prefix") or key.upper()).strip().upper()
        if not key:
            return {"ok": False, "detail": "handler_key requis"}
        existing = {v["handler_key"] for v in health.list_portal_vendors()}
        if key in existing:
            return {"ok": False, "detail": f"handler_key '{key}' existe déjà"}
        _csv_append(PORTAL_CSV, {
            "name": data.get("name", key), "qonto_label_patterns": data.get("qonto_label_patterns", ""),
            "handler_key": key, "env_prefix": prefix, "billing_url": data.get("billing_url", ""),
            "note": data.get("note", "générique, ajouté via dashboard"),
        })
        creds = {}
        if data.get("email"):
            creds[f"{prefix}_EMAIL"] = data["email"]
        if data.get("password"):
            creds[f"{prefix}_PASSWORD"] = data["password"]
        if creds:
            env_set(creds)
        return {"ok": True, "handler_key": key,
                "detail": f"{data.get('name', key)} ajouté (portail générique). "
                          "Cliquez « Capturer login » pour ouvrir le navigateur."}

    return {"ok": False, "detail": "type inconnu (email|portal)"}


# --- Agrégat status --------------------------------------------------------

def build_status() -> dict:
    vendors = health.list_portal_vendors()
    sessions = []
    for v in vendors:
        st = health.session_status(v["handler_key"])
        st.update({"name": v.get("name"), "billing_url": v.get("billing_url"),
                   "note": v.get("note")})
        sessions.append(st)
    recap = health.load_recap()
    return {
        "sessions": sessions,
        "flux": health.flux_counts(),
        "recap": {
            "confirmed": recap["confirmed"],
            "suspects": recap["suspects"],
            "backlog": recap["backlog"],
            "counts": {"confirmed": len(recap["confirmed"]),
                       "suspects": len(recap["suspects"]),
                       "backlog": len(recap["backlog"])},
        },
    }


# --- Non-rapprochés LIVE + masquage local ----------------------------------

def _load_ignored() -> dict:
    return health.load_json(IGNORED_PATH) or {}


def _save_ignored(d: dict) -> None:
    COCKPIT_DIR.mkdir(parents=True, exist_ok=True)
    IGNORED_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


_org_slug = None


def _qonto_org_slug() -> str:
    """Slug d'organisation pour l'URL web Qonto (mis en cache)."""
    global _org_slug
    if _org_slug is None:
        try:
            org = qonto().get("/organization").get("organization", {})
            _org_slug = org.get("slug") or "-"
        except Exception:
            _org_slug = "-"
    return _org_slug


def qonto_tx_url(tx_id: str) -> str:
    """Deep-link vers la transaction dans l'app web Qonto (panneau ouvert)."""
    return (f"https://app.qonto.com/organizations/{_qonto_org_slug()}"
            f"/transactions?highlight={tx_id}")


def set_ignored(tx_id: str, ignore: bool, tx: dict | None = None) -> dict:
    d = _load_ignored()
    if ignore:
        d[tx_id] = {"label": (tx or {}).get("label"), "amount": (tx or {}).get("amount")}
    else:
        d.pop(tx_id, None)
    _save_ignored(d)
    out = {"ok": True, "ignored_count": len(d)}
    if ignore:
        # Ouvre la tx dans Qonto web pour le clic « justificatif non requis » (fiable, 1 clic).
        out["qonto_url"] = qonto_tx_url(tx_id)
    return out


def build_unreconciled() -> dict:
    """LIVE depuis Qonto (vérité terrain, parité avec l'app Qonto), moins les masquées.

    Débit `attachment_required` ET sans pièce jointe. Contrairement au récap figé
    (confirmed.json), reflète l'état Qonto au moment du clic.
    """
    load_dotenv()
    ignored = _load_ignored()
    try:
        rows = qonto().fetch_unreconciled_expenses(SINCE)
    except Exception as exc:
        # Repli : récap figé si l'API échoue.
        recap = health.load_recap()
        return {"live": False, "error": str(exc), "items": recap["confirmed"],
                "ignored": list(ignored.keys()), "count": len(recap["confirmed"])}
    items, hidden = [], []
    for tx in rows:
        if not (tx.get("attachment_required") and not tx.get("attachment_ids")):
            continue
        # La ligne est renvoyée telle quelle à /api/retry : elle doit porter les champs dont
        # dépend la recherche (devise d'origine + sources de nom marchand hors libellé).
        row = {"id": tx["id"], "label": tx["label"], "amount": tx["amount"],
               "currency": tx["currency"], "date": tx["settled_at"],
               "operation_type": tx["operation_type"],
               "local_amount": tx.get("local_amount"),
               "local_currency": tx.get("local_currency") or "",
               "reference": tx.get("reference") or "",
               "clean_counterparty_name": tx.get("clean_counterparty_name") or ""}
        (hidden if tx["id"] in ignored else items).append(row)
    return {"live": True, "items": items, "hidden": hidden,
            "ignored": list(ignored.keys()), "count": len(items)}


# --- HTTP ------------------------------------------------------------------

HTML_PATH = Path(__file__).with_name("dashboard.html")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silencieux
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            html = HTML_PATH.read_text(encoding="utf-8").encode("utf-8")
            return self._send(200, html, "text/html; charset=utf-8")
        if path == "/api/status":
            return self._json(build_status())
        if path == "/api/unreconciled":
            return self._json(build_unreconciled())
        if path == "/api/pdf":
            return self._serve_pdf(urlparse(self.path).query)
        return self._json({"error": "not found"}, 404)

    def _serve_pdf(self, query: str):
        """Sert un PDF candidat pour aperçu. Bridé à reports/ (pas de lecture arbitraire)."""
        from urllib.parse import parse_qs
        p = (parse_qs(query).get("path") or [""])[0]
        try:
            fp = Path(p).resolve()
            fp.relative_to((ROOT / "reports").resolve())   # doit rester sous reports/
            if fp.suffix.lower() != ".pdf" or not fp.exists():
                raise ValueError
        except (ValueError, OSError):
            return self._json({"error": "chemin invalide"}, 400)
        return self._send(200, fp.read_bytes(), "application/pdf")

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._body()
        try:
            if path == "/api/session/test":
                return self._json(probe_session(data.get("vendor", "")))
            if path == "/api/session/relogin":
                return self._json(relogin(data.get("vendor", "")))
            if path == "/api/retry":
                return self._json(retry_tx(data.get("tx") or {}))
            if path == "/api/attach":
                return self._json(attach(data.get("tx_id", ""), data.get("pdf_path", "")))
            if path == "/api/subscription/add":
                return self._json(add_subscription(data))
            if path == "/api/ignore":
                return self._json(set_ignored(data.get("tx_id", ""), True, data.get("tx")))
            if path == "/api/unignore":
                return self._json(set_ignored(data.get("tx_id", ""), False))
        except Exception as exc:
            return self._json({"ok": False, "status": "error", "detail": str(exc)}, 500)
        return self._json({"error": "not found"}, 404)


def main() -> int:
    ap = argparse.ArgumentParser(description="Cockpit local de rapprochement.")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1", help="NE PAS mettre 0.0.0.0 (voir tunnel).")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Cockpit → http://{args.host}:{args.port}  (Ctrl-C pour arrêter)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
