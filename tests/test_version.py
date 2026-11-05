import importlib.util
import os
import re
import sys
import types
from pathlib import Path

import pytest

import dsan
from dsan import version as ver

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_single_source_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", dsan.__version__)
    assert 'dynamic = ["version"]' in (ROOT / "pyproject.toml").read_text()


def test_development_and_built_strings(monkeypatch):
    monkeypatch.delitem(sys.modules, "dsan._build", raising=False)
    monkeypatch.setattr(ver, "_build_module", lambda: None)
    assert ver.build() == "dev"
    assert ver.version_string() == f"{dsan.__version__} (development)"
    stamp = types.SimpleNamespace(BUILD="20261104.1540", COMMIT="abc1234", BUILT="2026-11-04 15:40")
    monkeypatch.setattr(ver, "_build_module", lambda: stamp)
    assert ver.version_string() == f"{dsan.__version__} (build 20261104.1540)"
    d = ver.details()
    assert d["Build"] == "20261104.1540" and d["Commit"] == "abc1234" and "Python" in d
    assert "Build: 20261104.1540" in ver.details_text()


def test_changelog_sections():
    assert "DeckLink SDI output" in ver.changelog_section("0.3.0")
    assert "DSAN Clock" in ver.changelog_section("0.2.0")
    assert ver.changelog_section("9.9.9") == ""
    assert f"## [{dsan.__version__}]" in (ROOT / "CHANGELOG.md").read_text()   # current version documented


def test_stamp_build(tmp_path, monkeypatch):
    stamp = _load("stamp_build")
    target = tmp_path / "_build.py"
    monkeypatch.setattr(stamp, "TARGET", target)
    monkeypatch.setattr(sys, "argv", ["stamp_build.py"])
    assert stamp.main() == 0
    text = target.read_text()
    assert re.search(r'BUILD = "\d{8}\.\d{4}"', text) and "COMMIT =" in text
    monkeypatch.setattr(sys, "argv", ["stamp_build.py", "--clean"])
    stamp.main()
    assert not target.exists()


@pytest.mark.parametrize("part, new", [("patch", "0.3.1"), ("minor", "0.4.0"), ("major", "1.0.0"),
                                       ("2.5.7", "2.5.7")])
def test_bump_version(tmp_path, monkeypatch, part, new):
    bump = _load("bump_version")
    init = tmp_path / "__init__.py"
    init.write_text('"""doc"""\n__version__ = "0.3.0"\n')
    log = tmp_path / "CHANGELOG.md"
    log.write_text("# Changelog\n\n## [Unreleased]\n- something\n\n## [0.3.0] — 2026-11-04\n- old\n")
    monkeypatch.setattr(bump, "INIT", init)
    monkeypatch.setattr(bump, "CHANGELOG", log)
    monkeypatch.setattr(sys, "argv", ["bump_version.py", part])
    bump.main()
    assert f'__version__ = "{new}"' in init.read_text()
    text = log.read_text()
    assert text.index("## [Unreleased]") < text.index(f"## [{new}]") < text.index("## [0.3.0]")
    assert text.index("- something") > text.index(f"## [{new}]")      # unreleased notes moved under it


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_about_and_whats_new(qapp, monkeypatch, tmp_path):
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QTextBrowser
    import dsan.clock.app as appmod
    real = appmod.QSettings
    QSettings("TRUCUE", "DSANClockVer").clear()
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "DSANClockVer"))
    w = appmod.ControlWindow()
    assert w.version_label.text().startswith(f"v{dsan.__version__}")
    box = w.about_box()
    assert f"Confidence {dsan.__version__}" in box.text()
    assert "Python" in box.informativeText()
    dlg = w.whats_new_dialog()
    text = dlg.findChild(QTextBrowser).toPlainText()
    assert dsan.__version__ in text and "DeckLink" in text
    w.show_debug()
    w.debug_window.save_log(str(tmp_path / "log.txt"))
    assert (tmp_path / "log.txt").read_text().startswith(f"# Confidence {dsan.__version__}")
    w.close()
    QSettings("TRUCUE", "DSANClockVer").clear()
