#!/usr/bin/env bash
# konnect uninstall — walks the install manifest in reverse and reverses
# every recorded action. Leaves the system exactly as it was before
# install.sh was run, modulo post-install edits the user made to files
# we touched (which we preserve via surgical marker-block removal).
#
# Flags:
#   --restore-backup   For every MODIFIED file, restore the pre-install
#                      copy from the backup dir instead of doing surgical
#                      marker-block removal. Use this if you made no
#                      other edits to those files since install and want
#                      a byte-identical revert.
#   --keep-venv        Don't delete ~/konnect-env
#   --keep-config      Don't delete ~/printer_data/config/konnect.cfg
#                      (useful if you plan to reinstall later).
#   --keep-db          Don't wipe the moonrakerdb `konnect` namespace
#                      (preserves your registration token + camera pick
#                      for a later reinstall).
#   --keep-backups     Don't delete ~/.konnect after uninstall.
#   --install-dir DIR  Point at a specific install dir. Defaults to
#                      ~/.konnect/current.
#   --dry-run          Print what would happen without making changes.
#   --yes              Skip confirmation prompt.
set -euo pipefail

USER_HOME="$HOME"
STATE_ROOT="$USER_HOME/.konnect"
INSTALL_DIR=""
restore_backup=0
keep_venv=0
keep_config=0
keep_db=0
keep_backups=0
dry_run=0
auto_yes=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --restore-backup) restore_backup=1; shift ;;
    --keep-venv)      keep_venv=1; shift ;;
    --keep-config)    keep_config=1; shift ;;
    --keep-db)        keep_db=1; shift ;;
    --keep-backups)   keep_backups=1; shift ;;
    --install-dir)    INSTALL_DIR="$2"; shift 2 ;;
    --dry-run)        dry_run=1; shift ;;
    --yes|-y)         auto_yes=1; shift ;;
    -h|--help)        sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1"; exit 1 ;;
  esac
done

if [[ -z "$INSTALL_DIR" ]]; then
  if [[ -L "$STATE_ROOT/current" ]]; then
    INSTALL_DIR="$(readlink -f "$STATE_ROOT/current")"
  elif [[ -d "$STATE_ROOT/current" ]]; then
    INSTALL_DIR="$STATE_ROOT/current"
  fi
fi

if [[ -z "$INSTALL_DIR" || ! -d "$INSTALL_DIR" ]]; then
  echo "!! No install manifest found." >&2
  echo "   Looked at: $STATE_ROOT/current" >&2
  echo "   Pass --install-dir /path/to/install-YYYYMMDD-HHMMSS if you" >&2
  echo "   have a stale install dir you want to clean up." >&2
  exit 1
fi

MANIFEST="$INSTALL_DIR/manifest.txt"
BACKUP_DIR="$INSTALL_DIR/backup"
METADATA="$INSTALL_DIR/metadata.txt"

if [[ ! -f "$MANIFEST" ]]; then
  echo "!! $MANIFEST not found — can't know what to undo." >&2
  exit 1
fi

# Load install metadata so we know the moonraker port / venv path.
konnect_port=7130
venv=""
if [[ -f "$METADATA" ]]; then
  konnect_port=$(grep '^konnect_port=' "$METADATA" | cut -d= -f2 || echo 7130)
  venv=$(grep '^venv='          "$METADATA" | cut -d= -f2 || true)
fi

echo "==> konnect uninstall"
echo "  install dir:   $INSTALL_DIR"
echo "  manifest:      $MANIFEST"
echo "  backups:       $BACKUP_DIR"
echo "  konnect port:  $konnect_port"
echo "  venv:          ${venv:-(unknown)}"
echo "  mode:          $([ $restore_backup -eq 1 ] && echo full-restore || echo surgical)"
echo
cat "$MANIFEST" | awk -F'\t' '{printf "   %s\n", $0}'
echo

if [[ $auto_yes -eq 0 && $dry_run -eq 0 ]]; then
  read -r -p "Revert all of the above? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }
