#!/bin/bash
# Build Confidence for Linux (x86_64 or arm64, whichever this machine is):
#
#   packaging/build_linux.sh
#
# Produces dist/Confidence/ (run dist/Confidence/Confidence) and
# dist/Confidence-<version>-Linux-<arch>.tar.gz, which also holds an icon, a
# desktop entry and install.sh (adds Confidence to the applications menu).
#
# Build on the oldest distribution you want to support: the app runs on that
# glibc and newer (the GitHub workflow uses Ubuntu 22.04).
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
VENV=.build-venv-linux

echo "==> Build environment ($("$PY" --version))"
[ -d "$VENV" ] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
PINS=()
if [ "$(uname -m)" = aarch64 ]; then
  # Raspberry Pi build. Newer PyQt6 only ships ARM builds for glibc 2.39+, which Raspberry Pi OS
  # Bookworm (2.36) and this build's Ubuntu 22.04 (2.35) don't have; 6.7 is the newest that runs.
  PINS=("PyQt6==6.7.1" "PyQt6-Qt6==6.7.3")
fi
"$VENV/bin/python" -m pip install --quiet --only-binary PyQt6,PyQt6-Qt6 ".[clock]" "pyinstaller>=6.16" "${PINS[@]}"

echo "==> DeckLink bridge"
packaging/build_decklink.sh || echo "   (skipped — the app will build without SDI output)"

echo "==> Version"
BUILD="$("$VENV/bin/python" packaging/stamp_build.py)"
trap '"$VENV/bin/python" packaging/stamp_build.py --clean' EXIT
VERSION="$("$VENV/bin/python" -c 'import sys; sys.path.insert(0, "src"); import dsan; print(dsan.__version__)')"
echo "   $VERSION build $BUILD"

echo "==> PyInstaller"
rm -rf build "dist/Confidence"
"$VENV/bin/pyinstaller" --noconfirm --clean --log-level WARN packaging/dsan_clock.spec

echo "==> Smoke test"
QT_QPA_PLATFORM=offscreen dist/Confidence/Confidence --smoke-test

echo "==> Package"
ARCH="$(uname -m)"
NAME="Confidence-$VERSION-Linux-$ARCH"
STAGE="$(mktemp -d)"
mkdir -p "$STAGE/$NAME"
cp -R dist/Confidence "$STAGE/$NAME/Confidence"
cp packaging/icon/Confidence.png "$STAGE/$NAME/confidence.png"
cat > "$STAGE/$NAME/confidence.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Confidence
Comment=Show clock for DSAN Limitimer, Perfect Cue and Millumin
Exec=@DIR@/Confidence/Confidence
Icon=@DIR@/confidence.png
Terminal=false
Categories=AudioVideo;Video;Utility;
EOF
cat > "$STAGE/$NAME/install.sh" <<'EOF'
#!/bin/bash
# Install Confidence for this user: ~/.local/opt/Confidence + an applications-menu entry.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
dest="$HOME/.local/opt/Confidence"
mkdir -p "$dest" "$HOME/.local/share/applications"
rm -rf "$dest/Confidence"
cp -R "$here/Confidence" "$here/confidence.png" "$dest/"
sed "s|@DIR@|$dest|g" "$here/confidence.desktop" > "$HOME/.local/share/applications/confidence.desktop"
echo "Installed to $dest — Confidence is in your applications menu."
if command -v ldconfig >/dev/null && ! ldconfig -p | grep -q libxcb-cursor.so.0 \
   && [ ! -e "$dest/Confidence/_internal/libxcb-cursor.so.0" ]; then
  echo
  echo "If Confidence doesn't open, install Qt's cursor library:  sudo apt install libxcb-cursor0"
fi
if ! id -nG | grep -qw dialout; then
  echo
  echo "To use USB-RS485 adapters, add yourself to the dialout group, then log out and back in:"
  echo "  sudo usermod -aG dialout ${USER:-$(id -un)}"
fi
EOF
chmod +x "$STAGE/$NAME/install.sh"
TAR="dist/$NAME.tar.gz"
tar -C "$STAGE" -czf "$TAR" "$NAME"
rm -rf "$STAGE"

echo
echo "Built:"
echo "  dist/Confidence/Confidence"
echo "  $TAR"
