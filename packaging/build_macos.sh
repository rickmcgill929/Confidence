#!/bin/bash
# Build "Confidence.app" and a .dmg on macOS.
#
#   packaging/build_macos.sh
#
# Optional, to share the app with other Macs without Gatekeeper warnings:
#   CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
#   NOTARY_PROFILE=<name>   # from: xcrun notarytool store-credentials <name> ...
#
# Without them the app is signed ad hoc: it runs on this Mac (and on others
# after right-click → Open).
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-/usr/local/bin/python3}"
[ -x "$PY" ] || PY="$(command -v python3)"
VENV=.build-venv

echo "==> Build environment ($("$PY" --version))"
[ -d "$VENV" ] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet ".[clock]" "pyinstaller>=6.16"

echo "==> DeckLink bridge"
packaging/build_decklink.sh || echo "   (skipped — the app will build without SDI output)"

echo "==> Version"
BUILD="$("$VENV/bin/python" packaging/stamp_build.py)"
trap '"$VENV/bin/python" packaging/stamp_build.py --clean' EXIT   # source runs stay "development"
echo "   $("$VENV/bin/python" -c 'import sys; sys.path.insert(0, "src"); import dsan; print(dsan.__version__)') build $BUILD"

echo "==> Icon"
iconutil -c icns packaging/icon/Confidence.iconset -o packaging/icon/Confidence.icns

echo "==> PyInstaller"
rm -rf build "dist/Confidence" "dist/Confidence.app"
"$VENV/bin/pyinstaller" --noconfirm --clean --log-level WARN packaging/dsan_clock.spec

APP="dist/Confidence.app"
VERSION="$("$VENV/bin/python" -c 'import dsan; print(dsan.__version__)')"
ARCH="$(uname -m)"

if [ -n "${CODESIGN_IDENTITY:-}" ]; then
  echo "==> Signing with $CODESIGN_IDENTITY"
  codesign --force --deep --options runtime --timestamp \
    --entitlements packaging/entitlements.plist --sign "$CODESIGN_IDENTITY" "$APP"
fi
codesign --verify --deep --strict "$APP"

echo "==> Smoke test"
QT_QPA_PLATFORM=offscreen "$APP/Contents/MacOS/Confidence" --smoke-test

echo "==> Disk image"
DMG="dist/Confidence-$VERSION-macOS-$ARCH.dmg"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -quiet -volname "Confidence" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"

if [ -n "${CODESIGN_IDENTITY:-}" ] && [ -n "${NOTARY_PROFILE:-}" ]; then
  echo "==> Notarizing"
  codesign --sign "$CODESIGN_IDENTITY" --timestamp "$DMG"
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
fi

echo
echo "Built:"
echo "  $APP"
echo "  $DMG"
