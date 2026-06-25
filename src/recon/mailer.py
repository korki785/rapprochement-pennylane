"""Envoi d'email via SMTP Gmail (stdlib uniquement).

Repris du modèle automation-orfeo : smtplib.SMTP_SSL + mot de passe
d'application Gmail. Aucune dépendance externe.

Variables d'environnement (.env) :
    GMAIL_USER           Adresse Gmail expéditeur.
    GMAIL_APP_PASSWORD   Mot de passe d'application Gmail (16 caractères).
    EMAIL_TO             Adresse destinataire.
"""
from __future__ import annotations

import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Optional

from .config import load_dotenv

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def load_email_config() -> tuple[str, str, str]:
    """Renvoie (GMAIL_USER, GMAIL_APP_PASSWORD, EMAIL_TO) depuis le .env."""
    load_dotenv()
    user = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
    to = os.environ.get("EMAIL_TO", "").strip()
    if not user or not password or not to:
        raise SystemExit(
            "GMAIL_USER / GMAIL_APP_PASSWORD / EMAIL_TO manquants dans .env "
            "(réutiliser les identifiants Gmail d'automation-orfeo)."
        )
    return user, password, to


def send_email(subject: str, body: str,
               attachments: Optional[List[Path]] = None) -> None:
    """Envoie un email texte (UTF-8) via Gmail SMTP_SSL.

    attachments : liste optionnelle de fichiers joints.
    """
    user, password, to = load_email_config()

    if attachments:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body, "plain", "utf-8"))
        for path in attachments:
            path = Path(path)
            if not path.exists():
                continue
            part = MIMEApplication(path.read_bytes(), Name=path.name)
            part["Content-Disposition"] = f'attachment; filename="{path.name}"'
            msg.attach(part)
    else:
        msg = MIMEText(body, "plain", "utf-8")

    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.login(user, password)
        smtp.sendmail(user, to, msg.as_string())
