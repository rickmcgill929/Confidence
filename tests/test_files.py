import json
import os

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_save_layout_from_read_only_cwd(qapp, tmp_path, monkeypatch):
    """Launched from Finder the cwd is '/'; a bare name must not be written there."""
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QFileDialog
    import dsan.clock.app as appmod
    from dsan.clock import files

    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o555)
    monkeypatch.chdir(ro)

    real = QSettings
    QSettings("TRUCUE", "DSANClockFiles").clear()
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "DSANClockFiles"))
    monkeypatch.setattr(files, "_settings", lambda: real("TRUCUE", "DSANClockFiles"))
    seen = {}

    def fake_save(parent, title, start, filt):
        seen["start"] = start
        return "Rick Look", filt                    # bare name, no extension, like the bug report
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(fake_save))

    w = appmod.ControlWindow()
    w.layout_obj.name = "Rick: Look"
    w._save_layout_as()
    target = home / "Documents" / "Confidence" / "Rick Look.json"
    assert seen["start"] == str(home / "Documents" / "Confidence" / "Rick- Look.json")
    assert target.exists() and json.loads(target.read_text())
    assert w.layout_path == str(target)

    # a relative path left over in settings from before the fix is repaired too
    w.layout_path = "Old.json"
    w._save_layout()
    assert (home / "Documents" / "Confidence" / "Old.json").exists()

    # the next dialog opens in the folder last used
    other = tmp_path / "shows"
    other.mkdir()
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda p, t, s, f: (str(other / "A.json"), f)))
    w._save_layout_as()
    assert files.last_dir("layouts") == str(other)
    w.close()
    os.chmod(ro, 0o755)
    QSettings("TRUCUE", "DSANClockFiles").clear()


def test_combo_popup_fits_longest_item(qapp, monkeypatch):
    from PyQt6.QtCore import QSettings
    import dsan.clock.app as appmod
    from dsan.clock.widgets import Combo
    real = QSettings
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "DSANClockCombo"))
    w = appmod.ControlWindow()
    w.show()
    c = w.preset_combo
    assert isinstance(c, Combo)
    c.setFixedWidth(120)                                   # as narrow as in the bug report
    c.showPopup()
    fm = c.view().fontMetrics()
    longest = max(fm.horizontalAdvance(c.itemText(i)) for i in range(c.count()))
    assert c.view().width() >= longest + 20
    c.hidePopup()
    c.addItem("A much longer preset name added later on")   # re-measured on each open
    c.showPopup()
    assert c.view().width() >= fm.horizontalAdvance("A much longer preset name added later on") + 20
    c.hidePopup()
    w.close()
    QSettings("TRUCUE", "DSANClockCombo").clear()