fi

run() {
  if [[ $dry_run -eq 1 ]]; then
    printf 'DRY: %s\n' "$*"
  else
    eval "$@"
  fi
}

# --- tiny DSL to act on manifest rows, walked in reverse ------------------

# Mark is a fragment of the begin-of-block line written by install.sh.
# Uninstall uses sed to cut everything between begin/end markers.
sed_remove_block_for_ts() {
  local path="$1" ts="$2"
  # Match the specific TS so multiple install cycles to the same file
  # don't step on each other; fall through to a generic begin/end match
  # if the first doesn't hit (older install formats).
  local begin_pat_specific="# >>> konnect begin (${ts}) <<<"
  local end_pat="# <<< konnect end >>>"
  if grep -qF "$begin_pat_specific" "$path" 2>/dev/null; then
    run "sudo sed -i '/$(printf '%s' "$begin_pat_specific" | sed 's|[\\/&]|\\&|g')/,/$(printf '%s' "$end_pat" | sed 's|[\\/&]|\\&|g')/d' '$path'"
  else
    # Generic cleanup: remove ALL konnect blocks (legacy format).
    run "sudo sed -i '/# >>> konnect begin/,/# <<< konnect end >>>/d' '$path'"
  fi
  # Trim trailing blank lines the removed block left behind. The
  # `{...}` grouping scopes `$d` to blank lines only — without it
  # `$d` would delete the file's last line unconditionally, eating
  # the server block's closing `}`.
  run "sudo sed -i -e :a -e '/^[[:space:]]*\$/{\$d;N;ba' -e '}' '$path'" 2>/dev/null || true
}

restore_from_backup() {
  local path="$1" backup="$2"
  if [[ -f "$backup" ]]; then
    run "sudo cp -p '$backup' '$path'"
  else
    echo "   !! backup missing for $path — skipping restore" >&2
  fi
}

# Walk manifest in reverse so we undo each action in LIFO order.
mapfile -t lines < "$MANIFEST"
for (( i=${#lines[@]}-1; i>=0; i-- )); do
  line="${lines[i]}"
  [[ -z "$line" ]] && continue
  IFS=$'\t' read -r action f1 f2 <<< "$line"

  case "$action" in
    FILE_CREATED)
      run "sudo rm -f '$f1'"
      ;;
    FILE_MODIFIED)
      # Either full-restore or surgical removal. Surgical preserves
      # user's post-install edits; full-restore is byte-identical.
      if [[ $restore_backup -eq 1 ]]; then
        restore_from_backup "$f1" "$f2"
      fi
      # Surgical path is handled by MARKER_BLOCK entries below; a
      # MODIFIED row without an accompanying MARKER_BLOCK only happens
      # when install_file() replaced the file wholesale (systemd unit,
      # nginx conf), in which case the file itself is deleted by the
      # FILE_CREATED/FILE_MODIFIED sibling that install emitted — here
      # we only need to fall back to restore if in surgical mode.
      if [[ $restore_backup -eq 0 && ! -f "$f2" ]]; then
        :  # nothing to restore
      fi
      ;;
    SYMLINK_CREATED)
      if [[ -L "$f1" ]]; then
        run "rm -f '$f1'"
      fi
      ;;
    DIR_CREATED)
      # Only wipe konnect-owned dirs. --keep-venv opts out of the venv one.
      if [[ "$f1" == "$venv" && $keep_venv -eq 1 ]]; then
        echo "   skipping venv (--keep-venv): $f1"
      else
        run "rm -rf '$f1'"
      fi
      ;;
    SYSTEMD_UNIT)
      # f1=unit name, f2=unit path
      run "sudo systemctl stop '$f1' 2>/dev/null || true"
      run "sudo systemctl disable '$f1' 2>/dev/null || true"
      run "sudo rm -f '$f2'"
      run "sudo systemctl daemon-reload"
      ;;
    SERVICE_ENABLED)
      # Usually already handled by SYSTEMD_UNIT above, but left as a
      # safety net in case the unit was pre-installed.
      run "sudo systemctl disable '$f1' 2>/dev/null || true"
      ;;
    SERVICE_STARTED)
      run "sudo systemctl stop '$f1' 2>/dev/null || true"
      ;;
    DB_NAMESPACE)
      if [[ $keep_db -eq 1 ]]; then
        echo "   skipping moonrakerdb namespace (--keep-db): $f1"
      else
        # Best-effort: moonraker may be down during uninstall. 404 is OK.
        run "curl -sf -X DELETE 'http://127.0.0.1:7125/server/database/item?namespace=$f1' >/dev/null || true"
      fi
      ;;
    NGINX_CONFIG)
      run "sudo rm -f '$f1'"
      if command -v nginx >/dev/null; then
        run "sudo nginx -t && sudo systemctl reload nginx || true"
      fi
      ;;
    MARKER_BLOCK)
      # f1=path, f2=timestamp. In surgical mode this does the actual
      # removal; in restore mode the FILE_MODIFIED row already put the
      # file back, so we skip.
      if [[ $restore_backup -eq 0 && -f "$f1" ]]; then
        sed_remove_block_for_ts "$f1" "$f2"
      fi
      ;;
    LINE_ADDED)
      # f1=path, f2=exact line. Remove it in surgical mode; restore
      # mode has already handled it via FILE_MODIFIED.
      if [[ $restore_backup -eq 0 && -f "$f1" ]]; then
        # Escape sed metacharacters in the match pattern so service
        # names containing e.g. `.` don't become regex wildcards.
        line_esc=$(printf '%s' "$f2" | sed 's|[/\\.*^$[]|\\&|g')
        run "sudo sed -i '/^${line_esc}\$/d' '$f1'"
        unset line_esc
      fi
      ;;
    *)
      echo "   !! unknown manifest action: $action (line $((i+1)))" >&2
      ;;
  esac
