#!/usr/bin/env bash
# =====================================================================
# rollback.sh — revert ONASSIS to the commit recorded by the last deploy.
#
#   ./rollback.sh          # asks for confirmation
#   ./rollback.sh -y       # no prompt (for scripted recovery)
#
# deploy.sh records the commit it deployed FROM in .onassis_deploy_state.
# This resets the working tree to that commit and restarts the service, so a
# broken deploy is undone with one command. Exits non-zero if it can't verify
# the service + health afterwards.
# =====================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

SERVICE_NAME="${ONASSIS_SERVICE:-onassis}"
HEALTH_URL="${ONASSIS_HEALTH_URL:-https://api.onassismed.com/health}"
STATE_FILE="${ONASSIS_STATE_FILE:-$APP_DIR/.onassis_deploy_state}"
HEALTH_RETRIES="${ONASSIS_HEALTH_RETRIES:-10}"
HEALTH_DELAY="${ONASSIS_HEALTH_DELAY:-3}"

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }
systemctl_do() { if [ "$(id -u)" -eq 0 ]; then systemctl "$@"; else sudo systemctl "$@"; fi; }

ASSUME_YES=0
case "${1:-}" in -y|--yes) ASSUME_YES=1 ;; esac

[ -f "$STATE_FILE" ] || fail "No rollback point found ($STATE_FILE). Was deploy.sh ever run?"
TARGET="$(tr -d '[:space:]' < "$STATE_FILE")"
[ -n "$TARGET" ] || fail "Rollback point file is empty: $STATE_FILE"

CURRENT="$(git rev-parse HEAD)"
git cat-file -e "${TARGET}^{commit}" 2>/dev/null || fail "Recorded commit $TARGET not found locally."

say "Rollback ONASSIS"
ok "Current commit : $CURRENT"
ok "Rollback target: $TARGET"

if [ "$CURRENT" = "$TARGET" ]; then
  ok "Already at the rollback target — nothing to revert."
  exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
  printf '\n\033[1;33mThis will hard-reset the working tree to %s and restart %s.\033[0m\n' "$TARGET" "$SERVICE_NAME"
  read -r -p "Proceed? [y/N] " reply
  case "$reply" in y|Y|yes|YES) ;; *) fail "Aborted by user." ;; esac
fi

say "Resetting to $TARGET…"
git reset --hard "$TARGET" || fail "git reset failed."
ok "Working tree is now at $(git rev-parse --short HEAD)."

# If dependencies differ at the rolled-back commit, refresh them (best-effort).
if [ -f requirements.txt ]; then
  PIP="pip3"; command -v pip3 >/dev/null 2>&1 || PIP="pip"
  [ -x "$APP_DIR/venv/bin/pip" ]  && PIP="$APP_DIR/venv/bin/pip"
  [ -x "$APP_DIR/.venv/bin/pip" ] && PIP="$APP_DIR/.venv/bin/pip"
  say "Reinstalling dependencies for the rolled-back commit…"
  "$PIP" install -r requirements.txt || fail "Dependency install failed during rollback."
fi

say "Restarting service: $SERVICE_NAME"
systemctl_do restart "$SERVICE_NAME" || fail "Failed to restart $SERVICE_NAME."
systemctl_do is-active --quiet "$SERVICE_NAME" || fail "Service $SERVICE_NAME not active after rollback."
ok "Service is active."

say "Verifying health: $HEALTH_URL"
for i in $(seq 1 "$HEALTH_RETRIES"); do
  if curl -fsS --max-time 10 "$HEALTH_URL" >/dev/null 2>&1; then
    ok "Health check passed."
    say "ROLLBACK SUCCESSFUL — now at $(git rev-parse --short HEAD)"
    exit 0
  fi
  printf '  … health not ready yet (attempt %s/%s)\n' "$i" "$HEALTH_RETRIES"
  sleep "$HEALTH_DELAY"
done
fail "Health check FAILED after rollback. Manual intervention required."
