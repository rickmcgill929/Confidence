#!/bin/bash
# Confidence Pi — install headless Confidence on a Raspberry Pi (or any 64-bit Debian/Ubuntu box)
# as a service that starts at power-on. Set it up from a phone: http://confidence.local:8080
#
#   curl -fsSL https://raw.githubusercontent.com/rickmcgill929/Confidence/main/packaging/pi/install.sh | sudo bash
#
# Options (after "bash -s --" when piping, e.g.  … | sudo bash -s -- --version 0.12.0):
#   --version X.Y.Z     install that release instead of the latest
#   --file PATH         install from a downloaded Confidence-…-Linux-<arch>.tar.gz (no internet needed)
#   --update            same as installing again: newest release, settings kept
#   --uninstall         remove Confidence (settings in /var/lib/confidence are kept; add --purge to remove them)
#   --no-start          install but don't start the service
#
# What it does: installs the libraries the display needs, makes a "confidence" system user (in the
# dialout/video/render groups, for USB-RS485 adapters and HDMI), unpacks the release to
# /opt/confidence/versions/<version> with /opt/confidence/current pointing at it, and adds:
#   /etc/systemd/system/confidence.service   runs: Confidence --headless, restarts it if it stops
#   /etc/default/confidence                  options (HDMI mode, remote port, PIN, name)
#   /etc/sudoers.d/confidence                lets the browser remote reboot / shut down the Pi
# Updating from the browser remote (System → Update) swaps /opt/confidence/current; the old version
# stays next to it.
set -euo pipefail

REPO="rickmcgill929/Confidence"
ROOT="${CONFIDENCE_ROOT:-}"                   # for testing: install under a scratch folder
BASE="$ROOT/opt/confidence"
HOME_DIR="$ROOT/var/lib/confidence"
UNIT="$ROOT/etc/systemd/system/confidence.service"
DEFAULTS="$ROOT/etc/default/confidence"
SUDOERS="$ROOT/etc/sudoers.d/confidence"
SVC_USER="confidence"

VERSION="" FILE="" MODE="install" START=1 PURGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="${2#v}"; shift ;;
    --file) FILE="$2"; shift ;;
    --update) MODE="install" ;;
    --uninstall) MODE="uninstall" ;;
    --purge) PURGE=1 ;;
    --no-start) START=0 ;;
    -h|--help) echo "Options: --version X.Y.Z · --file PATH · --update · --uninstall [--purge] · --no-start"; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)"; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!!  %s\033[0m\n' "$*"; }
die() { printf '\033[31mxx  %s\033[0m\n' "$*" >&2; exit 1; }
have_systemd() { [ -z "$ROOT" ] && [ -d /run/systemd/system ] && command -v systemctl >/dev/null; }

[ "$(id -u)" = 0 ] || [ -n "$ROOT" ] || die "Run it with sudo:  curl -fsSL … | sudo bash"

# ── uninstall ─────────────────────────────────────────────────────────────────
if [ "$MODE" = uninstall ]; then
  say "Removing Confidence"
  if have_systemd; then
    systemctl disable --now confidence.service 2>/dev/null || true
  fi
  rm -f "$UNIT" "$SUDOERS"
  have_systemd && systemctl daemon-reload || true
  rm -rf "$BASE"
  if [ "$PURGE" = 1 ]; then
    rm -rf "$HOME_DIR" "$DEFAULTS"
    [ -z "$ROOT" ] && id "$SVC_USER" >/dev/null 2>&1 && userdel "$SVC_USER" 2>/dev/null || true
    echo "Removed, settings too."
  else
    echo "Removed. Settings kept in $HOME_DIR and $DEFAULTS (--uninstall --purge removes them)."
  fi
  exit 0
fi

# ── which build ───────────────────────────────────────────────────────────────
case "$(uname -m)" in
  aarch64|arm64) ARCH=aarch64 ;;
  x86_64|amd64) ARCH=x86_64 ;;
  armv7l|armv6l) die "This needs the 64-bit Raspberry Pi OS (Pi 3/4/5/Zero 2). Re-flash with the 64-bit image." ;;
  *) die "No Confidence build for $(uname -m)" ;;
