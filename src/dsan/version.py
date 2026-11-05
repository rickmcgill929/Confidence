"""Version and build information for dsan / Confidence.

The version lives in one place, ``dsan.__version__`` (``packaging/bump_version.py``
changes it). App builds add a build stamp: ``packaging/stamp_build.py`` writes
``dsan/_build.py`` just before PyInstaller runs, so a built app knows when and
from which commit it was built. Running from source reports "development".
"""
from __future__ import annotations

import platform
import re
import sys
from pathlib import Path
from typing import Dict, Optional

from . import __version__


def _build_module():
    try:
        from . import _build  # type: ignore
        return _build
    except ImportError:
        return None


def build() -> str:
    """Build number, e.g. '20261104.1540', or 'dev' when running from source."""
    b = _build_module()
    return getattr(b, "BUILD", "dev") if b else "dev"


def commit() -> str:
    b = _build_module()
    return getattr(b, "COMMIT", "") if b else ""


def built_at() -> str:
    b = _build_module()
    return getattr(b, "BUILT", "") if b else ""


def version_string() -> str:
    """'0.3.0 (build 20261104.1540)' or '0.3.0 (development)'."""
    b = build()
    return f"{__version__} (build {b})" if b != "dev" else f"{__version__} (development)"


def details() -> Dict[str, str]:
    """Everything worth pasting into a bug report."""
    out = {"Confidence": __version__, "Build": build()}
    if commit():
        out["Commit"] = commit()
    if built_at():
        out["Built"] = built_at()
    out["Python"] = platform.python_version()
    try:
        from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR
        out["Qt / PyQt"] = f"{QT_VERSION_STR} / {PYQT_VERSION_STR}"
    except ImportError:
        pass
    mac = platform.mac_ver()[0]
    out["System"] = f"macOS {mac} ({platform.machine()})" if mac else f"{platform.system()} {platform.release()} ({platform.machine()})"
    try:
        from .clock import decklink
        ok, text = decklink.available()
        out["DeckLink"] = text
    except Exception:
        pass
    return out


def details_text() -> str:
    return "\n".join(f"{k}: {v}" for k, v in details().items())


def _changelog_path() -> Optional[Path]:
    here = Path(__file__).resolve().parent
    for p in (here / "CHANGELOG.md", here.parent.parent / "CHANGELOG.md",
              Path(getattr(sys, "_MEIPASS", "")) / "dsan" / "CHANGELOG.md"):
        if p.is_file():
            return p
    return None


def changelog_section(version: str = __version__) -> str:
    """The CHANGELOG.md notes for one version ('' if not found)."""
    p = _changelog_path()
    if p is None:
        return ""
    text = p.read_text(encoding="utf-8")
    m = re.search(rf"^## \[?{re.escape(version)}\]?.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


__all__ = ["__version__", "build", "built_at", "changelog_section", "commit", "details", "details_text",
           "version_string"]
