#!/bin/bash
# Build "Confidence.app", a .dmg (for people) and a .zip (for Sparkle updates) on macOS.
#
#   packaging/build_macos.sh
#
# Signing — picked up automatically:
#   • If your keychain has a "Developer ID Application" certificate, the app is signed with
#     it (hardened runtime) and Sparkle is built in, so the app updates itself.
#     Override with CODESIGN_IDENTITY="Developer ID Application: Name (TEAMID)", or set
#     ADHOC=1 to skip it.
#   • Otherwise the app is signed ad hoc: it runs on this Mac (on others after
#     right-click → Open) and updates by "Download" from the release page.
#
# Notarizing (needed to give the app to other Macs without warnings) — either
#   NOTARY_PROFILE=<name>            from: xcrun notarytool store-credentials <name> …
#   or NOTARY_KEY_PATH=AuthKey_XXXX.p8 NOTARY_KEY_ID=XXXX NOTARY_ISSUER_ID=uuid   (App Store Connect API key)
#
# Sparkle also needs packaging/sparkle_public_key.txt (packaging/setup_signing.sh makes it).
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-/usr/local/bin/python3}"
[ -x "$PY" ] || PY="$(command -v python3)"
VENV=.build-venv

echo "==> Build environment ($("$PY" --version))"
[ -d "$VENV" ] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet ".[clock]" "pyinstaller>=6.16"

echo "==> Signing identity"
if [ -z "${CODESIGN_IDENTITY:-}" ] && [ -z "${ADHOC:-}" ]; then
  CODESIGN_IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
    | sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' | head -1 || true)"
fi
export CODESIGN_IDENTITY="${CODESIGN_IDENTITY:-}"
if [ -n "$CODESIGN_IDENTITY" ]; then echo "   $CODESIGN_IDENTITY"; else echo "   ad hoc (no Developer ID found)"; fi

ARCH="$(uname -m)"
REPO="$("$VENV/bin/python" -c 'import sys; sys.path.insert(0, "src"); from dsan import updates; print(updates.REPO)')"
SPARKLE_DIR=""
KEYFILE=packaging/sparkle_public_key.txt
if [ -n "$CODESIGN_IDENTITY" ] && [ -s "$KEYFILE" ]; then
  echo "==> Sparkle"
  SPARKLE_DIR="$(packaging/fetch_sparkle.sh)"
  export SPARKLE_PUBLIC_KEY="$(tr -d ' \n\r' < "$KEYFILE")"
  export SPARKLE_FEED_URL="https://github.com/$REPO/releases/latest/download/appcast-$ARCH.xml"
  echo "   feed $SPARKLE_FEED_URL"
elif [ -n "$CODESIGN_IDENTITY" ]; then
  echo "   (no $KEYFILE — run packaging/setup_signing.sh to turn on in-app updates)"
fi

echo "==> DeckLink bridge"
packaging/build_decklink.sh || echo "   (skipped — the app will build without SDI output)"

echo "==> Version"
BUILD="$("$VENV/bin/python" packaging/stamp_build.py)"
trap '"$VENV/bin/python" packaging/stamp_build.py --clean' EXIT   # source runs stay "development"
VERSION="$("$VENV/bin/python" -c 'import sys; sys.path.insert(0, "src"); import dsan; print(dsan.__version__)')"
echo "   $VERSION build $BUILD"

echo "==> Icon"
iconutil -c icns packaging/icon/Confidence.iconset -o packaging/icon/Confidence.icns

echo "==> PyInstaller"
rm -rf build "dist/Confidence" "dist/Confidence.app"
"$VENV/bin/pyinstaller" --noconfirm --clean --log-level WARN packaging/dsan_clock.spec

APP="dist/Confidence.app"
ENT=packaging/entitlements.plist

if [ -n "$CODESIGN_IDENTITY" ]; then
  echo "==> Signing with $CODESIGN_IDENTITY"
  codesign --force --deep --options runtime --timestamp --entitlements "$ENT" --sign "$CODESIGN_IDENTITY" "$APP"
  if [ -n "$SPARKLE_DIR" ]; then
    # Sparkle goes in after the app's own code, and is signed inside-out without --deep
    # (Sparkle's own guidance). The XPC services are only for sandboxed apps.
    FW="$APP/Contents/Frameworks/Sparkle.framework"
    mkdir -p "$APP/Contents/Frameworks"
    rm -rf "$FW"
    ditto "$SPARKLE_DIR/Sparkle.framework" "$FW"
    rm -rf "$FW/Versions/B/XPCServices" "$FW/XPCServices"
    for part in "$FW/Versions/B/Autoupdate" "$FW/Versions/B/Updater.app" "$FW"; do
      codesign --force --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$part"
    done
    codesign --force --options runtime --timestamp --entitlements "$ENT" --sign "$CODESIGN_IDENTITY" "$APP"
  fi
fi
codesign --verify --deep --strict "$APP"

echo "==> Smoke test"
QT_QPA_PLATFORM=offscreen "$APP/Contents/MacOS/Confidence" --smoke-test

notarize() {   # notarize FILE — no-op without credentials
  if [ -n "${NOTARY_PROFILE:-}" ]; then
    xcrun notarytool submit "$1" --keychain-profile "$NOTARY_PROFILE" --wait
  elif [ -n "${NOTARY_KEY_PATH:-}" ] && [ -n "${NOTARY_KEY_ID:-}" ] && [ -n "${NOTARY_ISSUER_ID:-}" ]; then
    xcrun notarytool submit "$1" --key "$NOTARY_KEY_PATH" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER_ID" --wait
  else
    return 1
  fi
}

ZIP="dist/Confidence-$VERSION-macOS-$ARCH.zip"
if [ -n "$CODESIGN_IDENTITY" ]; then
  echo "==> Notarizing the app"
  rm -f "$ZIP"
  ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
  if notarize "$ZIP"; then
    xcrun stapler staple "$APP"
  else
    echo "   (skipped — set NOTARY_PROFILE or NOTARY_KEY_PATH/NOTARY_KEY_ID/NOTARY_ISSUER_ID)"
  fi
fi

echo "==> Update archive (Sparkle)"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"      # after stapling, so it carries the ticket

echo "==> Disk image"
DMG="dist/Confidence-$VERSION-macOS-$ARCH.dmg"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -quiet -volname "Confidence" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"
if [ -n "$CODESIGN_IDENTITY" ]; then
  codesign --sign "$CODESIGN_IDENTITY" --timestamp "$DMG"
  if notarize "$DMG"; then xcrun stapler staple "$DMG"; fi
fi

echo
echo "Built:"
echo "  $APP"
echo "  $DMG"
echo "  $ZIP"
[ -n "$SPARKLE_DIR" ] && echo "  (in-app updates on: $SPARKLE_FEED_URL)"
exit 0
