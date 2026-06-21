"""Automatisation du rapprochement bancaire Qonto -> Pennylane.

Le cœur métier (recon.normalize) est sans dépendance externe et testable hors ligne.
Les modules réseau (recon.pennylane_client, recon.transactions, recon.importer)
ne parlent qu'à l'API Pennylane v2.
"""

__version__ = "0.1.0"
