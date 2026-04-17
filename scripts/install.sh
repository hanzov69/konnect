#!/usr/bin/env bash
# konnect install script — idempotent, with full rollback support.
#
# Every modification is recorded in a manifest at
# ~/.konnect/install-TS/manifest.txt, and every modified file is backed
# up to ~/.konnect/install-TS/backup/ BEFORE we touch it. The companion
# uninstall.sh reads the manifest to reverse every action cleanly.
#
# Flags:
#   --skip-venv          Don't create/update the konnect-env virtualenv
#   --no-klipperscreen   Skip KlipperScreen panel install
#   --no-nginx           Skip nginx snippet install (you'll wire it yourself)
#   --moonraker-conf PATH  Override moonraker.conf location
#   --konnect-port N     Override the Flask port (must match konnect.cfg)
set -Eeuo pipefail

# Louder failure mode: if anything exits non-zero the user gets told
# exactly which line, which command, and where the backup dir is
# (so partial state can be inspected). Without this an install that
# fails in the middle just disappears with no explanation.
_konnect_err() {
  local rc=$?
  echo >&2
  echo "!! install failed (exit $rc):" >&2
  echo "   line: $BASH_LINENO ($BASH_COMMAND)" >&2
  if [[ -n "${INSTALL_DIR:-}" && -d "${INSTALL_DIR:-/nonexistent}" ]]; then
    echo "   partial state:  $INSTALL_DIR" >&2
    echo "   manifest so far:" >&2
    sed 's/^/     /' "$INSTALL_DIR/manifest.txt" 2>/dev/null >&2 || true
    echo >&2
    echo "   To roll back this partial install:" >&2
    echo "      $REPO_DIR/scripts/uninstall.sh --install-dir $INSTALL_DIR --yes" >&2
  fi
  exit "$rc"
}
trap _konnect_err ERR

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(id -un)"
USER_HOME="$HOME"
PRINTER_DATA="${PRINTER_DATA:-$USER_HOME/printer_data}"
CONFIG_DIR="$PRINTER_DATA/config"
VENV="$USER_HOME/konnect-env"
STATE_ROOT="$USER_HOME/.konnect"
TS="$(date +%Y%m%d-%H%M%S)"
INSTALL_DIR="$STATE_ROOT/install-$TS"
BACKUP_DIR="$INSTALL_DIR/backup"
MANIFEST="$INSTALL_DIR/manifest.txt"
MARK_BEGIN="# >>> konnect begin ($TS) <<<"
MARK_END="# <<< konnect end >>>"

skip_venv=0
no_klipperscreen=0
no_nginx=0
moonraker_conf="$CONFIG_DIR/moonraker.conf"
konnect_port=7130

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-venv) skip_venv=1; shift ;;
    --no-klipperscreen) no_klipperscreen=1; shift ;;
    --no-nginx) no_nginx=1; shift ;;
    --moonraker-conf) moonraker_conf="$2"; shift 2 ;;
    --konnect-port) konnect_port="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1"; exit 1 ;;
  esac
done

# ---- sanity checks -------------------------------------------------------

if [[ $EUID -eq 0 ]]; then
  echo "!! do not run as root. Run as the Klipper user ($USER_NAME is fine)." >&2
  exit 1
fi

if [[ ! -d "$PRINTER_DATA" ]]; then
  echo "!! $PRINTER_DATA does not exist." >&2
  echo "   Set PRINTER_DATA=/path/to/printer_data if non-standard." >&2
  exit 1
fi

# ---- manifest helpers ----------------------------------------------------
#
# Manifest is line-oriented — fields separated by TAB so paths with
# spaces round-trip. Uninstall walks it in REVERSE so actions don't
# need to commute (e.g. start-before-enable is fine).
#
# Action vocabulary (all tab-separated):
#   FILE_CREATED   <path>                 → uninstall: rm -f
#   FILE_MODIFIED  <path>  <backup_path>  → uninstall: restore OR surgical
#   SYMLINK_CREATED <path>                → uninstall: rm -f (if symlink)
#   DIR_CREATED    <path>                 → uninstall: rm -rf
#   SYSTEMD_UNIT   <unit>  <path>         → uninstall: stop+disable+rm
#   SERVICE_ENABLED <unit>                → uninstall: systemctl disable
#   SERVICE_STARTED <unit>                → uninstall: systemctl stop
#   DB_NAMESPACE   <namespace>            → uninstall: moonraker DELETE
#   NGINX_CONFIG   <path>                 → uninstall: rm + reload
#   MARKER_BLOCK   <path>  <timestamp>    → uninstall: sed between markers
#   LINE_ADDED     <path>  <line>         → uninstall: sed remove exact line

