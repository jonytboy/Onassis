#!/usr/bin/env bash
# =====================================================================
# install-restart-bridge.sh — one-time setup so the Operations Centre
# "Restart ONASSIS" button actually restarts the service.
#
# It installs the privilege-separated restart bridge: a root `path` unit
# that watches the spool for a restart request and a root `oneshot` that
# performs the restart. The (unprivileged) web app only writes a request
# file — it never runs systemctl itself, so the security boundary holds.
#
# Run once, as root, on the server:
#     sudo ONASSIS_DIR=/opt/onassis SERVICE=onassis deploy/install-restart-bridge.sh
#
# Requirements already provided by the main unit (deploy/systemd/onassis.service):
#   * Environment=ONASSIS_RESTART_SPOOL=/run/onassis
#   * RuntimeDirectory=onassis   (systemd creates /run/onassis, service-user owned)
# If your hand-written unit lacks those two lines, add them and
# `systemctl daemon-reload && systemctl restart onassis` before/after this.
# =====================================================================
set -euo pipefail

ONASSIS_DIR="${ONASSIS_DIR:-/opt/onassis}"
SERVICE="${SERVICE:-onassis}"
SPOOL="${ONASSIS_RESTART_SPOOL:-/run/onassis}"
HEALTH_URL="${ONASSIS_HEALTH_URL:-http://127.0.0.1:8000/health}"
UNIT_DIR="/etc/systemd/system"
SRC="$ONASSIS_DIR/deploy/systemd"

if [ "$(id -u)" -ne 0 ]; then
  echo "Must run as root (use sudo)." >&2; exit 1
fi
[ -f "$SRC/onassis-restart.path" ] || { echo "Missing $SRC/onassis-restart.path" >&2; exit 1; }

echo "Installing restart bridge for service '$SERVICE' (spool: $SPOOL)…"

# 1. Worker script is executable.
chmod +x "$ONASSIS_DIR/deploy/restart-worker.sh"

# 2. Install the path watcher verbatim.
install -m 0644 "$SRC/onassis-restart.path" "$UNIT_DIR/onassis-restart.path"

# 3. Install the oneshot, pinning this deployment's paths/service into it.
sed -e "s#^Environment=ONASSIS_SERVICE=.*#Environment=ONASSIS_SERVICE=$SERVICE#" \
    -e "s#^Environment=ONASSIS_RESTART_SPOOL=.*#Environment=ONASSIS_RESTART_SPOOL=$SPOOL#" \
    -e "s#^Environment=ONASSIS_HEALTH_URL=.*#Environment=ONASSIS_HEALTH_URL=$HEALTH_URL#" \
    -e "s#^ExecStart=.*#ExecStart=$ONASSIS_DIR/deploy/restart-worker.sh#" \
    "$SRC/onassis-restart.service" > "$UNIT_DIR/onassis-restart.service"
chmod 0644 "$UNIT_DIR/onassis-restart.service"

# 4. Enable the watcher.
systemctl daemon-reload
systemctl enable --now onassis-restart.path

echo
echo "Installed. Verify:"
echo "  systemctl status onassis-restart.path      # should be active (waiting)"
echo "  # then click 'Restart ONASSIS' in the Operations Centre, or:"
echo "  sudo -u ${SERVICE} sh -c 'echo token=test > ${SPOOL}/restart.request'"
echo "The service should restart within a couple of seconds."
