"""Client minimal Google Drive pour récupérer les factures fournisseurs.

Auth OAuth2 « desktop app » :
  - credentials.json : secret client OAuth (téléchargé depuis Google Cloud Console).
  - token.json       : jeton d'accès/rafraîchissement, créé au premier `authenticate(headless=False)`.

Le rafraîchissement du jeton est automatique et fonctionne sans navigateur :
seule la toute première autorisation nécessite un navigateur (flag `--auth`).
Les exécutions launchd suivantes tournent donc en headless.

Dépendances (optionnelles) : pip install -e ".[drive]"
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

# Lecture seule : on ne fait que lister et télécharger.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def _require_libs():
    """Importe les bibliothèques Google à la demande (déps optionnelles)."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError as exc:
        raise SystemExit(
            "Bibliothèques Google Drive absentes. Installer :\n"
            '  pip3 install google-api-python-client google-auth-oauthlib google-auth-httplib2\n'
            f"(détail : {exc})"
        )
    return Credentials, Request, InstalledAppFlow, build, MediaIoBaseDownload


class DriveClient:
    def __init__(self, credentials_path: Path, token_path: Path):
        self.credentials_path = Path(credentials_path)
        self.token_path = Path(token_path)
        self._service = None

    # -- Authentification ---------------------------------------------------
    def authenticate(self, headless: bool = True) -> None:
        """Charge ou rafraîchit le jeton.

        headless=True  : exécution automatique (launchd). Si aucun token valide
                         n'existe et ne peut être rafraîchi -> SystemExit.
        headless=False : autorise l'ouverture du navigateur pour la 1re auth.
        """
        Credentials, Request, InstalledAppFlow, build, _ = _require_libs()

        creds = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)

        if creds and creds.valid:
            pass
        elif creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())  # rafraîchissement headless OK
            self.token_path.write_text(creds.to_json(), encoding="utf-8")
        else:
            if headless:
                raise SystemExit(
                    "Aucun jeton Google Drive valide. Lancer d'abord l'auth interactive :\n"
                    "  python3 scripts/run_fournisseurs.py --auth --dry-run"
                )
            if not self.credentials_path.exists():
                raise SystemExit(
                    f"credentials.json introuvable ({self.credentials_path}). "
                    "Le télécharger depuis Google Cloud Console (OAuth Desktop App)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.credentials_path), SCOPES
            )
            creds = flow.run_local_server(port=0)
            self.token_path.write_text(creds.to_json(), encoding="utf-8")

        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)

    def _svc(self):
        if self._service is None:
            raise RuntimeError("DriveClient.authenticate() doit être appelé d'abord.")
        return self._service

    # -- Dossiers & fichiers ------------------------------------------------
    def find_folder_id(self, folder_name: str) -> str:
        """ID du dossier Drive par nom. ValueError si introuvable."""
        safe = folder_name.replace("'", "\\'")
        query = (
            f"name = '{safe}' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        resp = self._svc().files().list(
            q=query, spaces="drive", fields="files(id, name)", pageSize=10,
        ).execute()
        files = resp.get("files", [])
        if not files:
            raise ValueError(f"Dossier Drive introuvable : « {folder_name} »")
        return files[0]["id"]

    def list_pdfs(self, folder_id: str) -> List[Dict]:
        """Tous les PDF du dossier (récursif sur 1 niveau de sous-dossiers).

        Renvoie [{id, name, modifiedTime}].
        """
        out: List[Dict] = []
        self._list_pdfs_in(folder_id, out)
        # Sous-dossiers directs.
        sub_q = (
            f"'{folder_id}' in parents and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        sub = self._svc().files().list(
            q=sub_q, spaces="drive", fields="files(id, name)", pageSize=100,
        ).execute()
        for folder in sub.get("files", []):
            self._list_pdfs_in(folder["id"], out)
        return out

    def _list_pdfs_in(self, folder_id: str, out: List[Dict]) -> None:
        """Ajoute à `out` les PDF d'un dossier (pagination)."""
        query = (
            f"'{folder_id}' in parents and "
            "mimeType = 'application/pdf' and trashed = false"
        )
        page_token = None
        while True:
            resp = self._svc().files().list(
                q=query, spaces="drive",
                fields="nextPageToken, files(id, name, modifiedTime, createdTime)",
                pageSize=100, pageToken=page_token,
            ).execute()
            for f in resp.get("files", []):
                out.append({
                    "id": f["id"],
                    "name": f.get("name", f["id"]),
                    "modifiedTime": f.get("modifiedTime", ""),
                    "createdTime": f.get("createdTime", ""),
                })
            page_token = resp.get("nextPageToken")
            if not page_token:
                return

    def download_pdf(self, file_id: str, dest_path: Path) -> None:
        """Télécharge un fichier Drive vers dest_path."""
        import io

        _, _, _, _, MediaIoBaseDownload = _require_libs()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        request = self._svc().files().get_media(fileId=file_id)
        with io.FileIO(str(dest_path), "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