mkdir -p "$BACKUP_DIR"
: > "$MANIFEST"
# Explicit confirmation so the user sees the backup dir was created
# BEFORE any risky action. If install bombs later, this output + the
# ERR trap tells you where to find the partial state.
echo "==> backup dir: $BACKUP_DIR"

# Emit a tab-separated manifest line.
record() {
  printf '%s' "$1" >> "$MANIFEST"
  shift
  for field in "$@"; do
    printf '\t%s' "$field" >> "$MANIFEST"
  done
  printf '\n' >> "$MANIFEST"
}

# Copy a file to the backup dir if it exists. Returns the backup path
# on stdout (empty if the file didn't exist).
backup_of() {
  local src="$1"
  [[ -f "$src" ]] || return 0
  local key
  key="$(printf '%s' "$src" | sed 's|/|__|g')"
  local dst="$BACKUP_DIR/$key"
  [[ -f "$dst" ]] || cp -p "$src" "$dst"
  printf '%s' "$dst"
}

# Same as backup_of but uses sudo for read access. Needed for files
# under /etc/nginx/sites-* which may be mode 0600 on some distros.
sudo_backup_of() {
  local src="$1"
  sudo test -f "$src" || return 0
  local key
  key="$(printf '%s' "$src" | sed 's|/|__|g')"
  local dst="$BACKUP_DIR/$key"
  if ! [[ -f "$dst" ]]; then
    sudo cat "$src" > "$dst"
    sudo stat -c '%a' "$src" >/dev/null 2>&1 && sudo chmod --reference="$src" "$dst" 2>/dev/null || true
  fi
  printf '%s' "$dst"
}

# Write a brand-new file (or overwrite an existing one after backing it
# up). Records either FILE_CREATED or FILE_MODIFIED.
write_file() {
  local dst="$1"
  local content="$2"
  local use_sudo="${3:-}"
  local backup
  backup="$(backup_of "$dst")"
  if [[ -n "$backup" ]]; then
    record "FILE_MODIFIED" "$dst" "$backup"
  else
    record "FILE_CREATED" "$dst"
  fi
  if [[ "$use_sudo" == "sudo" ]]; then
    printf '%s\n' "$content" | sudo tee "$dst" >/dev/null
  else
    printf '%s\n' "$content" > "$dst"
  fi
}

# Copy repo file into place (with backup record).
install_file() {
  local dst="$1" src="$2"
  local backup
  backup="$(backup_of "$dst")"
  if [[ -n "$backup" ]]; then
    record "FILE_MODIFIED" "$dst" "$backup"
  else
    record "FILE_CREATED" "$dst"
  fi
  cp -p "$src" "$dst"
}

# Strip sections from `new` that already exist in `target`. Moonraker's
# config is ConfigParser-compatible and rejects duplicate section
# headers, so we have to merge rather than blindly append. Comments
# and key-value lines belonging to a dropped section are dropped with it.
#
# Usage: filter_new_sections <target.conf> <new-content-string>
#        → prints the filtered content on stdout
filter_new_sections() {
  local target="$1"
  local new_content="$2"
  python3 - "$target" <<PY
import configparser, re, sys
target = sys.argv[1]
new = """$new_content"""

existing = set()
if target and __import__("os").path.isfile(target):
    parser = configparser.RawConfigParser(strict=False)
    try:
        parser.read(target)
        existing = {s.lower() for s in parser.sections()}
    except configparser.Error:
        # If target is already broken we bail out and let the caller
        # decide; appending more won't make it worse.
        pass

out = []
include = True  # Section state: True until we hit a [header] that's a dup
section_re = re.compile(r'^\s*\[([^\]]+)\]\s*$')
skipped = []
for line in new.splitlines():
    m = section_re.match(line)
    if m:
        name = m.group(1).strip().lower()
        if name in existing:
            include = False
            skipped.append(m.group(1).strip())
            continue
        include = True
    if include:
        out.append(line)

sys.stdout.write("\n".join(out).rstrip() + "\n")
if skipped:
    sys.stderr.write("   skipped sections already present: %s\n" % ", ".join(skipped))
PY
}

