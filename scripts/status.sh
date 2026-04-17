#!/usr/bin/env bash
# konnect status — read-only diagnostic. Reports whether install state
# exists, what's in the manifest, and whether each manifest-recorded
# object is still present. Run anytime to verify your install is intact.
set -euo pipefail

USER_HOME="$HOME"
STATE_ROOT="$USER_HOME/.konnect"

echo "==> konnect state"
echo

# 1. State root
if [[ -d "$STATE_ROOT" ]]; then
  echo "  ✓ $STATE_ROOT exists"
else
  echo "  ✗ $STATE_ROOT does NOT exist — no install has been recorded."
  echo "    (If you installed an old pre-0.1.1 version, it had no backup"
  echo "     support. Re-run scripts/install.sh from the latest bundle.)"
  exit 1
fi

# 2. Current-install pointer
if [[ -L "$STATE_ROOT/current" ]]; then
  active="$(readlink -f "$STATE_ROOT/current")"
  echo "  ✓ current install: $active"
elif [[ -d "$STATE_ROOT/current" ]]; then
  active="$STATE_ROOT/current"
  echo "  ✓ current install: $active"
else
  echo "  ✗ $STATE_ROOT/current pointer missing."
  echo "    Partial install? Look in $STATE_ROOT for install-TS dirs:"
  ls -1 "$STATE_ROOT" 2>/dev/null | sed 's/^/      /'
  exit 1
fi
echo

# 3. Metadata
if [[ -f "$active/metadata.txt" ]]; then
  echo "  metadata:"
  sed 's/^/    /' "$active/metadata.txt"
  echo
fi

# 4. Manifest contents + per-row presence check
manifest="$active/manifest.txt"
if [[ ! -f "$manifest" ]]; then
  echo "  ✗ manifest missing at $manifest"
  exit 1
fi

echo "  manifest actions: $(grep -c . "$manifest" || true)"
echo
echo "  recorded state (✓ = still present, ✗ = missing):"

while IFS=$'\t' read -r action f1 f2; do
  [[ -z "$action" ]] && continue
  case "$action" in
    FILE_CREATED|FILE_MODIFIED)
      if [[ -f "$f1" ]]; then
        printf '    ✓ %-20s %s\n' "$action" "$f1"
      else
        printf '    ✗ %-20s %s  (file gone)\n' "$action" "$f1"
      fi
      ;;
    SYMLINK_CREATED)
      if [[ -L "$f1" ]]; then
        tgt="$(readlink "$f1" 2>/dev/null || true)"
        printf '    ✓ %-20s %s -> %s\n' "$action" "$f1" "$tgt"
      else
        printf '    ✗ %-20s %s  (symlink gone)\n' "$action" "$f1"
      fi
      ;;
    DIR_CREATED)
      if [[ -d "$f1" ]]; then
        printf '    ✓ %-20s %s\n' "$action" "$f1"
      else
        printf '    ✗ %-20s %s  (dir gone)\n' "$action" "$f1"
      fi
      ;;
    SYSTEMD_UNIT)
      state=$(systemctl is-enabled "$f1" 2>/dev/null || echo "unknown")
      active_state=$(systemctl is-active "$f1" 2>/dev/null || echo "unknown")
      printf '    · %-20s %s  (enabled=%s active=%s)\n' "$action" "$f1" "$state" "$active_state"
      ;;
    SERVICE_ENABLED|SERVICE_STARTED)
      ;;  # covered by SYSTEMD_UNIT
    DB_NAMESPACE)
      # Best-effort moonraker API probe.
      port=7125
      if [[ -f "$active/metadata.txt" ]]; then
        port=$(grep '^konnect_port=' "$active/metadata.txt" | cut -d= -f2 || echo 7125)
      fi
      if curl -sf "http://127.0.0.1:7125/server/database/item?namespace=$f1" >/dev/null 2>&1; then
        printf '    ✓ %-20s moonrakerdb namespace %s (populated)\n' "$action" "$f1"
      else
        printf '    · %-20s moonrakerdb namespace %s (not found or moonraker down)\n' "$action" "$f1"
      fi
      ;;
    NGINX_CONFIG)
      if sudo test -f "$f1" 2>/dev/null || [[ -f "$f1" ]]; then
        printf '    ✓ %-20s %s\n' "$action" "$f1"
      else
        printf '    ✗ %-20s %s  (gone)\n' "$action" "$f1"
      fi
      ;;
    MARKER_BLOCK)
      if grep -qF "konnect begin ($f2)" "$f1" 2>/dev/null || sudo grep -qF "konnect begin ($f2)" "$f1" 2>/dev/null; then
        printf '    ✓ %-20s %s  (ts=%s)\n' "$action" "$f1" "$f2"
      else
        printf '    ✗ %-20s %s  (marker ts=%s missing)\n' "$action" "$f1" "$f2"
      fi
      ;;
    LINE_ADDED)
      if grep -qxF "$f2" "$f1" 2>/dev/null || sudo grep -qxF "$f2" "$f1" 2>/dev/null; then
        printf '    ✓ %-20s %s line=%s\n' "$action" "$f1" "$f2"
      else
        printf '    ✗ %-20s %s line=%s (missing)\n' "$action" "$f1" "$f2"
      fi
      ;;
    *)
      printf '    ? %-20s %s %s\n' "$action" "$f1" "$f2"
      ;;
  esac
done < "$manifest"

echo

# 5. Backup contents
backups="$active/backup"
if [[ -d "$backups" ]]; then
  count=$(find "$backups" -maxdepth 1 -type f | wc -l | tr -d ' ')
  echo "  backups: $count files in $backups"
  ls -1 "$backups" 2>/dev/null | sed 's|__|/|g; s/^/    /'
fi
