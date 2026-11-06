#!/bin/bash
# Make Sparkle's update feeds (appcast-arm64.xml, appcast-x86_64.xml) and delta updates for a release.
# Runs on macOS (generate_appcast is a Mac tool) — the GitHub workflow calls it for version tags.
#
#   SPARKLE_PRIVATE_KEY=… packaging/make_appcast.sh v0.7.0 IN_DIR OUT_DIR
#
# IN_DIR holds this release's Confidence-<version>-macOS-<arch>.zip files. For each
# arch the script also downloads the same zip from the previous few releases, so
# generate_appcast can make deltas from them (only the changed bytes: a few MB, not
# the whole app). Delta file names get the arch in front so both arches can live in
# one GitHub release. Release notes come from CHANGELOG.md.
set -euo pipefail
cd "$(dirname "$0")/.."
TAG="$1"; IN="$2"; OUT="$3"
VERSION="${TAG#v}"
REPO="${GITHUB_REPOSITORY:-$(python3 -c 'import sys; sys.path.insert(0, "src"); from dsan import updates; print(updates.REPO)')}"
: "${SPARKLE_PRIVATE_KEY:?set SPARKLE_PRIVATE_KEY (from packaging/setup_signing.sh)}"
DELTA_FROM="${DELTA_FROM:-3}"           # make deltas from this many earlier releases

SPARKLE_DIR="$(packaging/fetch_sparkle.sh)"
mkdir -p "$OUT"
WORK="$(mktemp -d)"
PREVIOUS="$(gh release list --repo "$REPO" --limit 20 --json tagName,isDraft,isPrerelease \
             -q '.[] | select(.isDraft|not) | select(.isPrerelease|not) | .tagName' | grep -vx "$TAG" | head -"$DELTA_FROM" || true)"

made=0
for ARCH in arm64 x86_64; do
  NEW="$IN/Confidence-$VERSION-macOS-$ARCH.zip"
  [ -f "$NEW" ] || { echo "no $NEW — skipping $ARCH"; continue; }
  D="$WORK/$ARCH"
  mkdir -p "$D"
  cp "$NEW" "$D/"
  for T in $PREVIOUS; do
    gh release download "$T" --repo "$REPO" --pattern "Confidence-*-macOS-$ARCH.zip" --dir "$D" 2>/dev/null \
      && echo "   delta base: $T ($ARCH)" || true
  done
  PYTHONPATH=src python3 -c 'import sys; from dsan import version as v; print(v.changelog_section(sys.argv[1]) or "See the release page.")' \
    "$VERSION" > "$D/Confidence-$VERSION-macOS-$ARCH.md"

  FEED="$OUT/appcast-$ARCH.xml"
  echo "$SPARKLE_PRIVATE_KEY" | "$SPARKLE_DIR/bin/generate_appcast" --ed-key-file - \
    --download-url-prefix "https://github.com/$REPO/releases/download/$TAG/" \
    --full-release-notes-url "https://github.com/$REPO/releases/tag/$TAG" \
    --maximum-versions 1 --maximum-deltas "$DELTA_FROM" --embed-release-notes \
    -o "$FEED" "$D"

  for f in "$D"/*.delta; do
    [ -e "$f" ] || continue
    b="$(basename "$f")"
    mv "$f" "$OUT/$ARCH-$b"
    sed -i '' "s|/$b\"|/$ARCH-$b\"|g" "$FEED"      # both arches share delta names otherwise
  done
  echo "==> $FEED"
  grep -o 'sparkle:version="[^"]*"\|url="[^"]*"' "$FEED" | sed 's/^/   /'
  made=$((made + 1))
done
rm -rf "$WORK"
[ "$made" -gt 0 ] || { echo "no Mac update archives found in $IN"; exit 1; }