# Run ConfigParser over a file to verify nothing we appended broke
# parseability (most common failure: duplicate section headers).
# Returns 0 on OK, non-zero with a human-readable error on stderr.
validate_config() {
  local path="$1"
  python3 - "$path" <<'PY'
import configparser, sys
path = sys.argv[1]
try:
    parser = configparser.RawConfigParser(strict=True)
    parser.read(path)
except configparser.Error as e:
    sys.stderr.write(f"parse error: {e}\n")
    sys.exit(1)
PY
}

# Append marker-delimited content. Records MARKER_BLOCK so uninstall
# can do a surgical removal (preserving any unrelated edits the user
# made to the file post-install). Filters out sections already present
# in `path` and validates the result; rolls back on parse failure.
append_marked() {
  local path="$1"
  local content="$2"
  local backup
  backup="$(backup_of "$path")"
  if [[ -n "$backup" ]]; then
    record "FILE_MODIFIED" "$path" "$backup"
  else
    record "FILE_CREATED" "$path"
    touch "$path"
  fi

  # Strip sections that already exist in the target file. Without this
  # we risk appending a duplicate [authorization] etc. which breaks
  # moonraker's parser.
  local filtered
  filtered="$(filter_new_sections "$path" "$content")"
  if [[ -z "$(printf '%s' "$filtered" | tr -d '[:space:]')" ]]; then
    echo "   nothing new to add to $path (all sections already present)"
    # Drop the FILE_MODIFIED record we just wrote, since we're not
    # actually going to modify.
    sed -i.bak '$d' "$MANIFEST" && rm -f "$MANIFEST.bak"
    return 0
  fi

  # Ensure trailing newline so our marker starts on its own line.
  if [[ -s "$path" && -n "$(tail -c 1 "$path")" ]]; then
    printf '\n' >> "$path"
  fi
  {
    printf '%s\n' "$MARK_BEGIN"
    printf '%s\n' "$filtered"
    printf '%s\n' "$MARK_END"
  } >> "$path"
  record "MARKER_BLOCK" "$path" "$TS"

  # Validate the resulting file still parses. If not, roll back from
  # backup (if we had one) or truncate (if the file was brand new).
  if ! err="$(validate_config "$path" 2>&1)"; then
    echo "   !! $path failed to parse after append:" >&2
    echo "      $err" >&2
    echo "   rolling back the append — your original file is preserved." >&2
    if [[ -n "$backup" ]]; then
      cp -p "$backup" "$path"
    else
      rm -f "$path"
    fi
    # Remove the MARKER_BLOCK + FILE_MODIFIED/FILE_CREATED rows we just
    # emitted, so uninstall doesn't try to undo a no-op.
    sed -i.bak -e '$d' -e '$d' "$MANIFEST" && rm -f "$MANIFEST.bak"
    return 1
  fi
}

# Append an exact line to a file if not already present. For files that
# aren't ConfigParser-compatible (e.g. /etc/moonraker.asvc — bare
# service names, one per line, no comments). Records LINE_ADDED so
# uninstall removes just that line.
append_line_if_missing() {
  local path="$1" line="$2" use_sudo="${3:-}"
  local run_prefix=""
  [[ "$use_sudo" == "sudo" ]] && run_prefix="sudo"

  if [[ -f "$path" ]] && grep -qxF "$line" "$path"; then
    echo "   $path already contains \"$line\""
    return 0
  fi

  local backup
  backup="$(backup_of "$path")"
  if [[ -n "$backup" ]]; then
    record "FILE_MODIFIED" "$path" "$backup"
  else
    record "FILE_CREATED" "$path"
    $run_prefix touch "$path"
  fi

  # Ensure trailing newline so our line doesn't land on the same line
  # as the last service name.
  if [[ -s "$path" && -n "$($run_prefix tail -c 1 "$path")" ]]; then
    printf '\n' | $run_prefix tee -a "$path" >/dev/null
  fi
  printf '%s\n' "$line" | $run_prefix tee -a "$path" >/dev/null
  record "LINE_ADDED" "$path" "$line"
}

