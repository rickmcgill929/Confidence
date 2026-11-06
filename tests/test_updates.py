import io
import json
import os
import urllib.error

import pytest

import dsan
from dsan import updates

RELEASE = {
    "tag_name": "v0.5.0",
    "html_url": "https://github.com/rickmcgill929/Confidence/releases/tag/v0.5.0",
    "body": "### Added\n- Check for updates",
    "published_at": "2026-11-06T10:00:00Z",
    "assets": [
        {"name": "Confidence-0.5.0-macOS-arm64.dmg", "browser_download_url": "https://x/arm.dmg", "size": 30},
        {"name": "Confidence-0.5.0-macOS-x86_64.dmg", "browser_download_url": "https://x/intel.dmg", "size": 28},
        {"name": "Confidence-0.5.0-Windows.zip", "browser_download_url": "https://x/win.zip", "size": 40},
        {"name": "Confidence-0.5.0-Linux-x86_64.tar.gz", "browser_download_url": "https://x/linux.tgz", "size": 55},
    ],
}


def test_versions():
    assert updates.is_newer("v0.5.0", "0.4.0")
    assert updates.is_newer("0.4.1", "0.4.0") and updates.is_newer("1.0", "0.9.9")
    assert not updates.is_newer("v0.4.0", "0.4.0") and not updates.is_newer("0.3.9", "0.4.0")
    assert updates.is_newer("0.5.0", "0.5.0-beta") and not updates.is_newer("0.5.0-beta", "0.5.0")
    with pytest.raises(ValueError):
        updates.parse_version("latest")


@pytest.mark.parametrize("system, machine, key, url", [
    ("darwin", "arm64", "macOS-arm64", "https://x/arm.dmg"),
    ("darwin", "x86_64", "macOS-x86_64", "https://x/intel.dmg"),
    ("win32", "AMD64", "Windows", "https://x/win.zip"),
    ("linux", "x86_64", "Linux-x86_64", "https://x/linux.tgz"),
    ("linux", "aarch64", "Linux-aarch64", None),             # no Pi build in this release
])
def test_picks_the_download_for_this_computer(system, machine, key, url):
    assert updates.platform_key(system, machine) == key
    info = updates.from_release(RELEASE, current="0.4.0", key=key)
    assert info.available and info.latest == "0.5.0" and info.published == "2026-11-06"
    assert info.download_url == (url or RELEASE["html_url"])
    assert "Check for updates" in info.notes


def test_up_to_date_and_bad_tag():
    assert not updates.from_release(RELEASE, current="0.5.0").available
    with pytest.raises(updates.UpdateError):
        updates.from_release({"tag_name": "nightly"})


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_and_errors(monkeypatch):
    seen = {}

    def ok(req, timeout, context=None):
        seen["url"], seen["ua"], seen["ctx"] = req.full_url, req.get_header("User-agent"), context
        return _Resp(json.dumps(RELEASE).encode())
    monkeypatch.setattr(updates.urllib.request, "urlopen", ok)
    info = updates.check(current="0.4.0")
    assert info.available
    assert seen["url"] == "https://api.github.com/repos/rickmcgill929/Confidence/releases/latest"
    assert seen["ua"] == f"Confidence/{dsan.__version__}"                 # no personal details sent
    assert seen["ctx"] is not None and seen["ctx"].verify_mode.name == "CERT_REQUIRED"

    def http(code):
        def f(req, timeout, context=None):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
        return f
    monkeypatch.setattr(updates.urllib.request, "urlopen", http(404))
    with pytest.raises(updates.UpdateError, match="No releases"):
        updates.check()
    monkeypatch.setattr(updates.urllib.request, "urlopen", http(403))
    with pytest.raises(updates.UpdateError, match="limiting"):
        updates.check()

    def offline(req, timeout, context=None):
        raise urllib.error.URLError("nodename nor servname provided")
    monkeypatch.setattr(updates.urllib.request, "urlopen", offline)
    with pytest.raises(updates.UpdateError, match="online"):
        updates.check()

    import ssl

    def bad_cert(req, timeout, context=None):
        raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
    monkeypatch.setattr(updates.urllib.request, "urlopen", bad_cert)
    with pytest.raises(updates.UpdateError, match="secure connection"):
        updates.check()


def test_ssl_context_uses_certifi():
    certifi = pytest.importorskip("certifi")
    ctx = updates.ssl_context()
    assert ctx.verify_mode.name == "CERT_REQUIRED" and ctx.check_hostname
    assert ctx.cert_store_stats()["x509_ca"] > 50                         # loaded certifi's bundle
    assert certifi.where().endswith("cacert.pem")


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_app_update_flow(qapp, monkeypatch):
    import time
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QDialog, QMessageBox
    import dsan.clock.app as appmod
    real = appmod.QSettings
    QSettings("TRUCUE", "ConfTestUpd").clear()
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "ConfTestUpd"))
    shown, opened = [], []
    monkeypatch.setattr(QMessageBox, "information", lambda *a: shown.append(a[2]))
    monkeypatch.setattr(appmod.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    monkeypatch.setattr(QDialog, "exec", lambda self: shown.append(self) or 0)
    w = appmod.ControlWindow()
    assert w.update_action.text() == "Check for Updates…" and w.auto_update_action.isChecked()
    assert not w._auto_update_due()                                       # never in headless runs

    def pump(cond):
        end = time.monotonic() + 3
        while not cond() and time.monotonic() < end:
            qapp.processEvents()
            time.sleep(0.01)

    # newer release → dialog with the right download; the status bar says so
    monkeypatch.setattr(updates, "check", lambda: updates.from_release(RELEASE, current="0.4.0", key="Windows"))
    w.check_for_updates(manual=True)
    pump(lambda: shown)
    dlg = shown.pop()
    assert isinstance(dlg, QDialog) and "0.5.0" in w.version_label.text()
    dlg.download_button.click()
    assert opened == ["https://x/win.zip"]
    # skipping hides it from automatic checks, not from manual ones
    w.update_dialog(w.update_info).skip_button.click()
    w._on_update_result(w.update_info, None, False)
    assert not shown
    w._on_update_result(w.update_info, None, True)
    assert shown and isinstance(shown.pop(), QDialog)

    # up to date / offline: only a manual check says anything
    monkeypatch.setattr(updates, "check", lambda: updates.from_release(RELEASE, current="0.5.0"))
    w.check_for_updates(manual=True)
    pump(lambda: shown)
    assert "up to date" in shown.pop()

    def fail():
        raise updates.UpdateError("Couldn't reach GitHub")
    monkeypatch.setattr(updates, "check", fail)
    w.check_for_updates(manual=False)
    pump(lambda: not w._update_running)
    assert not shown
    w.check_for_updates(manual=True)
    pump(lambda: shown)
    assert "Couldn't reach GitHub" in shown.pop()

    w.auto_update_action.setChecked(False)
    assert real("TRUCUE", "ConfTestUpd").value("updates/auto", type=bool) is False
    w.close()
    QSettings("TRUCUE", "ConfTestUpd").clear()
