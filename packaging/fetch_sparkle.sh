#!/bin/bash
# Download Sparkle (sparkle-project.org) into packaging/vendor/Sparkle-<version>/ and print that folder.
#
#   packaging/fetch_sparkle.sh            # → packaging/vendor/Sparkle-2.10.1
#
# It holds Sparkle.framework (bundled into the Mac app) and bin/ (generate_keys,
# generate_appcast, sign_update). Downloaded once; the folder isn't committed.
set -euo pipefail
cd "$(dirname "$0")"
VERSION="${SPARKLE_VERSION:-2.10.1}"
DEST="vendor/Sparkle-$VERSION"
if [ ! -d "$DEST/Sparkle.framework" ]; then
  mkdir -p "$DEST"
  URL="https://github.com/sparkle-project/Sparkle/releases/download/$VERSION/Sparkle-$VERSION.tar.xz"
  echo "Downloading Sparkle $VERSION…" >&2
  curl -fsSL "$URL" | tar -xJ -C "$DEST"
  [ -d "$DEST/Sparkle.framework" ] || { echo "Sparkle.framework not found in $URL" >&2; exit 1; }
fi
echo "$(pwd)/$DEST"