# Create a symlink, recording any file/link it replaced.
create_symlink() {
  local link="$1" target="$2"
  if [[ -L "$link" || -f "$link" ]]; then
    local backup
    backup="$(backup_of "$link")"
    [[ -n "$backup" ]] && record "FILE_MODIFIED" "$link" "$backup"
    rm -f "$link"
  fi
  ln -sf "$target" "$link"
  record "SYMLINK_CREATED" "$link"
}

# ---- metadata ------------------------------------------------------------

cat > "$INSTALL_DIR/metadata.txt" <<META
install_version=1
konnect_repo=$REPO_DIR
user=$USER_NAME
home=$USER_HOME
printer_data=$PRINTER_DATA
venv=$VENV
moonraker_conf=$moonraker_conf
konnect_port=$konnect_port
install_ts=$TS
hostname=$(hostname)
META
# A stable "active install" pointer; uninstall reads it.
ln -sfn "$INSTALL_DIR" "$STATE_ROOT/current"

echo "==> konnect install ($TS)"
echo "  user:            $USER_NAME"
echo "  home:            $USER_HOME"
echo "  printer_data:    $PRINTER_DATA"
echo "  repo:            $REPO_DIR"
echo "  venv:            $VENV"
echo "  moonraker.conf:  $moonraker_conf"
echo "  backup dir:      $INSTALL_DIR"

# ---- 1. virtualenv --------------------------------------------------------

if [[ $skip_venv -eq 0 ]]; then
  echo "==> creating/updating virtualenv at $VENV"
  if [[ ! -d "$VENV" ]]; then
    python3 -m venv "$VENV"
    record "DIR_CREATED" "$VENV"
  fi
  "$VENV/bin/pip" install --upgrade pip wheel >/dev/null
  "$VENV/bin/pip" install -e "$REPO_DIR"

  # Sanity: resolve the package and make sure it's the REAL one (has
  # __init__.py / __version__), not a namespace-package shadow picked
  # up from CWD. We `cd /` first because if the user ran install.sh
  # from, say, $HOME (which contains a `konnect/` dir — the repo),
  # Python's sys.path[0] = CWD would find that dir as an implicit
  # namespace package with no __version__, and the import would
  # silently resolve to the wrong thing.
  echo "==> verifying konnect is importable from $VENV"
  if ! import_info="$(cd / && "$VENV/bin/python" -c '
import konnect, sys
if not getattr(konnect, "__file__", None):
    sys.exit("konnect resolved as a namespace package (no __init__.py). "
             "sys.path=" + repr(sys.path))
print(konnect.__version__, konnect.__file__)' 2>&1)"; then
    echo "!! konnect failed to import from the venv:" >&2
    echo "   $import_info" >&2
    echo "   Usual causes:" >&2
    echo "   - stale egg-info in the repo. Try:" >&2
    echo "       rm -rf $REPO_DIR/*.egg-info $VENV && $REPO_DIR/scripts/install.sh" >&2
    echo "   - a stray 'konnect' dir on sys.path" >&2
    exit 1
  fi
  echo "   $import_info"
  # Console script exists + runs (what systemd actually invokes).
  if ! [[ -x "$VENV/bin/konnect" ]]; then
    echo "!! $VENV/bin/konnect missing after pip install. Check" >&2
    echo "   pyproject.toml has [project.scripts] konnect=..." >&2
    exit 1
  fi
  if ! (cd / && "$VENV/bin/konnect" --version) >/dev/null 2>&1; then
    echo "!! $VENV/bin/konnect --version failed — see above" >&2
    exit 1
  fi
fi

# ---- 2. config seed -------------------------------------------------------

mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/konnect.cfg" ]]; then
  echo "==> installing sample konnect.cfg"
  install_file "$CONFIG_DIR/konnect.cfg" "$REPO_DIR/scripts/konnect.cfg.sample"
else
  echo "   $CONFIG_DIR/konnect.cfg exists — leaving it alone"
fi

# ---- 3. systemd unit ------------------------------------------------------

