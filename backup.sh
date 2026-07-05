#!/usr/bin/env bash
# =====================================================================
# backup.sh — timestamped backup of ONASSIS's stateful data.
#
#   ./backup.sh
#
# Copies the database, secrets, config and generated/operational data into
#   backups/YYYY-MM-DD-HHMMSS/
# Backups are git-ignored and never committed. Exits non-zero on failure so
# deploy.sh can stop before pulling if the backup didn't succeed.
# =====================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

TS="$(date +%Y-%m-%d-%H%M%S)"
DEST="${ONASSIS_BACKUP_DIR:-$APP_DIR/backups}/$TS"

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

mkdir -p "$DEST" || fail "Could not create backup directory: $DEST"
say "Backing up ONASSIS -> $DEST"

# Copy a file or directory if it exists; a missing item is a warning, not a
# failure (e.g. no exports yet on a fresh install). A copy that fails IS fatal.
copy_item() {
  local src="$1"
  if [ -e "$src" ]; then
    cp -a "$src" "$DEST/" || fail "Failed to copy $src"
    ok "Backed up $src"
  else
    warn "Skipped (not present): $src"
  fi
}

copy_item "data/onassis.db"
copy_item ".env"
copy_item "config.yaml"
copy_item "exports"
copy_item "logs"

# A small manifest so a restore knows exactly what this snapshot holds.
{
  echo "backup_timestamp: $TS"
  echo "git_commit: $(git rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "git_branch: $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  echo "host: $(hostname 2>/dev/null || echo unknown)"
  echo "contents:"
  ( cd "$DEST" && ls -1 )
} > "$DEST/MANIFEST.txt"

SIZE="$(du -sh "$DEST" 2>/dev/null | awk '{print $1}')"
ok "Backup complete: $DEST ($SIZE)"
echo "$DEST"
