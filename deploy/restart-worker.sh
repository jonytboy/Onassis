#!/usr/bin/env bash
# =====================================================================
# restart-worker.sh — privilege-separated ONASSIS restart.
#
# Triggered by onassis-restart.path when the (unprivileged) web application
# drops a restart request into the spool directory. This runs as ROOT via the
# onassis-restart.service oneshot unit, so the web app itself NEVER calls
# systemctl — it can only *ask* for a restart. That keeps the security boundary
# intact while still automating the last manual deployment step.
#
# Protocol (plain key=value so no JSON parser is needed):
#   request:  <spool>/restart.request   token=… reason=… requested_at=…
#   result:   <spool>/restart.result    token=… ok=true|false at=…
# =====================================================================
set -uo pipefail

SPOOL="${ONASSIS_RESTART_SPOOL:-/run/onassis}"
SERVICE="${ONASSIS_SERVICE:-onassis}"
HEALTH_URL="${ONASSIS_HEALTH_URL:-http://127.0.0.1:8000/health}"
MARKER="${ONASSIS_RESTART_MARKER:-}"
REQ="$SPOOL/restart.request"
RES="$SPOOL/restart.result"

[ -f "$REQ" ] || exit 0                                  # nothing to do
TOKEN="$(sed -n 's/^token=//p' "$REQ" | head -n1)"
rm -f "$REQ"                                             # consume the request

systemctl restart "$SERVICE"

# Wait for the service to come back healthy.
ok=false
for _ in $(seq 1 20); do
  if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then ok=true; break; fi
  sleep 2
done

# Clear the app's restart-required marker on a healthy restart.
if [ "$ok" = true ] && [ -n "$MARKER" ]; then rm -f "$MARKER" 2>/dev/null || true; fi

umask 022
printf 'token=%s\nok=%s\nat=%s\n' "$TOKEN" "$ok" "$(date -Is)" > "$RES"
[ "$ok" = true ]