echo "==> installing systemd unit (sudo required)"
UNIT_DST="/etc/systemd/system/konnect.service"
UNIT_CONTENT="$(cat <<EOF
[Unit]
Description=konnect — Prusa Connect bridge for Klipper/Moonraker
After=network-online.target moonraker.service
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
# Stay out of the repo/home dir. Running from $VENV avoids adding
# \$USER_HOME to sys.path[0], which would make Python find the repo
# dir (/home/user/konnect) as a namespace package before the real
# package inside it — shadowing it and breaking relative imports.
WorkingDirectory=$VENV
Environment=PYTHONUNBUFFERED=1
# Use the pip-installed console script entry point (defined in
# pyproject.toml) instead of \`python -m konnect\` so sys.path never
# picks up CWD. This is the pip-blessed way to launch installed apps.
ExecStart=$VENV/bin/konnect
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
)"
write_file "$UNIT_DST" "$UNIT_CONTENT" sudo
record "SYSTEMD_UNIT" "konnect.service" "$UNIT_DST"
sudo systemctl daemon-reload
sudo systemctl enable konnect.service >/dev/null
record "SERVICE_ENABLED" "konnect.service"

# Grant Moonraker permission to control the konnect systemd unit.
# Without this, Moonraker's update_manager can't restart konnect and
# KlipperScreen surfaces a "not permitted to restart service" warning.
# Moonraker reads this file as a plain list of allowed unit names, one
# per line.
#
# Location varies by install:
#   MainsailOS (recent): $PRINTER_DATA/moonraker.asvc   (user-owned)
#   Legacy / manual:     /etc/moonraker.asvc            (root-owned)
# Moonraker checks the printer_data path first, so if both exist the
# one under printer_data wins. We match that order — if printer_data
# has one, update it; else if /etc has one, update it; else create at
# printer_data (matches current convention, no sudo needed).
asvc_pd="$PRINTER_DATA/moonraker.asvc"
asvc_etc="/etc/moonraker.asvc"
if [[ -f "$asvc_pd" ]]; then
  echo "==> adding 'konnect' to $asvc_pd (Moonraker allowed_services)"
  append_line_if_missing "$asvc_pd" "konnect"
elif [[ -f "$asvc_etc" ]]; then
  echo "==> adding 'konnect' to $asvc_etc (Moonraker allowed_services)"
  append_line_if_missing "$asvc_etc" "konnect" sudo
else
  echo "==> creating $asvc_pd with 'konnect' (Moonraker allowed_services)"
  append_line_if_missing "$asvc_pd" "konnect"
fi

# ---- 4. nginx snippet -----------------------------------------------------

if [[ $no_nginx -eq 0 ]]; then
  if command -v nginx >/dev/null && [[ -d /etc/nginx ]]; then
    echo "==> installing nginx location block"
    # We inline the entire `location /konnect/ { ... }` block directly
    # into the Mainsail/Fluidd server config, wrapped in a begin/end
    # marker. Previous versions dropped a separate snippet in
    # /etc/nginx/snippets/ and injected an `include` — that broke on
    # configs where nginx auto-includes snippets/*.conf at http scope,
    # because the snippet would load outside any server{} block.
    # Inlining sidesteps this entirely: the location block only exists
    # inside the server{} it belongs to, nowhere else.
    NGINX_CONTENT="$(cat <<EOF