esac
glibc="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$' || echo 0)"
if [ -n "$glibc" ] && [ "$(printf '%s\n' 2.35 "$glibc" | sort -V | head -1)" != 2.35 ]; then
  die "This system is too old (glibc $glibc; needs 2.35+: Raspberry Pi OS Bookworm, Debian 12, Ubuntu 22.04 or newer)."
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
if [ -n "$FILE" ]; then
  [ -f "$FILE" ] || die "No such file: $FILE"
  ARCHIVE="$FILE"
  VERSION="$(basename "$FILE" | sed -nE 's/^Confidence-([0-9][0-9.]*)-Linux-.*\.tar\.gz$/\1/p')"
  [ -n "$VERSION" ] || die "Not a Confidence Linux release file: $FILE"
else
  command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl; }
  if [ -n "$VERSION" ]; then API="https://api.github.com/repos/$REPO/releases/tags/v$VERSION"
  else API="https://api.github.com/repos/$REPO/releases/latest"; fi
  say "Finding the ${VERSION:-latest} release"
  curl -fsSL "$API" -o "$WORK/release.json" || die "Couldn't reach GitHub ($API) — is the Pi online?"
  read -r VERSION URL < <(python3 - "$WORK/release.json" "$ARCH" <<'PY'
import json, sys
rel = json.load(open(sys.argv[1]))
want = f"-Linux-{sys.argv[2]}.tar.gz"
asset = next((a for a in rel.get("assets", []) if a["name"].endswith(want)), None)
print(rel["tag_name"].lstrip("v"), asset["browser_download_url"] if asset else "")
PY
)
  [ -n "$URL" ] || die "Release $VERSION has no Linux-$ARCH build yet."
  say "Downloading Confidence $VERSION ($ARCH)"
  ARCHIVE="$WORK/Confidence.tar.gz"
  curl -fL --progress-bar "$URL" -o "$ARCHIVE"
fi

# ── system pieces ─────────────────────────────────────────────────────────────
if [ -z "$ROOT" ] && command -v apt-get >/dev/null; then
  say "Libraries for the display and fonts"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    libdrm2 libgbm1 libegl1 libgl1 libfontconfig1 fonts-dejavu-core libxkbcommon0 libdbus-1-3 \
    libglib2.0-0 ca-certificates >/dev/null
fi

if [ -z "$ROOT" ]; then
  if ! id "$SVC_USER" >/dev/null 2>&1; then
    say "Service user '$SVC_USER'"
    useradd --system --home-dir "$HOME_DIR" --create-home --shell /usr/sbin/nologin "$SVC_USER"
  fi
  for g in dialout video render tty; do
    getent group "$g" >/dev/null && usermod -aG "$g" "$SVC_USER"
  done
fi
mkdir -p "$HOME_DIR" "$BASE/versions"

