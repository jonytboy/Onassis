#!/usr/bin/env bash
# =====================================================================
# health.sh — one-glance production health for ONASSIS.
#
#   ./health.sh
#
# Reports git commit, service status, API health, disk + memory usage,
# recent service errors, and whether /exports is reachable locally.
# Prints a clear overall PASS/FAIL and exits non-zero on FAIL, so it can be
# used in cron/monitoring too. Read-only — it changes nothing.
# =====================================================================
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

SERVICE_NAME="${ONASSIS_SERVICE:-onassis}"
HEALTH_URL="${ONASSIS_HEALTH_URL:-https://api.onassismed.com/health}"
LOCAL_BASE="${ONASSIS_LOCAL_URL:-http://127.0.0.1:8000}"
DISK_WARN="${ONASSIS_DISK_WARN:-85}"
DISK_FAIL="${ONASSIS_DISK_FAIL:-95}"

FAILED=0
pass() { printf '\033[1;32m  PASS\033[0m  %s\n' "$*"; }
warn() { printf '\033[1;33m  WARN\033[0m  %s\n' "$*"; }
crit() { printf '\033[1;31m  FAIL\033[0m  %s\n' "$*"; FAILED=1; }
info() { printf '\033[1;36m  INFO\033[0m  %s\n' "$*"; }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }

systemctl_do() { if [ "$(id -u)" -eq 0 ]; then systemctl "$@"; else sudo -n systemctl "$@" 2>/dev/null || systemctl "$@"; fi; }
journal_do()   { if [ "$(id -u)" -eq 0 ]; then journalctl "$@"; else sudo -n journalctl "$@" 2>/dev/null || journalctl "$@"; fi; }

printf '\033[1m================ ONASSIS health @ %s ================\033[0m\n' "$(date '+%Y-%m-%d %H:%M:%S')"

# --- Git commit ------------------------------------------------------
hdr "Git"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  info "commit $(git rev-parse --short HEAD)  branch $(git rev-parse --abbrev-ref HEAD)"
else
  crit "not a git repository"
fi

# --- Service status --------------------------------------------------
hdr "Service ($SERVICE_NAME)"
if command -v systemctl >/dev/null 2>&1; then
  STATE="$(systemctl_do is-active "$SERVICE_NAME" 2>/dev/null || true)"
  if [ "$STATE" = "active" ]; then pass "systemd unit is active"
  else crit "systemd unit is '$STATE' (expected active)"; fi
else
  warn "systemctl not available — cannot check the service unit"
fi

# --- API health (public) ---------------------------------------------
hdr "API health"
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$HEALTH_URL" 2>/dev/null)"; CODE="${CODE:-000}"
if [ "$CODE" = "200" ]; then pass "$HEALTH_URL -> 200"
else crit "$HEALTH_URL -> $CODE"; fi

# --- /exports reachable locally --------------------------------------
hdr "/exports (local static mount)"
XCODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$LOCAL_BASE/exports/" 2>/dev/null)"; XCODE="${XCODE:-000}"
# Any HTTP response (even 404 for the no-listing root) proves the mount is served.
if [ "$XCODE" != "000" ]; then pass "$LOCAL_BASE/exports/ reachable (HTTP $XCODE)"
else crit "$LOCAL_BASE/exports/ not reachable — is the app listening on $LOCAL_BASE?"; fi

# --- Disk usage ------------------------------------------------------
hdr "Disk"
USE="$(df -P "$APP_DIR" | awk 'NR==2{gsub("%","",$5); print $5}')"
LINE="$(df -Ph "$APP_DIR" | awk 'NR==2{printf "%s used of %s (%s) on %s", $3, $2, $5, $6}')"
if   [ -z "$USE" ];               then warn "could not read disk usage"
elif [ "$USE" -ge "$DISK_FAIL" ]; then crit "disk ${USE}% — $LINE"
elif [ "$USE" -ge "$DISK_WARN" ]; then warn "disk ${USE}% — $LINE"
else pass "disk ${USE}% — $LINE"; fi

# --- Memory usage ----------------------------------------------------
hdr "Memory"
if command -v free >/dev/null 2>&1; then
  info "$(free -h | awk 'NR==1{print "      "$0} NR==2{print $0}')"
  AVAIL_PCT="$(free | awk 'NR==2{printf "%d", ($7/$2)*100}')"
  if [ "${AVAIL_PCT:-100}" -lt 10 ]; then warn "only ${AVAIL_PCT}% memory available"
  else info "${AVAIL_PCT}% memory available"; fi
else
  warn "'free' not available — cannot report memory"
fi

# --- Recent errors from journalctl -----------------------------------
hdr "Recent errors (journalctl -u $SERVICE_NAME, last hour)"
if command -v journalctl >/dev/null 2>&1; then
  ERR="$(journal_do -u "$SERVICE_NAME" -p err --since '1 hour ago' --no-pager -q 2>/dev/null | tail -n 20)"
  if [ -z "$ERR" ]; then pass "no error-level log entries in the last hour"
  else
    N="$(printf '%s\n' "$ERR" | grep -c . || true)"
    warn "$N error-level entrie(s) in the last hour (most recent below):"
    printf '%s\n' "$ERR" | tail -n 5 | sed 's/^/        /'
  fi
else
  warn "journalctl not available — cannot inspect service logs"
fi

# --- Verdict ---------------------------------------------------------
printf '\n\033[1m----------------------------------------------------------\033[0m\n'
if [ "$FAILED" -eq 0 ]; then
  printf '\033[1;32mOVERALL: PASS — production looks healthy.\033[0m\n'
  exit 0
else
  printf '\033[1;31mOVERALL: FAIL — see the FAIL lines above.\033[0m\n'
  exit 1
fi
