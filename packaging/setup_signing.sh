#!/bin/bash
# One-time setup for signed, notarized, self-updating Mac releases. Run on your Mac:
#
#   packaging/setup_signing.sh
#
# It
#   1. makes Sparkle's update-signing key (kept in your login keychain) and writes its
#      public half to packaging/sparkle_public_key.txt (commit that file);
#   2. stores these GitHub secrets on the repo with `gh secret set` (nothing is printed):
#        SPARKLE_PRIVATE_KEY          signs each update so the app can trust it
#        MACOS_CERTIFICATE_P12        your "Developer ID Application" certificate, exported as .p12
#        MACOS_CERTIFICATE_PASSWORD   the password you give that .p12 when exporting
#        NOTARY_KEY_P8 / NOTARY_KEY_ID / NOTARY_ISSUER_ID   an App Store Connect API key, for notarizing
#
# Safe to run again: the Sparkle key is reused, and each step can be skipped.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v gh >/dev/null || { echo "Install the GitHub CLI first:  brew install gh   (then: gh auth login)"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Sign in to GitHub first:  gh auth login"; exit 1; }
REPO="$(python3 -c 'import sys; sys.path.insert(0, "src"); from dsan import updates; print(updates.REPO)')"
echo "Repository: $REPO"
ask() { local a; read -r -p "$1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]; }

# ── 1. Sparkle key ───────────────────────────────────────────────────────────────
SPARKLE_DIR="$(packaging/fetch_sparkle.sh)"
echo
echo "== Sparkle update-signing key"
PUB="$("$SPARKLE_DIR/bin/generate_keys" --account confidence -p 2>/dev/null || true)"
if [ -z "$PUB" ]; then
  echo "   Making a new key (stored in your login keychain as 'Private key for signing Sparkle updates')…"
  "$SPARKLE_DIR/bin/generate_keys" --account confidence >/dev/null
  PUB="$("$SPARKLE_DIR/bin/generate_keys" --account confidence -p)"
fi
printf '%s\n' "$PUB" > packaging/sparkle_public_key.txt
echo "   Public key → packaging/sparkle_public_key.txt"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
rm -f "$TMP"
"$SPARKLE_DIR/bin/generate_keys" --account confidence -x "$TMP" >/dev/null
gh secret set SPARKLE_PRIVATE_KEY --repo "$REPO" < "$TMP"
rm -f "$TMP"
echo "   SPARKLE_PRIVATE_KEY set on GitHub."
echo "   Keep a backup of this key (Keychain Access → login → 'Private key for signing Sparkle updates'):"
echo "   if it's lost, installed copies can't verify updates any more."

# ── 2. Developer ID certificate ──────────────────────────────────────────────────
echo
echo "== Developer ID Application certificate"
security find-identity -v -p codesigning | grep "Developer ID Application" || echo "   (none found in your keychain)"
if ask "Upload a .p12 export of it to GitHub now?"; then
  echo "   In Keychain Access: My Certificates → right-click 'Developer ID Application: …' → Export… → .p12,"
  echo "   choose a password. Then drag the .p12 file into this window and press Return:"
  read -r -p "   .p12 file: " P12
  P12="${P12%\"}"; P12="${P12#\"}"; P12="${P12//\\ / }"; P12="${P12% }"
  [ -f "$P12" ] || { echo "   Not found: $P12"; exit 1; }
  read -r -s -p "   Its password: " P12PW; echo
  base64 -i "$P12" | gh secret set MACOS_CERTIFICATE_P12 --repo "$REPO"
  printf '%s' "$P12PW" | gh secret set MACOS_CERTIFICATE_PASSWORD --repo "$REPO"
  echo "   MACOS_CERTIFICATE_P12 and MACOS_CERTIFICATE_PASSWORD set. You can delete the .p12 now."
fi

# ── 3. Notarization key ──────────────────────────────────────────────────────────
echo
echo "== Notarization (App Store Connect API key)"
if ask "Upload an App Store Connect API key now?"; then
  echo "   appstoreconnect.apple.com → Users and Access → Integrations → App Store Connect API →"
  echo "   Team Keys → + (access: Developer). Download the AuthKey_XXXXXXXXXX.p8 (only possible once)."
  read -r -p "   Drag the .p8 file here: " P8
  P8="${P8%\"}"; P8="${P8#\"}"; P8="${P8//\\ / }"; P8="${P8% }"
  [ -f "$P8" ] || { echo "   Not found: $P8"; exit 1; }
  KEYID="$(basename "$P8" | sed -n 's/^AuthKey_\(.*\)\.p8$/\1/p')"
  read -r -p "   Key ID [$KEYID]: " k; KEYID="${k:-$KEYID}"
  read -r -p "   Issuer ID (shown above the keys list): " ISSUER
  gh secret set NOTARY_KEY_P8 --repo "$REPO" < "$P8"
  printf '%s' "$KEYID" | gh secret set NOTARY_KEY_ID --repo "$REPO"
  printf '%s' "$ISSUER" | gh secret set NOTARY_ISSUER_ID --repo "$REPO"
  echo "   NOTARY_KEY_P8, NOTARY_KEY_ID and NOTARY_ISSUER_ID set."
  if ask "Also save it for notarizing local builds (xcrun notarytool store-credentials confidence)?"; then
    xcrun notarytool store-credentials confidence --key "$P8" --key-id "$KEYID" --issuer "$ISSUER"
    echo "   Local builds notarize with:  NOTARY_PROFILE=confidence packaging/build_macos.sh"
  fi
fi

echo
echo "Done. Commit packaging/sparkle_public_key.txt, then release as usual:"
echo "  python3 packaging/bump_version.py minor && git add -A && git commit -m \"Confidence x.y.z\""
echo "  git tag vX.Y.Z && git push && git push --tags"
echo "From that release on, the Mac app updates itself (Help → Check for Updates…)."
