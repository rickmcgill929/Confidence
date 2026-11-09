"""Render the Confidence app icon (packaging/brand/confidence-icon.svg) into a macOS .iconset,
a .png and a Windows .ico.

    QT_QPA_PLATFORM=offscreen python packaging/make_icon.py

macOS's iconutil turns the .iconset into Confidence.icns (build_macos.sh does this).

The SVG's tile spans x 4-136 of its 140-unit box. For macOS the tile is placed on
Apple's icon grid (824 of 1024 px, centered) so it sits right in the Dock.
"""
import os
import sys

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QGuiApplication, QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer

PACKAGING = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.join(PACKAGING, "icon")
SVG = os.path.join(PACKAGING, "brand", "confidence-icon.svg")
NAME = "Confidence"


def draw(size: int, renderer: QSvgRenderer) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 1024
    unit = 824 * s / 132                      # 132 SVG units of tile → 824 px at 1024
    origin = 100 * s - 4 * unit               # the SVG's tile starts at x = 4
    renderer.render(p, QRectF(origin, origin, 140 * unit, 140 * unit))
    p.end()
    return img


def main():
    QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    r = QSvgRenderer(SVG)
    if not r.isValid():
        raise SystemExit(f"can't read {SVG}")
    iconset = os.path.join(HERE, f"{NAME}.iconset")
    os.makedirs(iconset, exist_ok=True)
    for pt in (16, 32, 128, 256, 512):
        draw(pt, r).save(os.path.join(iconset, f"icon_{pt}x{pt}.png"))
        draw(pt * 2, r).save(os.path.join(iconset, f"icon_{pt}x{pt}@2x.png"))
    big = os.path.join(HERE, f"{NAME}.png")
    draw(1024, r).save(big)
    try:
        from PIL import Image
        Image.open(big).save(os.path.join(HERE, f"{NAME}.ico"),
                             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    except ImportError:
        print("Pillow not installed; skipped .ico")
    print("wrote", iconset, big)


if __name__ == "__main__":
    main()
