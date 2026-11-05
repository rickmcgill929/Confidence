"""Entry point for the packaged Confidence app.

``--smoke-test`` starts the app off-screen, runs the demo through the real
decoder, checks state arrives, and exits 0/1. The build scripts run it
against the built app before anything ships.
"""
import sys


def smoke_test() -> int:
    import time

    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])
    import dsan.clock.app as appmod

    real = appmod.QSettings
    appmod.QSettings = lambda *_a: real("TRUCUE", "DSANClockSmokeTest")
    QSettings("TRUCUE", "DSANClockSmokeTest").clear()
    w = appmod.ControlWindow()
    w.demo_btn.click()
    end = time.monotonic() + 5
    while w.lt_state is None and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)
    ok = w.lt_state is not None and w.osc_enable.isEnabled()
    import serial.tools.list_ports  # noqa: F401  (bundled?)
    import serial.urlhandler.protocol_socket  # noqa: F401  (socket:// adapters bundled?)
    w.close()
    QSettings("TRUCUE", "DSANClockSmokeTest").clear()
    import dsan
    from dsan.clock import decklink
    dl_ok, dl_text = decklink.available()
    print(f"DeckLink: {dl_text}")
    from dsan import version as ver
    print(f"Confidence {ver.version_string()} smoke test: {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


def main() -> int:
    if "--smoke-test" in sys.argv:
        return smoke_test()
    from dsan.clock.app import main as run
    return run()


if __name__ == "__main__":
    sys.exit(main())
