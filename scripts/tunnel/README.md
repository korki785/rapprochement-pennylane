# Exposer le cockpit en ligne (Option 1 — tunnel vers le Mac)

Le cockpit tourne sur le Mac (`python3 scripts/dashboard.py`, bind `127.0.0.1:8787`).
Le tunnel le rend joignable depuis le téléphone/ailleurs **sans** ouvrir de port ni déplacer
les secrets. Deux options, la 1re recommandée.

## Option A — Tailscale (le plus simple, privé)

Réseau privé entre tes appareils. Personne d'autre ne peut atteindre la page (auth = tes
appareils eux-mêmes). Aucune URL publique.

```bash
brew install --cask tailscale        # Mac
# installer aussi Tailscale sur le téléphone, se connecter au MÊME compte
tailscale up
# exposer le cockpit dans le tailnet :
tailscale serve --bg 8787
```

Puis sur le téléphone : ouvrir `http://<nom-mac>.<ton-tailnet>.ts.net` (visible via
`tailscale serve status`). Seuls tes appareils connectés y accèdent.

## Option B — Cloudflare Tunnel + Access (URL publique, protégée par login)

URL publique mais **verrouillée** derrière un login email (Cloudflare Access) : indispensable
car le formulaire « Ajouter un abonnement » accepte des mots de passe.

```bash
brew install cloudflared
cloudflared tunnel login
cloudflared tunnel create cockpit
# router un sous-domaine vers le tunnel (nécessite un domaine sur Cloudflare) :
cloudflared tunnel route dns cockpit cockpit.tondomaine.com
# lancer :
cloudflared tunnel run --url http://127.0.0.1:8787 cockpit
```

Dans le dashboard Cloudflare Zero Trust → **Access → Applications** : ajouter
`cockpit.tondomaine.com`, policy « email == hello@maisondarwish.com » (OTP). Sans ça,
n'importe qui avec l'URL atteindrait le formulaire de mots de passe.

⚠ Ne **jamais** lancer `dashboard.py --host 0.0.0.0`. Le bind reste `127.0.0.1` ; seul le
tunnel (authentifié) sort de la machine.

## Test rapide (sans internet)

```bash
python3 scripts/dashboard.py
open http://127.0.0.1:8787
```
