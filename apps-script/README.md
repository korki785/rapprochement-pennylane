# Pennylane Mailer (Google Apps Script)

Envoie automatiquement vers Pennylane les factures reçues par email — **y compris celles
qui sont dans le corps du mail** (sans PDF joint), que le simple transfert Gmail ne gère pas.

✅ **Aucun filtre Gmail à créer.** Le script cherche lui-même les emails-factures.
À installer sur **chaque compte Gmail** d'où arrivent des factures.

## Ce que le script cherche
```
(facture OR invoice OR reçu OR receipt OR billing OR "votre facture") -from:qonto.com -from:me after:2026/03/31 -label:Pennylane-Sent
```
- depuis le **1ᵉʳ avril 2026** (`SINCE` en haut du script, modifiable),
- **hors notifications Qonto** (`EXCLUDE_SENDERS`),
- **hors emails déjà traités** (libellé `Pennylane-Sent`).

## 1. Installer le script
1. Va sur https://script.google.com → *Nouveau projet*.
2. Supprime le code par défaut, colle le contenu de `pennylane-mailer.gs`.
3. Vérifie en haut : `PENNYLANE_ADDRESS` (ton adresse d'import) et `SINCE` (date de départ).
4. Enregistre 💾.

## 2. Vérifier AVANT d'envoyer (aperçu)
1. En haut, choisis la fonction **`previewMatches`** → **Exécuter**.
2. Autorise l'accès quand Google le demande (*Examiner les autorisations* → ton compte → *Avancé* → *Accéder au projet* → *Autoriser*).
3. Menu **Exécution / Journaux** (en bas) : tu vois le **nombre** d'emails qui seraient traités + leurs objets.
   → C'est le moment de vérifier qu'il n'y a pas de bruit. Si tu vois un expéditeur parasite, ajoute-le dans `EXCLUDE_SENDERS` (ex. `['qonto.com', 'autre-domaine.com']`) et relance `previewMatches`.

## 3. Lancer le vrai envoi
1. Choisis **`forwardInvoicesToPennylane`** → **Exécuter**.
2. Vérifie côté Pennylane (**Achats → Factures fournisseurs**) que les PDF arrivent.
   - Les emails traités reçoivent le libellé `Pennylane-Sent` (visible dans Gmail).

## 4. Automatiser (1×/heure)
Choisis **`installHourlyTrigger`** → **Exécuter** une fois. Le script tourne alors tout seul.

## Notes / limites
- **Quota Gmail** : ~100 envois/jour (compte gratuit). Si gros backlog, il se résorbe sur quelques jours (garde-fou `MAX_THREADS_PER_RUN = 40` par passage).
- **Anti-doublon** : le libellé `Pennylane-Sent` empêche de renvoyer ; et Pennylane dédoublonne de son côté.
- **Anti-boucle** : `-from:me` exclut les emails que le script envoie lui-même.
- Le **rendu PDF d'un corps de mail** très stylé peut être imparfait, mais reste lisible/exploitable par l'OCR Pennylane.
- **2e compte Gmail** : répète les étapes 1→4 connecté à ce compte.
- Tu peux **supprimer l'ancien filtre/transfert** Gmail créé précédemment : il ne sert plus.
