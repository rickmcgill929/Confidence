# PyInstaller spec for Confidence.
#
#   macOS:    packaging/build_macos.sh      → dist/Confidence.app + .dmg
#   Windows:  packaging/build_windows.ps1   → dist/Confidence/ + .zip
#
# Environment (optional, macOS): CODESIGN_IDENTITY="Developer ID Application: …"
# signs with the hardened runtime; otherwise PyInstaller signs ad hoc.
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
import dsan  # noqa: E402

VERSION = dsan.__version__
try:                                   # written by packaging/stamp_build.py just before the build
    from dsan import _build as _stamp
    BUILD = _stamp.BUILD
except ImportError:
    BUILD = VERSION
print(f"Confidence {VERSION} build {BUILD}")
ICON_DIR = os.path.join(SPECPATH, "icon")
MAC = sys.platform == "darwin"
WIN = sys.platform.startswith("win")

hiddenimports = (
    ["dsan.version"] + (["dsan._build"] if BUILD != VERSION else []) +
    collect_submodules("dsan.clock")   # includes dsan.clock.decklink
    + ["dsan.osc", "dsan.sim"]
    # imported lazily at runtime, so PyInstaller can't see them:
    + collect_submodules("serial")            # serial_for_url loads socket:// etc. handlers by name
    + ["dsan.updates", "certifi"]                # update check (certifi's cacert.pem comes with its hook)
    + ["pythonosc.udp_client", "pythonosc.osc_bundle_builder", "pythonosc.osc_message_builder",
       "pythonosc.dispatcher", "pythonosc.osc_server"]
)
if MAC:
    hiddenimports += ["objc", "Foundation"]   # pyobjc: window levels over full-screen slideshows; Sparkle

excludes = [
    "aiohttp", "tkinter", "pytest", "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets",
    "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtMultimedia", "PyQt6.QtBluetooth", "PyQt6.Qt3DCore",
    "PyQt6.QtPdf", "PyQt6.QtSql", "PyQt6.QtTest",
]

# DeckLink bridge (optional): built by packaging/build_decklink.sh
_native = os.path.join(ROOT, "src", "dsan", "clock", "native")
binaries = [(os.path.join(_native, f), "dsan/clock/native")
            for f in ("libdsan_decklink.dylib", "libdsan_decklink.so") if os.path.exists(os.path.join(_native, f))]
print("DeckLink bridge:", "bundled" if binaries else "not built (SDI output will be unavailable)")

_changelog = os.path.join(ROOT, "CHANGELOG.md")
datas = [(_changelog, "dsan")] if os.path.exists(_changelog) else []

a = Analysis(
    [os.path.join(SPECPATH, "dsan_clock_entry.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)


def _keep(entry) -> bool:
    """Drop Qt pieces the clock never uses (translations, the PDF module)."""
    dest = entry[0].replace("\\", "/")
    return not ("/translations/" in dest or "Qt6Pdf" in dest or "QtPdf" in dest
                or "qpdf" in dest.rsplit("/", 1)[-1])


a.datas = [e for e in a.datas if _keep(e)]
a.binaries = [e for e in a.binaries if _keep(e)]
pyz = PYZ(a.pure)

icon = None
if MAC and os.path.exists(os.path.join(ICON_DIR, "Confidence.icns")):
    icon = os.path.join(ICON_DIR, "Confidence.icns")
elif WIN:
    icon = os.path.join(ICON_DIR, "Confidence.ico")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Confidence",
    console=False,
    icon=icon,
    argv_emulation=False,
    codesign_identity=os.environ.get("CODESIGN_IDENTITY") or None,
    entitlements_file=os.path.join(SPECPATH, "entitlements.plist") if MAC else None,
)

coll = COLLECT(exe, a.binaries, a.datas, name="Confidence")

# Sparkle in-app updates: build_macos.sh sets these when it signs with a Developer ID
SPARKLE_KEYS = {}
if MAC and os.environ.get("SPARKLE_FEED_URL") and os.environ.get("SPARKLE_PUBLIC_KEY"):
    SPARKLE_KEYS = {
        "SUFeedURL": os.environ["SPARKLE_FEED_URL"],
        "SUPublicEDKey": os.environ["SPARKLE_PUBLIC_KEY"],
        "SUEnableAutomaticChecks": True,
        "SUScheduledCheckInterval": 86400,
        "SUAllowsAutomaticUpdates": True,
    }
    print("Sparkle:", SPARKLE_KEYS["SUFeedURL"])

if MAC:
    app = BUNDLE(
        coll,
        name="Confidence.app",
        icon=icon,
        bundle_identifier="io.trucue.confidence",
        version=VERSION,
        info_plist={
            "CFBundleName": "Confidence",
            "CFBundleDisplayName": "Confidence",
            "CFBundleShortVersionString": VERSION,
            # version + build (0.6.1.20261105.1540): Sparkle compares this, so a release always beats an
            # older version's later build
            "CFBundleVersion": VERSION if BUILD == VERSION else f"{VERSION}.{BUILD}",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.utilities",
            "NSHumanReadableCopyright": "© 2026 Rick McGill / TRUCUE. MIT licensed.",
            **SPARKLE_KEYS,
        },
    )
