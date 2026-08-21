"""Souvenir text as an exposable GDS -- each LETTER is its own pflm array.

No pipeline change needed: every letter is a glyph on the pinfin layer (3/0) inside its own
per-letter bbox (4/0), so pflm.build_set detects each letter as an array and the stage steps to
each one, centered in the field -> the letters land at their true positions and spell the text.

Three lines, centered on the wafer, with per-line sizing (LINE_SCALE): the name is emphasized,
the rest smaller (x1.5 line 1, x0.75 lines 2-3). Two souvenirs are generated (see SOUVENIRS).

Build:  python -m pflm.cli build design/souvenir_ryan.gds --set souvenir_ryan
        python -m pflm.cli build design/souvenir_v2.gds   --set souvenir_v2
"""
from __future__ import annotations
import math
import klayout.db as pya

# Each souvenir: three lines + output GDS. Same text scales for all (LINE_SCALE below).
SOUVENIRS = [
    {"lines": ["Ryan Scott", "Stanford NanoHeat", "Summer 2026"], "out": "design/souvenir_ryan.gds"},
    {"lines": ["Jess+Ryan", "California Trip", "Summer 2026"],    "out": "design/souvenir_v2.gds"},
]

LINE_SCALE = [1.5, 0.75, 0.75]   # line 1 (name) 50% bigger; lines 2-3 25% smaller
BASE_GLYPH_H_UM = 6000.0         # 1.0x letter height
CELL_GAP_FRAC = 0.15             # inter-letter gap, as a fraction of glyph width
BBOX_HALF_W_FRAC = 0.54          # letter-cell bbox half-width, x glyph width (< advance/2 -> no overlap)
BBOX_HALF_H_FRAC = 0.60          # letter-cell bbox half-height, x glyph height
LINE_GAP_UM = 3500.0             # vertical gap between line edges
WAFER_R_UM = 50000.0
PIN_LAYER, BBOX_LAYER, FRAME_LAYER, ALIGN_LAYER = (3, 0), (4, 0), (1, 0), (5, 0)

# Alignment fiducials ripped verbatim from 081826_UVPFLMv2.gds (layer 5/0): four crosses
# (H-bar + V-bar) at r~45 mm, outside the text, so a later metalization step can register to
# them. pflm.build_set auto-detects 5/0 and etches each mark last (15-pass crosshatch).
ALIGN_CENTERS_UM = [(-44985.0, 0.0), (45015.0, 0.0), (15.0, -45000.0), (15.0, 45000.0)]
ALIGN_ARM_UM = 1250.0            # cross arm length (full)
ALIGN_W_UM = 50.0                # cross arm width


def _um(ly, v):
    return int(round(v / ly.dbu))


def build(lines, out):
    ly = pya.Layout(); ly.dbu = 0.001
    top = ly.create_cell("SOUVENIR")
    Lp, Lb, Lf = ly.layer(*PIN_LAYER), ly.layer(*BBOX_LAYER), ly.layer(*FRAME_LAYER)
    La = ly.layer(*ALIGN_LAYER)
    tg = pya.TextGenerator.default_generator()

    chars = {c for line in lines for c in line if c != " "}
    h1 = max(tg.text(c, ly.dbu, 1.0).bbox().height() * ly.dbu for c in chars)
    w1 = max(tg.text(c, ly.dbu, 1.0).bbox().width() * ly.dbu for c in chars)

    # Vertical stack, centered as a block on the wafer origin.
    heights = [BASE_GLYPH_H_UM * s for s in LINE_SCALE]
    cursor = (sum(heights) + LINE_GAP_UM * (len(lines) - 1)) / 2.0
    line_y = []
    for h in heights:
        line_y.append(cursor - h / 2.0)
        cursor -= (h + LINE_GAP_UM)

    # Wafer outline (reference only; not exposed).
    top.shapes(Lf).insert(pya.Polygon(
        [pya.Point(_um(ly, WAFER_R_UM * math.cos(2 * math.pi * i / 512)),
                   _um(ly, WAFER_R_UM * math.sin(2 * math.pi * i / 512)))
         for i in range(512)]))

    # Alignment fiducials (layer 5/0): four crosses, each an H-bar + a V-bar.
    for mx, my in ALIGN_CENTERS_UM:
        top.shapes(La).insert(pya.Box(_um(ly, mx - ALIGN_ARM_UM / 2), _um(ly, my - ALIGN_W_UM / 2),
                                      _um(ly, mx + ALIGN_ARM_UM / 2), _um(ly, my + ALIGN_W_UM / 2)))
        top.shapes(La).insert(pya.Box(_um(ly, mx - ALIGN_W_UM / 2), _um(ly, my - ALIGN_ARM_UM / 2),
                                      _um(ly, mx + ALIGN_W_UM / 2), _um(ly, my + ALIGN_ARM_UM / 2)))

    n_letters = 0
    xs, ys = [], []
    summary = []
    for li, line in enumerate(lines):
        gh = heights[li]
        mag = gh / h1
        gw = w1 * mag
        advance = gw * (1.0 + CELL_GAP_FRAC)
        half_w, half_h = gw * BBOX_HALF_W_FRAC, gh * BBOX_HALF_H_FRAC
        y = line_y[li]
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
            xs += [cx - half_w, cx + half_w]; ys += [y - half_h, y + half_h]
        summary.append("  '%s': x%.2f -> glyph %.2f x %.2f mm, line width %.1f mm, y=%+.1f mm"
                       % (line, LINE_SCALE[li], gw / 1000, gh / 1000, n * advance / 1000, y / 1000))

    ly.write(out)
    corner_r = max((x * x + yy * yy) ** 0.5 for x in (min(xs), max(xs)) for yy in (min(ys), max(ys)))
    print("wrote %s  (%d letters + %d align crosses on 5/0)" % (out, n_letters, len(ALIGN_CENTERS_UM)))
    print("\n".join(summary))
    print("  text extent X[%.1f,%.1f] Y[%.1f,%.1f] mm; farthest corner r=%.1f mm (wafer r=50)\n"
          % (min(xs) / 1000, max(xs) / 1000, min(ys) / 1000, max(ys) / 1000, corner_r / 1000))


if __name__ == "__main__":
    for s in SOUVENIRS:
        build(s["lines"], s["out"])
