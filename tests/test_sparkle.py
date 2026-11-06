"""Sparkle runs only in the signed Mac app; these tests stand in fake PyObjC / Sparkle objects."""
import os
import sys
import types
from pathlib import Path

import pytest

from dsan.clock import sparkle

ROOT = Path(__file__).resolve().parents[1]


class FakeUpdater:
    def __init__(self):
        self.auto, self.checks = True, 0

    def automaticallyChecksForUpdates(self):
        return self.auto

    def setAutomaticallyChecksForUpdates_(self, on):
        self.auto = on

    def canCheckForUpdates(self):
        return True

    def feedURL(self):
        return types.SimpleNamespace(absoluteString=lambda: "https://example/appcast-arm64.xml")


class FakeController:
    def __init__(self):
        self._u = FakeUpdater()
        self.shown = 0

    def initWithStartingUpdater_updaterDelegate_userDriverDelegate_(self, start, d1, d2):
        assert start is True and d1 is None and d2 is None
        return self

    def updater(self):
        return self._u

    def checkForUpdates_(self, sender):
        self.shown += 1


def install_fakes(monkeypatch, tmp_path, info):
    fw = tmp_path / "Frameworks" / "Sparkle.framework"
    fw.mkdir(parents=True)
    loaded = []
    objc = types.ModuleType("objc")
    objc.loadBundle = lambda name, bundle_path, module_globals: loaded.append((name, bundle_path))
    controller = FakeController()
    objc.lookUpClass = lambda name: types.SimpleNamespace(alloc=lambda: controller) if name == \
        "SPUStandardUpdaterController" else None
    foundation = types.ModuleType("Foundation")
    bundle = types.SimpleNamespace(infoDictionary=lambda: info,
                                   privateFrameworksPath=lambda: str(tmp_path / "Frameworks"))
    foundation.NSBundle = types.SimpleNamespace(mainBundle=lambda: bundle)
    monkeypatch.setitem(sys.modules, "objc", objc)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    return loaded, controller


def test_loads_sparkle_in_a_signed_mac_app(monkeypatch, tmp_path):
    loaded, controller = install_fakes(monkeypatch, tmp_path, {"SUFeedURL": "https://x", "SUPublicEDKey": "abc"})
    up = sparkle.load()
    assert up is not None and loaded == [("Sparkle", str(tmp_path / "Frameworks" / "Sparkle.framework"))]
    up.check_for_updates()
    assert controller.shown == 1 and up.can_check()
    up.automatic = False
    assert up.automatic is False and controller.updater().auto is False
    assert up.feed_url.endswith("appcast-arm64.xml")


@pytest.mark.parametrize("info", [{}, {"SUFeedURL": "https://x"}, {"SUPublicEDKey": "abc"}])
def test_not_configured_means_no_sparkle(monkeypatch, tmp_path, info):
    install_fakes(monkeypatch, tmp_path, info)              # ad hoc build: no keys in Info.plist
    assert sparkle.load() is None


def test_never_outside_the_mac_app(monkeypatch, tmp_path):
    install_fakes(monkeypatch, tmp_path, {"SUFeedURL": "https://x", "SUPublicEDKey": "abc"})
    monkeypatch.setattr(sys, "frozen", False)
    assert sparkle.load() is None                            # running from source
    monkeypatch.setattr(sys, "frozen", True)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    assert sparkle.load() is None                            # smoke test / headless
    monkeypatch.delenv("QT_QPA_PLATFORM")
    monkeypatch.setattr(sys, "platform", "win32")
    assert sparkle.load() is None


def test_broken_sparkle_never_raises(monkeypatch, tmp_path):
    install_fakes(monkeypatch, tmp_path, {"SUFeedURL": "https://x", "SUPublicEDKey": "abc"})

    def boom(*a, **k):
        raise RuntimeError("dlopen failed")
    monkeypatch.setattr(sys.modules["objc"], "loadBundle", boom)
    assert sparkle.load() is None


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_app_uses_sparkle_when_present(qapp, monkeypatch):
    from PyQt6.QtCore import QSettings
    import dsan.clock.app as appmod
    real = appmod.QSettings
    QSettings("TRUCUE", "ConfTestSparkle").clear()
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "ConfTestSparkle"))
    fake = sparkle.SparkleUpdater(FakeController())
    monkeypatch.setattr(sparkle, "load", lambda: fake)
    from dsan import updates
    monkeypatch.setattr(updates, "check", lambda: pytest.fail("GitHub check used instead of Sparkle"))
    w = appmod.ControlWindow()
    assert w.sparkle is fake and w.auto_update_action.isChecked()
    w.check_for_updates(manual=True)
    assert fake._controller.shown == 1
    w.check_for_updates(manual=False)                         # Sparkle schedules its own background checks
    assert fake._controller.shown == 1 and not w._update_running
    w.auto_update_action.setChecked(False)
    assert fake.automatic is False
    w.close()
    QSettings("TRUCUE", "ConfTestSparkle").clear()


def test_build_files():
    spec = (ROOT / "packaging" / "dsan_clock.spec").read_text()
    for key in ("SUFeedURL", "SUPublicEDKey", "SUEnableAutomaticChecks", 'f"{VERSION}.{BUILD}"'):
        assert key in spec
    mac = (ROOT / "packaging" / "build_macos.sh").read_text()
    assert "appcast-$ARCH.xml" in mac and "Sparkle.framework" in mac and "--options runtime" in mac
    assert "XPCServices" in mac and "stapler staple" in mac
    import yaml
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "build-clock.yml").read_text())
    assert wf["jobs"]["appcast"]["needs"] == ["macos"]
    assert "appcast" in wf["jobs"]["release"]["needs"]
    appcast = (ROOT / "packaging" / "make_appcast.sh").read_text()
    for flag in ("--ed-key-file -", "--download-url-prefix", "--maximum-deltas", "--embed-release-notes"):
        assert flag in appcast
