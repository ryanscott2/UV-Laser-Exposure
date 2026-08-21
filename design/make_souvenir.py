"""Souvenir text as an exposable GDS -- each LETTER is its own pflm array.

No pipeline change needed: every letter is a glyph on the pinfin layer (3/0) inside its own
per-letter bbox (4/0), so pflm.build_set detects each letter as an array and the stage steps to
each one, centered in the field -> the letters land at their true positions and spell the text.

Three lines, centered on the wafer, sized so the longest line (17 chars) nearly spans it:

    Ryan Scott
    Stanford NanoHeat
    Summer 2026

Build:  python -m pflm.cli build design/souvenir_ryan.gds --set output/DXFs/souvenir --no-align
"""
from __future__ import annotations
import math
import klayout.db as pya

LINES = ["Ryan Scott", "Stanford NanoHeat", "Summer 2026"]
GLYPH_H_UM = 6000.0          # letter height
CELL_GAP_UM = 900.0          # horizontal gap budget between adjacent letter cells
BBOX_MARGIN_UM = 250.0       # bbox is this much bigger than the glyph (so it never clips)
LINE_PITCH_UM = 10500.0      # top/bottom line centers at +/- this (<= ~21 mm reachable in Y)
WAFER_R_UM = 50000.0
PIN_LAYER, BBOX_LAYER, FRAME_LAYER = (3, 0), (4, 0), (1, 0)
OUT = "design/souvenir_ryan.gds"


def _um(ly, v):
    return int(round(v / ly.dbu))


def build():
    ly = pya.Layout(); ly.dbu = 0.001
    top = ly.create_cell("SOUVENIR")
    Lp, Lb, Lf = ly.layer(*PIN_LAYER), ly.layer(*BBOX_LAYER), ly.layer(*FRAME_LAYER)
    tg = pya.TextGenerator.default_generator()

    # Scale the font to GLYPH_H_UM and monospace it on the widest glyph.
    chars = {c for line in LINES for c in line if c != " "}
    h1 = max(tg.text(c, ly.dbu, 1.0).bbox().height() * ly.dbu for c in chars)
    w1 = max(tg.text(c, ly.dbu, 1.0).bbox().width() * ly.dbu for c in chars)
    mag = GLYPH_H_UM / h1
    glyph_w = w1 * mag
    advance = glyph_w + CELL_GAP_UM
    half_w = glyph_w / 2.0 + BBOX_MARGIN_UM
    half_h = GLYPH_H_UM / 2.0 + BBOX_MARGIN_UM

    # Wafer outline (reference only; not exposed).
    top.shapes(Lf).insert(pya.Polygon(
        [pya.Point(_um(ly, WAFER_R_UM * math.cos(2 * math.pi * i / 512)),
                   _um(ly, WAFER_R_UM * math.sin(2 * math.pi * i / 512)))
         for i in range(512)]))

    n_letters = 0
    extents = []
    for li, line in enumerate(LINES):
        y = (1 - li) * LINE_PITCH_UM         # li 0 -> +pitch (top), 1 -> 0, 2 -> -pitch (bottom)
        n = len(line)
        x0 = -(n * advance) / 2.0 + advance / 2.0
        for ci, ch in enumerate(line):
            if ch == " ":
                continue
            cx = x0 + ci * advance
            g = tg.text(ch, ly.dbu, mag)
            gb = g.bbox()
            gcx = (gb.left + gb.right) / 2.0 * ly.dbu
            gcy = (gb.bottom + gb.top) / 2.0 * ly.dbu
            g.transform(pya.Trans(_um(ly, cx - gcx), _um(ly, y - gcy)))  # center glyph on the cell
            top.shapes(Lp).insert(g)
            top.shapes(Lb).insert(pya.Box(_um(ly, cx - half_w), _um(ly, y - half_h),
                                          _um(ly, cx + half_w), _um(ly, y + half_h)))
            n_letters += 1
            extents.append((cx - half_w, y - half_h, cx + half_w, y + half_h))

    ly.write(OUT)
    xs = [e[0] for e in extents] + [e[2] for e in extents]
    ys = [e[1] for e in extents] + [e[3] for e in extents]
    corner_r = max((x * x + yy * yy) ** 0.5 for x in (min(xs), max(xs)) for yy in (min(ys), max(ys)))
    print("wrote %s" % OUT)
    print("  %d letters, glyph %.2f x %.2f mm, advance %.2f mm, line pitch %.1f mm"
          % (n_letters, glyph_w / 1000, GLYPH_H_UM / 1000, advance / 1000, LINE_PITCH_UM / 1000))
    print("  text extent X[%.1f,%.1f] Y[%.1f,%.1f] mm; farthest corner r=%.1f mm (wafer r=50)"
          % (min(xs) / 1000, max(xs) / 1000, min(ys) / 1000, max(ys) / 1000, corner_r / 1000))


if __name__ == "__main__":
    build()
