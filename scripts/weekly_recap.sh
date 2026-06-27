#!/bin/bash
# Récap hebdo FIABLE (lundi 9h via launchd). Remplace email_weekly.sh.
# 1. Best-effort : rapproche tout ce qui est auto-attachable (4 flux).
# 2-3. Audit LIVE Qonto + vérif adversariale (audit_unreconciled.py).
# 4. Si suspects → escalade Claude headless (autonome) ; sinon repli bloquer+alerter.
# 5. Envoi du récap fiable (email_recap.py) — ou alerte si needs_review non vide.
# Dédup hebdo (.derniere_semaine_email) : une seule fois par semaine même si reboot.
# DRY=1 : enchaînement complet sans attacher/envoyer (tests).

PROJECT="/Users/naeldarwish/rapprochement-pennylane"
LOG="$PROJECT/reports/recap/weekly.log"
PYTHON="/Users/naeldarwish/Library/Python/3.9/bin/python3"
[ -x "$PYTHON" ] || PYTHON="/usr/bin/python3"
mkdir -p "$PROJECT/reports/recap"

SEMAINE=$(date +%Y-%W)
F="$PROJECT/.derniere_semaine_email"
if [ -z "$DRY" ] && [ -f "$F" ] && [ "$(cat "$F")" = "$SEMAINE" ]; then
    echo "$(date '+%F %T') — déjà envoyé semaine $SEMAINE, skip" >> "$LOG"
    exit 0
fi

cd "$PROJECT" || exit 1
echo "============== $(date '+%F %T') récap semaine $SEMAINE${DRY:+ (DRY)}" >> "$LOG"

# État du run précédent.
rm -f reports/recap/needs_review.json

# 1. Best-effort : attacher tout justificatif auto-trouvable AVANT de mesurer.
if [ -z "$DRY" ]; then
    "$PYTHON" scripts/run_fournisseurs.py --since "2026-04-01" --force >> "$LOG" 2>&1 || true
    "$PYTHON" scripts/run_saas.py --since "2026-04-01" >> "$LOG" 2>&1 || true
    "$PYTHON" scripts/run_ubereats.py --since "2026-05-01" >> "$LOG" 2>&1 || true
    "$PYTHON" scripts/run_portals.py --since "2026-04-01" >> "$LOG" 2>&1 || true
fi

# 2-3. Audit live + vérif adversariale sur TOUT l'exercice comptable (exit 2 si suspects).
"$PYTHON" scripts/audit_unreconciled.py >> "$LOG" 2>&1
AUDIT_RC=$?

# 4. Escalade si suspects.
if [ "$AUDIT_RC" -eq 2 ]; then
    CLAUDE=$(command -v claude 2>/dev/null)
    [ -z "$CLAUDE" ] && [ -x /opt/homebrew/bin/claude ] && CLAUDE=/opt/homebrew/bin/claude
    if [ -n "$CLAUDE" ] && [ -z "$DRY" ]; then
        echo "$(date '+%F %T') — suspects → escalade Claude" >> "$LOG"
        "$CLAUDE" -p --dangerously-skip-permissions \
            "$(cat scripts/recap_escalation_prompt.txt)" >> "$LOG" 2>&1 || true
        # Si Claude a laissé des suspects non résolus dans suspects.json sans needs_review,
        # on ne bloque pas faute de signal — needs_review fait foi.
    else
        echo "$(date '+%F %T') — claude indispo/DRY → repli : récap bloqué" >> "$LOG"
        cp reports/recap/suspects.json reports/recap/needs_review.json 2>/dev/null || true
    fi
fi

# 5. Envoi : récap fiable, ou alerte « à vérifier » si needs_review non vide.
if [ -n "$DRY" ]; then
    "$PYTHON" scripts/email_recap.py --dry-run >> "$LOG" 2>&1
    RC=$?
else
    "$PYTHON" scripts/email_recap.py >> "$LOG" 2>&1
    RC=$?
    [ "$RC" -eq 0 ] && echo "$SEMAINE" > "$F"
fi
echo "$(date '+%F %T') — terminé (code $RC)" >> "$LOG"
exit "$RC"