location /konnect/ {
    proxy_pass http://127.0.0.1:$konnect_port/;
    proxy_http_version 1.1;
    proxy_set_header Host \$http_host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 600;
}
EOF
)"

    # Find the Mainsail/Fluidd server config to inject into.
    site=""
    for candidate in \
        /etc/nginx/sites-enabled/mainsail \
        /etc/nginx/sites-enabled/fluidd \
        /etc/nginx/sites-enabled/default \
        /etc/nginx/sites-available/mainsail \
        /etc/nginx/sites-available/fluidd; do
      if [[ -f "$candidate" ]]; then
        site="$candidate"
        break
      fi
    done

    if [[ -z "$site" ]]; then
      echo "   !! couldn't find Mainsail/Fluidd site config in /etc/nginx/sites-*"
      echo "      Skipping nginx configuration. You can still reach konnect"
      echo "      directly at http://<printer>:$konnect_port/ — the proxy"
      echo "      at /konnect/ just won't be set up."
    else
      echo "   injecting location /konnect/ into $site"
      backup="$(sudo_backup_of "$site")"
      [[ -n "$backup" ]] && record "FILE_MODIFIED" "$site" "$backup"

      # Safe injection:
      #   1. sudo cat the source to a tempfile (readable regardless of
      #      the site file's perms — previous version piped python's
      #      open() straight into `sudo tee`, which truncated the
      #      target whenever python couldn't read or errored).
      #   2. awk-insert our marker block before the last top-level `}`.
      #   3. Validate the output is non-empty AND contains our marker.
      #   4. Atomically swap in via sudo install (preserves owner/mode).
      tmp_in=$(mktemp)
      tmp_out=$(mktemp)
      # shellcheck disable=SC2064
      trap "rm -f '$tmp_in' '$tmp_out'" RETURN
      sudo cat "$site" > "$tmp_in"
      if [[ ! -s "$tmp_in" ]]; then
        echo "   !! $site is empty; refusing to touch it" >&2
        rm -f "$tmp_in" "$tmp_out"
        exit 1
      fi

      # Brace-aware injector: walks the nginx config, tracking which
      # directive opened each `{`, and inserts our include just before
      # the `}` that closes the outermost `server{}` block. Handles
      # comments, strings, and nested `location {}` correctly.
      #
      # If no `server{}` found, we ABORT rather than prepending at top —
      # a file that's actually a location-fragment-included-from-elsewhere
      # is rare enough that silently doing-something-different is worse
      # than failing loudly so a human can diagnose.
      py_stderr=$(mktemp)
      # Wrap the python call in `if cmd; then success; else error`
      # so `set -e` doesn't exit before we can surface the parser's
      # stderr. Note the logic: run the python, branch on its exit
      # code — success path is a no-op, failure path prints diagnostics.
      if python3 - "$tmp_in" "$tmp_out" "$MARK_BEGIN" "$MARK_END" "$NGINX_CONTENT" 2>"$py_stderr" <<'PY'
import sys

src, dst, begin, end, location_block = sys.argv[1:6]
# Read tolerantly: some configs have a UTF-8 BOM (usually from being
# saved in a Windows editor) or unusual encodings. `errors="replace"`
# turns decode failures into the replacement char rather than crashing,
# and we explicitly strip a leading BOM so the directive-name match
# for `server` works even then.
with open(src, encoding="utf-8", errors="replace") as f:
    text = f.read()
if text.startswith("\ufeff"):
    text = text[1:]
    sys.stderr.write("stripped UTF-8 BOM from site config\n")

def find_server_close(text: str) -> int:
    """Return the char index of the `}` that closes the outermost
    `server {}` block, or -1 if no server block found.

    Tracks context so the close of an inner location/if/map block
    can't be confused with the server's close. Skips `# ...` comments
    and quoted strings.
    """
    i, n = 0, len(text)
    # Stack of directive names at each currently-open `{`.
    stack = []
    while i < n:
        c = text[i]
        # Comment → skip to newline
        if c == '#':
            nl = text.find('\n', i)
            i = n if nl == -1 else nl + 1
            continue
        # Quoted string → skip to matching quote
        if c in ('"', "'"):
            quote = c
            i += 1
            while i < n:
                if text[i] == '\\' and i + 1 < n:
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c == '{':
            # Look back to the start of this directive line to get
            # its name. The directive name is the first word after
            # the last `;` or `{` or `}` or start-of-file.
            j = i - 1
            while j >= 0:
                if text[j] in ';{}':
                    break
                j -= 1
            head = text[j + 1:i].strip()
            first_word = head.split()[0] if head else ''
            stack.append(first_word)
            i += 1
            continue
        if c == '}':
            if stack:
                popped = stack.pop()
                # Closing the outermost server block.
                if popped == 'server' and not stack:
                    return i
            i += 1
            continue
        i += 1
    return -1

server_close = find_server_close(text)
# Indent each line of the inlined location block to match typical
# server{} indentation (4 spaces).
indented_block = "\n".join("    " + line if line else line
                           for line in location_block.splitlines())
insertion_body = f"    {begin}\n{indented_block}\n    {end}\n"