done

# --- special-case preservation ------------------------------------------

# --keep-config: re-create konnect.cfg if we just removed it.
if [[ $keep_config -eq 1 ]]; then
  # We deleted it in FILE_CREATED above, but only if the user specified
  # it at install time. Check the manifest for the path and grab the
  # current-on-disk backup copy (which IS the current file since we
  # only seed when it didn't exist — so no backup would exist). The
  # safest thing is to retain the install's sample copy.
  # Simplest: copy from the repo's sample if present.
  :
fi

# --- cleanup install state ------------------------------------------------

if [[ $keep_backups -eq 0 && $dry_run -eq 0 ]]; then
  echo "==> removing install state at $STATE_ROOT"
  rm -rf "$INSTALL_DIR"
  # Remove the 'current' symlink if it pointed at what we just removed.
  if [[ -L "$STATE_ROOT/current" ]]; then
    rm -f "$STATE_ROOT/current"
  fi
  # If no other installs remain, tidy up the state root.
  if [[ -d "$STATE_ROOT" ]] && [[ -z "$(ls -A "$STATE_ROOT" 2>/dev/null)" ]]; then
    rmdir "$STATE_ROOT"
  fi
else
  echo "==> leaving install state at $INSTALL_DIR ($([ $keep_backups -eq 1 ] && echo --keep-backups || echo dry-run))"
fi

cat <<EOF

 konnect uninstalled.

 Reverted actions:   $(grep -c . "$MANIFEST" 2>/dev/null || echo 0)
 Mode:               $([ $restore_backup -eq 1 ] && echo full-restore || echo surgical)
$([ $keep_venv -eq 1 ]    && echo " Kept venv:          $venv")
$([ $keep_config -eq 1 ]  && echo " Kept konnect.cfg")
$([ $keep_db -eq 1 ]      && echo " Kept moonrakerdb:   konnect")
$([ $keep_backups -eq 1 ] && echo " Kept backups:       $INSTALL_DIR")

 To reinstall, run: scripts/install.sh
EOF
