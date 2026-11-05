#!/bin/bash
# Build the DeckLink bridge used by Confidence's SDI output (fill/key).
#
#   packaging/build_decklink.sh [path to "Blackmagic DeckLink SDK <version>"]
#
# Without an argument it uses $DECKLINK_SDK, or the newest
# "Blackmagic DeckLink SDK*" folder on your Desktop or in Downloads.
# Output: src/dsan/clock/native/libdsan_decklink.dylib (macOS) or .so (Linux).
# Running cards also need Blackmagic Desktop Video installed.
set -euo pipefail
cd "$(dirname "$0")/.."

SDK="${1:-${DECKLINK_SDK:-}}"
if [ -z "$SDK" ]; then
  for c in "$HOME/Downloads/Blackmagic DeckLink SDK"* "$HOME/Desktop/Blackmagic DeckLink SDK"*; do
    [ -d "$c" ] && SDK="$c"
  done
fi
if [ -z "$SDK" ] || [ ! -d "$SDK" ]; then
  echo "DeckLink SDK not found. Download it from https://www.blackmagicdesign.com/developer/"
  echo "then run:  $0 \"/path/to/Blackmagic DeckLink SDK 16.0\""
  exit 1
fi

OUT=src/dsan/clock/native
SRC="$OUT/dsan_decklink.cpp"
case "$(uname)" in
  Darwin)
    INC="$SDK/Mac/include"
    ARCHS="${ARCHS:-$(uname -m)}"            # e.g. ARCHS="arm64 x86_64" for a universal library
    arch_flags=()
    for a in $ARCHS; do arch_flags+=(-arch "$a"); done
    LIB="$OUT/libdsan_decklink.dylib"
    clang++ -std=c++17 -O2 -fvisibility=hidden -dynamiclib "${arch_flags[@]}" -mmacosx-version-min=11.0 \
      -I"$INC" "$INC/DeckLinkAPIDispatch.cpp" "$SRC" -framework CoreFoundation \
      -install_name @rpath/libdsan_decklink.dylib -o "$LIB"
    codesign --force --sign - "$LIB" 2>/dev/null || true
    ;;
  Linux)
    INC="$SDK/Linux/include"
    LIB="$OUT/libdsan_decklink.so"
    g++ -std=c++17 -O2 -fPIC -shared -fvisibility=hidden -I"$INC" "$INC/DeckLinkAPIDispatch.cpp" "$SRC" \
      -o "$LIB" -ldl -lpthread
    ;;
  *)
    echo "DeckLink output isn't supported on $(uname) yet."; exit 1 ;;
esac
echo "Built $LIB (from $(basename "$SDK"))"