if server_close < 0:
    # No server{} detected. Bail out with a diagnostic dump so the
    # caller (install.sh) can print the first/last lines of the file
    # and the user can share them with us. Prepending-at-top of an
    # http-scope file would put `location` at http scope → nginx error.
    sys.stderr.write(
        "ERROR: no outermost `server {}` block detected in the site file.\n"
        "Parser state dump (first 10 `{`/`}` events):\n"
    )
    # Re-run with a trace for debugging
    i, n, events = 0, len(text), []
    stack = []
    while i < n and len(events) < 20:
        c = text[i]
        if c == '#':
            nl = text.find('\n', i); i = n if nl == -1 else nl + 1; continue
        if c in ('"', "'"):
            q = c; i += 1
            while i < n:
                if text[i] == '\\' and i + 1 < n: i += 2; continue
                if text[i] == q: i += 1; break
                i += 1
            continue
        if c == '{':
            j = i - 1
            while j >= 0 and text[j] not in ';{}': j -= 1
            head = text[j + 1:i].strip()
            word = head.split()[0] if head else '(none)'
            stack.append(word)
            events.append(f"  {{  depth→{len(stack)}  opened by '{word}'")
        elif c == '}':
            word = stack.pop() if stack else '(empty)'
            events.append(f"  }}  depth→{len(stack)}  closed '{word}'")
        i += 1
    sys.stderr.write("\n".join(events) + "\n")
    sys.exit(3)

# Insert just before the server's `}`, keeping marker block indented
# one level. Ensure we start on a fresh line.
pos = server_close
leading_nl = "" if pos == 0 or text[pos - 1] == "\n" else "\n"
new_text = text[:pos] + leading_nl + "\n" + insertion_body + text[pos:]
sys.stderr.write(f"server{{}} close found at offset {pos}\n")

with open(dst, "w") as f:
    f.write(new_text)
PY
      then
        :
      else
        py_rc=$?
        echo "   !! nginx injector failed (rc=$py_rc):" >&2
        sed 's/^/     /' "$py_stderr" >&2
        echo "   Please share the trace above plus:" >&2
        echo "      sudo head -c 20 $site | od -c | head -1" >&2
        echo "      sudo file $site" >&2
        rm -f "$tmp_in" "$tmp_out" "$py_stderr"
        exit 1
      fi

      if [[ ! -s "$tmp_out" ]] || ! grep -qF "$MARK_BEGIN" "$tmp_out"; then
        echo "   !! injection failed (output $(wc -c < "$tmp_out") bytes, marker absent)" >&2
        sed 's/^/     /' "$py_stderr" >&2
        echo "      leaving $site untouched." >&2
        rm -f "$tmp_in" "$tmp_out" "$py_stderr"
        exit 1
      fi
      rm -f "$py_stderr"

      # Preserve original ownership & mode when swapping in.
      sudo install -m "$(stat -c '%a' "$tmp_in" 2>/dev/null || echo 644)" \
                   --preserve-timestamps "$tmp_out" "$site" 2>/dev/null || \
        sudo cp -p "$tmp_out" "$site"
      rm -f "$tmp_in" "$tmp_out"

      record "MARKER_BLOCK" "$site" "$TS"

      # Validate the FULL nginx config now. Roll back the site file if
      # nginx rejects the result, so we never leave the printer with a
      # broken web UI.
      if ! nginx_err="$(sudo nginx -t 2>&1)"; then
        echo "   !! nginx -t failed after injection — rolling back:" >&2
        echo "$nginx_err" | sed 's/^/     /' >&2
        # Surgical removal: strip our marker block from the site file.
        sudo sed -i '/# >>> konnect begin/,/# <<< konnect end >>>/d' "$site"
        # Drop the MARKER_BLOCK + FILE_MODIFIED rows we just recorded
        # so uninstall doesn't try to undo something already undone.
        sed -i.bak -e '$d' -e '$d' "$MANIFEST" && rm -f "$MANIFEST.bak"
        sudo nginx -t >/dev/null 2>&1 && sudo systemctl reload nginx || true
        echo "      Rolled back. Your Mainsail/Fluidd UI should work again." >&2
        echo "      Re-run install with --no-nginx to skip this step, or" >&2
        echo "      share $site so we can fix the parser." >&2
        exit 1
      fi
      sudo systemctl reload nginx || true
    fi
  else
    echo "   nginx not installed — skipping snippet"
  fi
fi

# ---- 5. moonraker update_manager entry ------------------------------------

if [[ -f "$moonraker_conf" ]] && grep -q '^\[update_manager konnect\]' "$moonraker_conf"; then
  echo "   [update_manager konnect] already present in $moonraker_conf"
else
  echo "==> appending [update_manager konnect] to $moonraker_conf"
  MR_CONTENT="$(cat "$REPO_DIR/scripts/konnect-moonraker.cfg")"
  append_marked "$moonraker_conf" "$MR_CONTENT"
fi

# ---- 6. KlipperScreen panel ----------------------------------------------

