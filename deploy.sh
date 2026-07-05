#!/usr/bin/env bash
# =====================================================================
# deploy.sh — one-command, safe, repeatable ONASSIS deployment.
#
#   cd ~/Onassis && ./deploy.sh
#
# It backs up first (stops if the backup fails), records the current commit
# for rollback, pulls the latest code, updates dependencies only if
# requirements.txt changed, restarts the systemd service, and verifies the
# service + public health endpoint. Any failure exits non-zero.
#
# This script changes NOTHING about the application's behaviour — it only
# operates the deployment. Override the defaults via the environment, e.g.
#   ONASSIS_SERVICE=onassis ONASSIS_HEALTH_URL=https://api.onassismed.com/health ./deploy.sh
# =====================================================================
set -euo pipefail

# --- Configuration (override via env) --------------------------------
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="${ONASSIS_SERVICE:-onassis}"
HEALTH_URL="${ONASSIS_HEALTH_URL:-https://api.onassismed.com/health}"
STATE_FILE="${ONASSIS_STATE_FILE:-$APP_DIR/.onassis_deploy_state}"
HEALTH_RETRIES="${ONASSIS_HEALTH_RETRIES:-10}"
HEALTH_DELAY="${ONASSIS_HEALTH_DELAY:-3}"

cd "$APP_DIR"

# --- Helpers ----------------------------------------------------------
say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

systemctl_do() {  # run systemctl as root or via sudo
  if [ "$(id -u)" -eq 0 ]; then systemctl "$@"; else sudo systemctl "$@"; fi
}

pip_bin() {  # prefer the service's virtualenv if present
  if   [ -x "$APP_DIR/venv/bin/pip" ];  then echo "$APP_DIR/venv/bin/pip"
  elif [ -x "$APP_DIR/.venv/bin/pip" ]; then echo "$APP_DIR/.venv/bin/pip"
  elif command -v pip3 >/dev/null 2>&1; then echo "pip3"
  else echo "pip"; fi
}

trap 'fail "Deployment FAILED. Review the output above. To revert: ./rollback.sh"' ERR

# --- 0. Sanity -------------------------------------------------------
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "Not a git repository: $APP_DIR"

# --- 1. Confirm branch ----------------------------------------------
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
say "Deploying ONASSIS from $APP_DIR"
ok "Branch: $BRANCH"

# --- 2. Old commit ---------------------------------------------------
OLD_SHA="$(git rev-parse HEAD)"
ok "Current commit (old): $OLD_SHA"

# --- 3. Pre-deploy backup (stop if it fails) -------------------------
say "Backing up before deploy…"
if [ -x "$APP_DIR/backup.sh" ]; then
  "$APP_DIR/backup.sh" || fail "Backup failed — deployment stopped (nothing was pulled)."
else
  fail "backup.sh not found or not executable — refusing to deploy without a backup."
fi
ok "Backup complete."

# Record the commit we are deploying FROM, so rollback can return to it.
printf '%s\n' "$OLD_SHA" > "$STATE_FILE"
ok "Recorded rollback point -> $STATE_FILE"

# --- 4. Detect requirements changes across the pull ------------------
req_hash() { [ -f requirements.txt ] && sha256sum requirements.txt | awk '{print $1}' || echo "none"; }
REQ_BEFORE="$(req_hash)"

# --- 5. Pull latest code --------------------------------------------
say "Pulling latest code (origin/$BRANCH)…"
git fetch --prune origin "$BRANCH" || fail "git fetch failed."
git pull --ff-only origin "$BRANCH" || fail "git pull failed (not a fast-forward?). Resolve manually."

# --- 6. New commit ---------------------------------------------------
NEW_SHA="$(git rev-parse HEAD)"
ok "New commit: $NEW_SHA"
if [ "$OLD_SHA" = "$NEW_SHA" ]; then
  ok "Already up to date (no new commits)."
fi

# --- 7. Update dependencies only if requirements.txt changed ---------
REQ_AFTER="$(req_hash)"
if [ "$REQ_BEFORE" != "$REQ_AFTER" ]; then
  say "requirements.txt changed — updating dependencies…"
  PIP="$(pip_bin)"
  ok "Using pip: $PIP"
  "$PIP" install -r requirements.txt || fail "Dependency install failed."
  ok "Dependencies updated."
else
  ok "requirements.txt unchanged — skipping dependency install."
fi

# --- 8. Restart the service -----------------------------------------
say "Restarting service: $SERVICE_NAME"
systemctl_do restart "$SERVICE_NAME" || fail "Failed to restart $SERVICE_NAME."

# --- 9. Service status ----------------------------------------------
if systemctl_do is-active --quiet "$SERVICE_NAME"; then
  ok "Service is active."
else
  systemctl_do status "$SERVICE_NAME" --no-pager -l || true
  fail "Service $SERVICE_NAME is not active after restart."
fi

# --- 10. Health check (retry) ---------------------------------------
say "Checking health: $HEALTH_URL"
for i in $(seq 1 "$HEALTH_RETRIES"); do
  if curl -fsS --max-time 10 "$HEALTH_URL" >/dev/null 2>&1; then
    ok "Health check passed."
    say "DEPLOYMENT SUCCESSFUL  ($OLD_SHA -> $NEW_SHA)"
    exit 0
  fi
  printf '  … health not ready yet (attempt %s/%s)\n' "$i" "$HEALTH_RETRIES"
  sleep "$HEALTH_DELAY"
done

fail "Health check FAILED after $HEALTH_RETRIES attempts. Consider: ./rollback.sh"
