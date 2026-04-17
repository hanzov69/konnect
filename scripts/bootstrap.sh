#!/usr/bin/env bash
# konnect bootstrap — clone + install in one shot.
#
# Designed to be piped from curl:
#
#   curl -fsSL https://raw.githubusercontent.com/hanzov69/konnect/main/scripts/bootstrap.sh | bash
#
# To pass flags through to install.sh, use `bash -s --`:
#
#   curl -fsSL https://raw.githubusercontent.com/hanzov69/konnect/main/scripts/bootstrap.sh | \
#       bash -s -- --no-klipperscreen --konnect-port 7131
#
# Env var overrides (useful since you can't set them after the pipe):
#
#   KONNECT_REPO=https://github.com/you/fork.git  (default: hanzov69/konnect)
#   KONNECT_REF=some-branch-or-tag                 (default: main)
#   KONNECT_DIR=/path/to/checkout                  (default: ~/konnect)
#
# Example:
#
#   curl -fsSL .../bootstrap.sh | KONNECT_REF=0.2.0 bash
set -Eeuo pipefail

REPO="${KONNECT_REPO:-https://github.com/hanzov69/konnect.git}"
REF="${KONNECT_REF:-main}"
DEST="${KONNECT_DIR:-$HOME/konnect}"

red()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }

die()  { red "!! $*"; exit 1; }

# Refuse to run as root — install.sh enforces the same, but fail early
# so we don't waste time cloning first.
[[ $EUID -eq 0 ]] && die "run as the user that owns ~/printer_data, not root"

# Prereqs. python3/pip are checked by install.sh; we only need git + curl
# here (curl was already used to fetch this script, but belt-and-braces).
for bin in git python3; do
  command -v "$bin" >/dev/null 2>&1 || die "missing required command: $bin"
done

grn "==> konnect bootstrap"
echo "    repo:  $REPO"
echo "    ref:   $REF"
echo "    dest:  $DEST"
echo

# Clone fresh or update in place. If $DEST exists and isn't a git
# checkout of $REPO we bail — don't want to clobber unrelated files.
if [[ -d "$DEST/.git" ]]; then
  existing_url="$(git -C "$DEST" remote get-url origin 2>/dev/null || true)"
  if [[ "$existing_url" != "$REPO" ]]; then
    die "$DEST already exists with a different origin ($existing_url); move it aside or set KONNECT_DIR"
  fi
  ylw "==> existing checkout found, updating"
  git -C "$DEST" fetch --tags --prune origin
  git -C "$DEST" checkout "$REF"
  # Only fast-forward if we're on a branch; tags/detached heads are
  # already at the right commit after checkout.
  if git -C "$DEST" symbolic-ref -q HEAD >/dev/null; then
    git -C "$DEST" pull --ff-only origin "$REF"
  fi
elif [[ -e "$DEST" ]]; then
  die "$DEST exists but is not a git checkout; move it aside or set KONNECT_DIR"
else
  grn "==> cloning $REPO → $DEST"
  git clone --branch "$REF" --depth 1 "$REPO" "$DEST" 2>/dev/null || \
    git clone "$REPO" "$DEST"
  # If --depth 1 with a specific branch failed, the fallback clone
  # landed us on the default branch; move to the requested ref.
  git -C "$DEST" checkout "$REF"
fi

grn "==> running install.sh"
echo
exec "$DEST/scripts/install.sh" "$@"
