"""Réponse Qonto TRONQUÉE : re-tenter, jamais parser un JSON partiel (régression 2026-07-20).

`_request` ne regardait NI le code de retour de curl NI l'absence du marqueur `__STATUS__`.
Quand curl mourait en cours de transfert (timeout `--max-time`, connexion coupée), le corps
partiel partait quand même au parseur JSON et remontait en `JSONDecodeError` illisible
(« Unterminated string starting at ... char 61805 ») au lieu d'être re-tenté.

Même classe de bug que l'IMAP avalé dans `reconcile_qonto` : une ERREUR prise pour une DONNÉE.
"""
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from recon.qonto_client import QontoClient, QontoError  # noqa: E402


def _result(stdout: str, returncode: int = 0):
    m = mock.Mock()
    m.stdout, m.returncode = stdout, returncode
    return m


@pytest.fixture
def client():
    return QontoClient("slug", "key")


def test_truncated_body_retries_then_raises_clear_error(client):
    """Corps coupé (pas de marqueur de fin, curl code 28) -> re-tenté, puis erreur EXPLICITE."""
    calls = []

    def fake(cmd, **kw):
        calls.append(1)
        return _result('{"transactions":[{"id":"abc', returncode=28)   # 28 = timeout curl

    with mock.patch.object(subprocess, "run", side_effect=fake), mock.patch("time.sleep"):
        with pytest.raises(QontoError) as exc:
            client.get("/transactions")
    assert "TRONQU" in str(exc.value).upper()          # message actionnable, pas un JSONDecodeError
    assert len(calls) == 4                              # bien re-tenté (_MAX_RETRIES)


def test_truncation_recovers_transparently(client):
    """Une coupure suivie d'une réponse complète : l'appelant ne voit rien."""
    seq = [_result('{"x": 1', returncode=28),
           _result('{"x": 1}\n__STATUS__200')]
    with mock.patch.object(subprocess, "run", side_effect=seq), mock.patch("time.sleep"):
        assert client.get("/t") == {"x": 1}


def test_partial_json_never_reaches_the_parser(client):
    """Le JSON partiel ne doit JAMAIS remonter en JSONDecodeError nu."""
    with mock.patch.object(subprocess, "run",
                           return_value=_result('{"a":[1,2', returncode=28)), \
         mock.patch("time.sleep"):
        with pytest.raises(QontoError):                 # ... et surtout pas JSONDecodeError
            client.get("/t")


def test_unreadable_json_with_clean_exit_is_retried(client):
    """Corps complet selon curl mais illisible -> re-tenté, puis QontoError explicite."""
    with mock.patch.object(subprocess, "run",
                           return_value=_result("<html>maintenance</html>\n__STATUS__200")), \
         mock.patch("time.sleep"):
        with pytest.raises(QontoError) as exc:
            client.get("/t")
    assert "illisible" in str(exc.value)


def test_normal_response_unchanged(client):
    with mock.patch.object(subprocess, "run",
                           return_value=_result('{"ok":true}\n__STATUS__200')):
        assert client.get("/t") == {"ok": True}


def test_http_error_still_surfaces(client):
    with mock.patch.object(subprocess, "run",
                           return_value=_result("nope\n__STATUS__401")), \
         mock.patch("time.sleep"):
        with pytest.raises(QontoError) as exc:
            client.get("/t")
    assert "401" in str(exc.value)


def test_empty_body_is_empty_dict(client):
    with mock.patch.object(subprocess, "run", return_value=_result("\n__STATUS__200")):
        assert client.get("/t") == {}
