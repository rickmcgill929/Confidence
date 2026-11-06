"""In-app updates on macOS with Sparkle (sparkle-project.org), through PyObjC.

The packaged Mac app carries Sparkle.framework in Contents/Frameworks and the
Sparkle keys in its Info.plist (SUFeedURL, SUPublicEDKey) when it was built and
signed with a Developer ID — see packaging/build_macos.sh. Then Sparkle checks the
appcast, shows what's new, downloads only the difference from this version (a
delta) when it can, verifies it and installs it on relaunch.

Anywhere else — Windows, Linux, running from source, an ad-hoc-signed build —
:func:`load` returns None and the app uses the GitHub release check (dsan.updates).
"""
from __future__ import annotations

import os
import sys
from typing import Optional


class SparkleUpdater:
    """A started SPUStandardUpdaterController."""

    def __init__(self, controller):
        self._controller = controller           # keep a strong reference: Sparkle stops when it's freed

    @property
    def updater(self):
        return self._controller.updater()

    def check_for_updates(self) -> None:
        """Sparkle's own "Check for Updates…" window (progress, release notes, Install and Relaunch)."""
        self._controller.checkForUpdates_(None)

    def can_check(self) -> bool:
        return bool(self.updater.canCheckForUpdates())

    @property
    def automatic(self) -> bool:
        return bool(self.updater.automaticallyChecksForUpdates())

    @automatic.setter
    def automatic(self, on: bool) -> None:
        self.updater.setAutomaticallyChecksForUpdates_(bool(on))

    @property
    def feed_url(self) -> str:
        url = self.updater.feedURL()
        return str(url.absoluteString()) if url is not None else ""


def _framework_path(bundle) -> Optional[str]:
    path = bundle.privateFrameworksPath()
    if not path:
        return None
    fw = os.path.join(str(path), "Sparkle.framework")
    return fw if os.path.isdir(fw) else None


def load() -> Optional[SparkleUpdater]:
    """Start Sparkle if this is a Mac app bundle that ships it and is configured for it, else None.

    Call on the main thread once the QApplication exists. Never raises.
    """
    if sys.platform != "darwin" or not getattr(sys, "frozen", False) or os.environ.get("CONFIDENCE_NO_SPARKLE") \
            or os.environ.get("QT_QPA_PLATFORM") == "offscreen":     # smoke test / headless
        return None
    try:
        import objc
        from Foundation import NSBundle
    except ImportError:
        return None
    try:
        bundle = NSBundle.mainBundle()
        info = bundle.infoDictionary() or {}
        if not info.get("SUFeedURL") or not info.get("SUPublicEDKey"):
            return None                              # not built for Sparkle (no Developer ID)
        fw = _framework_path(bundle)
        if fw is None:
            return None
        objc.loadBundle("Sparkle", bundle_path=fw, module_globals={})
        cls = objc.lookUpClass("SPUStandardUpdaterController")
        controller = cls.alloc().initWithStartingUpdater_updaterDelegate_userDriverDelegate_(True, None, None)
        return SparkleUpdater(controller) if controller is not None else None
    except Exception:
        return None


__all__ = ["SparkleUpdater", "load"]
