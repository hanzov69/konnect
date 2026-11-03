#!/usr/bin/env bash
# Build the pinned release asset the Snapmaker U1 firmware mod installs.
#
# extended-pkg on the printer downloads a release ASSET (byte-stable,
# unlike GitHub tag archives), verifies its sha256 and unpacks it to
# konnect-u1-<version>. This script builds that asset from a ref and
# prints the checksum to paste into
# contrib/u1/.../root/usr/local/share/extended-pkg/konnect.
#
# Usage: scripts/release-u1.sh [ref]     (default: HEAD)
set -euo pipefail

REF="${1:-HEAD}"
VERSION=$(git show "$REF:konnect/__init__.py" \
    | sed -n 's/^__version__ = "\(.*\)"$/\1/p')
[[ -n "$VERSION" ]] || { echo "could not read __version__ from $REF"; exit 1; }

NAME="konnect-u1-$VERSION"
mkdir -p dist
git archive --format=zip --prefix="$NAME/" -o "dist/$NAME.zip" "$REF"

echo "asset:   dist/$NAME.zip"
echo "tag:     u1-$VERSION"
echo "sha256:  $(sha256sum "dist/$NAME.zip" | cut -d' ' -f1)"
echo
echo "Next:"
echo "  git tag u1-$VERSION $REF && git push origin u1-$VERSION"
echo "  gh release create u1-$VERSION dist/$NAME.zip --title 'Konnect $VERSION for Snapmaker U1'"
echo "  paste the sha256 into contrib/u1/.../extended-pkg/konnect (PKG_SHA256)"
