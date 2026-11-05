"""Confidence brand: app icon, mark and logo SVGs (lettering as outlines).

    python3 packaging/brand/make_brand.py          # needs fontTools + uharfbuzz + Inter

The mark is a countdown ring shaped like a "C": open on the right, running
clockwise from the bottom through the top, green → yellow → red as time runs
out — the same phase colours the app uses for timers and lights.

packaging/make_icon.py renders confidence-icon.svg into the macOS .iconset, .png and .ico.
"""
from __future__ import annotations

import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = os.environ.get("CONFIDENCE_FONT", "/usr/share/fonts/opentype/inter/Inter-SemiBold.otf")

GREEN, YELLOW, RED = "#00D06C", "#FFB000", "#FF3B3B"
TILE_TOP, TILE_BOTTOM = "#1B2230", "#0B0E14"
LIGHT, INK = "#ECEFF4", "#0B0E14"

# Ring geometry in a 140-unit icon space. Angles: 0 = 12 o'clock, clockwise.
CX, CY, R, W = 70, 70, 36, 15
SEGMENTS = [(120, 292, GREEN), (296, 340, YELLOW), (344, 420, RED)]   # gaps of 4° between colours


def _arc(cx: float, cy: float, r: float, a0: float, a1: float) -> str:
    def pt(a):
        t = math.radians(a - 90)
        return cx + r * math.cos(t), cy + r * math.sin(t)
    (x0, y0), (x1, y1) = pt(a0), pt(a1)
    large = 1 if (a1 - a0) % 360 > 180 else 0
    return f"M {x0:.3f} {y0:.3f} A {r} {r} 0 {large} 1 {x1:.3f} {y1:.3f}"


def ring(cx: float = CX, cy: float = CY) -> str:
    return "\n  ".join(f'<path d="{_arc(cx, cy, R, a0, a1)}" fill="none" stroke="{c}" stroke-width="{W}"/>'
                       for a0, a1, c in SEGMENTS)


def icon_svg() -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 140 140">\n'
            f'  <defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="{TILE_TOP}"/><stop offset="1" stop-color="{TILE_BOTTOM}"/>'
            f'</linearGradient></defs>\n'
            f'  <rect x="4" y="4" width="132" height="132" rx="30" fill="url(#bg)"/>\n'
            f'  {ring()}\n</svg>\n')


def mark_svg() -> str:
    """The ring alone, on a transparent background (works on dark and light)."""
    m = R + W / 2 + 1
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" '
            f'viewBox="{CX - m:g} {CY - m:g} {2 * m:g} {2 * m:g}">\n  {ring()}\n</svg>\n')


def text_path(text: str, cap_height: float, x0: float, baseline: float) -> tuple[str, float]:
    """SVG path data for `text` in the font (kerned by HarfBuzz), capitals `cap_height` tall."""
    import uharfbuzz as hb
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.ttLib import TTFont
    data = open(FONT, "rb").read()
    tt = TTFont(FONT)
    cap = tt["OS/2"].sCapHeight or 0.727 * tt["head"].unitsPerEm
    scale = cap_height / cap
    font = hb.Font(hb.Face(data))
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf, {"kern": True, "liga": True})
    glyphs, order = tt.getGlyphSet(), tt.getGlyphOrder()
    pen = SVGPathPen(glyphs)
    x = 0
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        t = TransformPen(pen, (scale, 0, 0, -scale, x0 + (x + pos.x_offset) * scale, baseline - pos.y_offset * scale))
        glyphs[order[info.codepoint]].draw(t)
        x += pos.x_advance
    return pen.getCommands(), x * scale


def logo_svg(on_light: bool) -> str:
    """Ring + 'Confidence'. Capitals are 0.9 × the ring's outer height... kept optically centred."""
    outer = R + W / 2                          # 43.5
    top, bottom = CY - outer, CY + outer       # 26.5 … 113.5
    cap = 52
    baseline = CY + cap / 2
    gap = 18
    d, w = text_path("Confidence", cap, CX + outer + gap, baseline)
    left, right = CX - outer, CX + outer + gap + w
    pad = 8
    vb = f"{left - pad:g} {top - pad:g} {right - left + 2 * pad:.1f} {bottom - top + 2 * pad:g}"
    scale = 3
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{(right - left + 2 * pad) * scale:.0f}" '
            f'height="{(bottom - top + 2 * pad) * scale:.0f}" viewBox="{vb}">\n'
            f'  <title>Confidence</title>\n  {ring()}\n'
            f'  <path d="{d}" fill="{INK if on_light else LIGHT}"/>\n</svg>\n')


def main() -> int:
    out = {
        "confidence-icon.svg": icon_svg(),
        "confidence-mark.svg": mark_svg(),
        "confidence-logo.svg": logo_svg(False),
        "confidence-logo-light.svg": logo_svg(True),
    }
    for name, svg in out.items():
        with open(os.path.join(HERE, name), "w", encoding="utf-8") as f:
            f.write(svg)
        print("wrote", os.path.join("packaging/brand", name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
