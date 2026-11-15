"""The remote's System section: version and the update check."""
from dsan.clock.system import SystemControl


def release(tag, assets=("Confidence-9.9.9-macOS-arm64.dmg",)):
    return {"tag_name": tag, "html_url": f"https://example/{tag}", "body": "",
            "assets": [{"name": n, "browser_download_url": f"https://example/{n}", "size": 1} for n in assets]}


def test_check_finds_a_newer_release():
    s = SystemControl(fetch=lambda: release("v99.0.0"), platform_key="macOS-arm64")
    assert s.state()["latest"] is None
    s.check()
    s._thread.join(5)
    st = s.state()
    assert st["phase"] == "idle" and st["available"] and st["latest"] == "99.0.0"
    assert st["page"] == "https://example/v99.0.0"


def test_check_reports_errors():
    def boom():
        raise OSError("offline")
    s = SystemControl(fetch=boom)
    s.check()
    s._thread.join(5)
    assert s.state()["phase"] == "error" and "offline" in s.state()["error"]
