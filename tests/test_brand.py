import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_brand_svgs_render(qapp):
    from PyQt6.QtSvg import QSvgRenderer
    for name in ("confidence-icon", "confidence-mark", "confidence-logo", "confidence-logo-light"):
        svg = ROOT / "packaging" / "brand" / f"{name}.svg"
        text = svg.read_text()
        assert all(c in text for c in ("#00D06C", "#FFB000", "#FF3B3B"))   # green / yellow / red ring
        assert "<text" not in text                                        # lettering is outlines
        assert QSvgRenderer(str(svg)).isValid(), name
    assert not list((ROOT / "packaging" / "brand").glob("trutime-*"))


def test_app_icon_files():
    icon_dir = ROOT / "packaging" / "icon"
    assert (icon_dir / "Confidence.png").exists() and (icon_dir / "Confidence.ico").exists()
    assert len(list((icon_dir / "Confidence.iconset").glob("icon_*.png"))) == 10
    assert not [p for p in icon_dir.iterdir() if p.name.startswith(("DSANClock", "TruTime"))]


@pytest.mark.parametrize("old_name", ["TruTime", "DSANClock"])
def test_settings_carry_over(qapp, old_name):
    from PyQt6.QtCore import QSettings
    from dsan.clock.app import open_settings
    names = {"Confidence": "ConfTestNew", "TruTime": "ConfTestTru", "DSANClock": "ConfTestDsan"}
    factory = lambda org, app: QSettings(org, names[app])       # noqa: E731
    for n in names.values():
        QSettings("TRUCUE", n).clear()
    old = QSettings("TRUCUE", names[old_name])
    old.setValue("inputs/limitimer", '{"serial": "D30JXNEN"}')
    old.setValue("millumin/port", 5001)
    old.sync()
    s = open_settings(factory)
    assert s.value("inputs/limitimer") == '{"serial": "D30JXNEN"}' and int(s.value("millumin/port")) == 5001
    assert s.value("settings/migrated_from") == old_name
    s.setValue("millumin/port", 6000)                            # later launches don't copy again
    s.sync()
    assert int(open_settings(factory).value("millumin/port")) == 6000
    for n in names.values():
        QSettings("TRUCUE", n).clear()


def test_window_title_and_about(qapp, monkeypatch):
    from PyQt6.QtCore import QSettings
    import dsan
    import dsan.clock.app as appmod
    real = appmod.QSettings
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "ConfTestWin"))
    w = appmod.ControlWindow()
    assert w.windowTitle() == f"Confidence {dsan.__version__}"
    assert f"Confidence {dsan.__version__}" in w.about_box().text()
    w.close()
    QSettings("TRUCUE", "ConfTestWin").clear()
