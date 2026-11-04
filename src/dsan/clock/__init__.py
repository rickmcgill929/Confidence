"""DSAN Clock — an on-screen video clock for a Limitimer and a Perfect Cue.

Run ``dsan-clock`` (``pip install 'dsan[clock]'``). The pieces are usable on their own:

* :mod:`dsan.clock.layout` — layouts, elements, presets, JSON (no Qt)
* :mod:`dsan.clock.ports`  — remembering USB adapters and detecting which is which (no Qt)
* :mod:`dsan.clock.render` — the QPainter renderer
* :mod:`dsan.clock.output` — the output window (window / full screen / overlay)
* :mod:`dsan.clock.app`    — the operator window
"""