# ── unpack and switch ─────────────────────────────────────────────────────────
say "Installing Confidence $VERSION to $BASE"
mkdir -p "$WORK/x"
tar -xzf "$ARCHIVE" -C "$WORK/x"
PROG="$(find "$WORK/x" -path '*/Confidence/Confidence' -type f | head -1)"
[ -n "$PROG" ] || die "The download doesn't contain the Confidence program."
SRC="$(dirname "$(dirname "$PROG")")"
rm -rf "$BASE/versions/$VERSION"
mv "$SRC" "$BASE/versions/$VERSION"
PREV="$( [ -L "$BASE/current" ] && basename "$(readlink "$BASE/current")" || true)"
ln -sfn "versions/$VERSION" "$BASE/current.new"
mv -T "$BASE/current.new" "$BASE/current"
for d in "$BASE"/versions/*; do                     # keep this and the previous version
  n="$(basename "$d")"
  [ "$n" = "$VERSION" ] || [ "$n" = "$PREV" ] || rm -rf "$d"
done

# ── service ───────────────────────────────────────────────────────────────────
mkdir -p "$(dirname "$UNIT")" "$(dirname "$DEFAULTS")" "$(dirname "$SUDOERS")"
if [ ! -f "$DEFAULTS" ]; then
  HDMI=drm
  if [ -z "$ROOT" ] && have_systemd && [ "$(systemctl get-default 2>/dev/null)" = graphical.target ]; then
    HDMI=off
    warn "This Pi boots to a desktop, which keeps HDMI for itself. Confidence will run without the"
    warn "HDMI clock (remote, OSC and adapters still work). For the clock on HDMI, use Raspberry Pi OS"
    warn "Lite, or:  sudo systemctl set-default multi-user.target  and set CONFIDENCE_HDMI=drm in $DEFAULTS"
  fi
  cat > "$DEFAULTS" <<EOF
# Confidence (headless) — change, then:  sudo systemctl restart confidence
#
# HDMI clock: drm (default: straight to the display), fb0 (older /dev/fb0), off (no clock on HDMI)
CONFIDENCE_HDMI=$HDMI
#
# Browser remote and Bonjour name — these also change in the remote itself; set here to force them:
#   --remote-port 8080   --remote-pin 1234   --name stage-left   (http://stage-left.local:8080)
CONFIDENCE_ARGS=
EOF
fi

cat > "$UNIT" <<EOF
[Unit]
Description=Confidence show clock (headless)
Documentation=https://github.com/$REPO
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
Group=$SVC_USER
SupplementaryGroups=dialout video render tty
Environment=HOME=/var/lib/confidence
Environment=LANG=C.UTF-8
Environment=XDG_CONFIG_HOME=/var/lib/confidence/.config
Environment=XDG_RUNTIME_DIR=/var/lib/confidence/.run
Environment=CONFIDENCE_INSTALL_DIR=/opt/confidence
Environment=CONFIDENCE_SYSTEM_CONTROL=1
EnvironmentFile=-/etc/default/confidence
ExecStartPre=+/bin/mkdir -p /var/lib/confidence/.run
ExecStartPre=+/bin/chown $SVC_USER:$SVC_USER /var/lib/confidence/.run
ExecStart=/opt/confidence/current/Confidence/Confidence --headless \$CONFIDENCE_ARGS
Restart=always
RestartSec=2
# lets the remote use port 80 (http://confidence.local) without running as root
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF

cat > "$SUDOERS.tmp" <<EOF
# Confidence's browser remote: System → Reboot / Shut down
$SVC_USER ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff
EOF
chmod 0440 "$SUDOERS.tmp"
if command -v visudo >/dev/null && ! visudo -cf "$SUDOERS.tmp" >/dev/null; then
  rm -f "$SUDOERS.tmp"; warn "Skipped the reboot/shut-down permission (visudo rejected it)."
else
  mv "$SUDOERS.tmp" "$SUDOERS"
fi

[ -z "$ROOT" ] && chown -R "$SVC_USER:$SVC_USER" "$BASE" "$HOME_DIR"

if have_systemd; then
  systemctl daemon-reload
  systemctl enable confidence.service >/dev/null 2>&1
  if [ "$START" = 1 ]; then
    say "Starting"
    systemctl restart confidence.service
    for _ in $(seq 30); do
      line="$(journalctl -u confidence -n 30 --no-pager -o cat 2>/dev/null | grep 'Browser remote: http' | tail -2 || true)"
      [ -n "$line" ] && break
      sleep 1
    done
    echo
    if [ -n "$line" ]; then
      echo "Confidence $VERSION is running. Open it on a phone or computer on this network:"
      echo "$line" | sed 's/^Browser remote: /    /'
    else
      warn "Started, but it hasn't reported its address yet. See:  journalctl -u confidence -f"
    fi
  fi
else
  warn "No systemd here: installed, but not set to start at boot."
fi
echo
echo "Logs:        journalctl -u confidence -f"
echo "Options:     sudo nano $DEFAULTS   then   sudo systemctl restart confidence"
echo "Update:      from the browser remote (System), or run this script again"
echo "Uninstall:   curl -fsSL https://raw.githubusercontent.com/$REPO/main/packaging/pi/install.sh | sudo bash -s -- --uninstall"