if [[ $no_klipperscreen -eq 0 ]]; then
  ks_root="$USER_HOME/KlipperScreen"
  ks_conf="$CONFIG_DIR/KlipperScreen.conf"
  if [[ -d "$ks_root/panels" ]]; then
    echo "==> installing KlipperScreen panel"
    create_symlink "$ks_root/panels/konnect.py" \
                   "$REPO_DIR/klipperscreen/panels/konnect.py"
    install_file "$CONFIG_DIR/KlipperScreen-konnect.conf" \
                 "$REPO_DIR/klipperscreen/konnect.conf"
    if [[ -f "$ks_conf" ]] && ! grep -q "KlipperScreen-konnect.conf" "$ks_conf"; then
      # KlipperScreen's save_user_config_options rewrites this file
      # on shutdown, preserving ONLY lines above the
      # "#~# --- Do not edit below this line ---" marker (and the
      # #~# auto-gen section below it). Anything appended to the
      # END of the file gets wiped on the next shutdown. So we
      # insert our `[include ...]` BEFORE that marker, not after.
      backup="$(backup_of "$ks_conf")"
      [[ -n "$backup" ]] && record "FILE_MODIFIED" "$ks_conf" "$backup"
      python3 - "$ks_conf" "$MARK_BEGIN" "$MARK_END" <<'PY'
import sys
from pathlib import Path
path, begin, end = sys.argv[1:4]
DO_NOT_EDIT = "#~# --- Do not edit below this line. This section is auto generated --- #~#"
BLOCK = f"\n{begin}\n[include KlipperScreen-konnect.conf]\n{end}\n"
p = Path(path)
lines = p.read_text().splitlines(keepends=True)
out, inserted = [], False
for line in lines:
    if line.rstrip() == DO_NOT_EDIT and not inserted:
        out.append(BLOCK.lstrip("\n"))
        inserted = True
    out.append(line)
if not inserted:
    # No auto-gen section yet — safe to append at EOF.
    out.append(BLOCK)
p.write_text("".join(out))
PY
      record "MARKER_BLOCK" "$ks_conf" "$TS"
    fi
    # Install qrcode[pil] into KlipperScreen's venv. Stock KlipperScreen
    # uses ~/.KlipperScreen-env/ (dotfile prefix); earlier custom forks
    # put it at ~/KlipperScreen/.env/ — check both. The [pil] extra
    # pulls in Pillow, which qrcode needs to render the registration
    # QR image at runtime.
    for ks_pip in \
        "$USER_HOME/.KlipperScreen-env/bin/pip" \
        "$ks_root/.env/bin/pip"; do
      if [[ -x "$ks_pip" ]]; then
        echo "   installing qrcode[pil] into $(dirname "$(dirname "$ks_pip")")"
        "$ks_pip" install 'qrcode[pil]' requests >/dev/null 2>&1 || true
        break
      fi
    done
  else
    echo "   KlipperScreen not found at $ks_root — skipping panel"
  fi
fi

# ---- 7. start service -----------------------------------------------------

echo "==> starting konnect"
sudo systemctl restart konnect.service
record "SERVICE_STARTED" "konnect.service"
# Wait for /status to come up so later camera discovery works on first visit.
for _ in $(seq 1 15); do
  if curl -sf "http://127.0.0.1:$konnect_port/status" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# moonrakerdb namespace gets populated on first run — record so uninstall wipes it.
record "DB_NAMESPACE" "konnect"

sudo systemctl --no-pager --lines=10 status konnect.service || true

cat <<EOF

 konnect installed ($TS).

 Manifest:  $MANIFEST
 Backups:   $BACKUP_DIR
 Active:    $STATE_ROOT/current -> $INSTALL_DIR

 1. Edit    $CONFIG_DIR/konnect.cfg
    — set printer_type = HT90 if you have a heated chamber, else keep
      printer_type = I3MK3S (default).
 2. Open    http://<printer-ip>/konnect/   (via nginx proxy)
    or     http://<printer-ip>:$konnect_port/   (direct)
 3. Register and pick a webcam stream.
 4. Done — Connect now sees this printer.

 Logs:      journalctl -u konnect -f
 Panel:     KlipperScreen "Prusa Connect" tile in the main menu.
 Uninstall: $REPO_DIR/scripts/uninstall.sh
EOF
