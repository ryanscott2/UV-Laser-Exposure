"""Souvenir text as an exposable GDS -- each LETTER is its own pflm array.

No pipeline change needed: every letter is a glyph on the pinfin layer (3/0) inside its own
per-letter bbox (4/0), so pflm.build_set detects each letter as an array and the stage steps to
each one, centered in the field -> the letters land at their true positions and spell the text.

Three lines, centered on the wafer, with per-line sizing (LINE_SCALE): the name is emphasized,
the rest smaller (x1.5 line 1, x0.75 lines 2-3). Two souvenirs are generated (see SOUVENIRS).

This also emits a <gds>_params.csv (all letters at LETTER_PASSES / LETTER_SPEED_MM_S);
pass it to `pflm build --params` so every letter gets a uniform etch -- no pipeline edit.
(Alignment crosses use the pipeline's fixed 15-pass ALIGN_ETCH regardless of the CSV.)

Build:  python design/make_souvenir.py
        python -m pflm.cli build design/souvenir_ryan.gds --set souvenir_ryan --params design/souvenir_ryan_params.csv
        python -m pflm.cli build design/souvenir_v2.gds   --set souvenir_v2   --params design/souvenir_v2_params.csv
"""
from __future__ import annotations
import csv
import math
import klayout.db as pya

# Each souvenir: three lines + output GDS. Same text scales for all (LINE_SCALE below).
# souvenir_ryan template = <name> / Stanford NanoHeat / Summer 2026.
SOUVENIRS = [
    {"lines": ["Ryan Scott", "Stanford NanoHeat", "Summer 2026"],        "out": "design/souvenir_ryan.gds"},
    {"lines": ["Jess+Ryan", "California Trip", "Summer 2026"],           "out": "design/souvenir_v2.gds"},
    {"lines": ["Johnathan Martinez", "Stanford NanoHeat", "Summer 2026"], "out": "design/souvenir_johnathan.gds"},
    {"lines": ["Ismael Martinez", "Stanford NanoHeat", "Summer 2026"],    "out": "design/souvenir_ismael.gds"},
]

LINE_SCALE = [1.5, 0.75, 0.75]   # line 1 (name) 50% bigger; lines 2-3 25% smaller
BASE_GLYPH_H_UM = 6000.0         # 1.0x letter height
CELL_GAP_FRAC = 0.15             # inter-letter gap, as a fraction of glyph width
BBOX_HALF_W_FRAC = 0.54          # letter-cell bbox half-width, x glyph width (< advance/2 -> no overlap)
BBOX_HALF_H_FRAC = 0.60          # letter-cell bbox half-height, x glyph height
LINE_GAP_UM = 3500.0             # vertical gap between line edges
WAFER_R_UM = 50000.0
FIT_MAX_R_UM = 43500.0           # shrink the whole block if its farthest corner exceeds this,
                                 # so long names stay inside the r~45 mm fiducial ring (never enlarges)
PIN_LAYER, BBOX_LAYER, FRAME_LAYER, ALIGN_LAYER = (3, 0), (4, 0), (1, 0), (5, 0)

# Alignment fiducials ripped verbatim from 081826_UVPFLMv2.gds (layer 5/0): four crosses
# (H-bar + V-bar) at r~45 mm, outside the text, so a later metalization step can register to
# them. pflm.build_set auto-detects 5/0 and etches each mark last (15-pass crosshatch).
ALIGN_CENTERS_UM = [(-44985.0, 0.0), (45015.0, 0.0), (15.0, -45000.0), (15.0, 45000.0)]
ALIGN_ARM_UM = 1250.0            # cross arm length (full)
ALIGN_W_UM = 50.0                # cross arm width

# Uniform etch recipe for every letter, emitted as a --params manifest CSV (one row per
# letter at its exposed center). NO pipeline change: `pflm build --params <csv>` joins each
# array to its row by nearest exposed position. Assumes the default build rotation 0
# (exposed center == design center); rebuild the CSV if you expose at a nonzero rotation.
LETTER_PASSES = 50
LETTER_SPEED_MM_S = 800.0
LETTER_FILL_STYLE = "crosshatch"
LETTER_FILL_ANGLES = "0/90"
LETTER_HATCH_MM = 0.05           # crosshatch fill pitch (coarse: decorative text, ~10 min/wafer)


def _um(ly, v):
    return int(round(v / ly.dbu))


def build(lines, out, hatch_mm=LETTER_HATCH_MM):
    ly = pya.Layout(); ly.dbu = 0.001
    top = ly.create_cell("SOUVENIR")
    Lp, Lb, Lf = ly.layer(*PIN_LAYER), ly.layer(*BBOX_LAYER), ly.layer(*FRAME_LAYER)
    La = ly.layer(*ALIGN_LAYER)
    tg = pya.TextGenerator.default_generator()

    chars = {c for line in lines for c in line if c != " "}
    h1 = max(tg.text(c, ly.dbu, 1.0).bbox().height() * ly.dbu for c in chars)
    w1 = max(tg.text(c, ly.dbu, 1.0).bbox().width() * ly.dbu for c in chars)

    # Vertical stack, centered as a block on the wafer origin. Per-line metrics + line-center
    # y's for a global scale `fit`; `fit` is chosen so the farthest letter corner <= FIT_MAX_R_UM
    # (a no-op for text that already fits, e.g. souvenir_ryan).
    def metrics(fit):
        heights = [BASE_GLYPH_H_UM * s * fit for s in LINE_SCALE]
        gap = LINE_GAP_UM * fit
        cursor = (sum(heights) + gap * (len(lines) - 1)) / 2.0
        per, ys_ = [], []
        for h in heights:
            mag = h / h1
            gw = w1 * mag
            adv = gw * (1.0 + CELL_GAP_FRAC)
            per.append((h, mag, gw, adv, gw * BBOX_HALF_W_FRAC, h * BBOX_HALF_H_FRAC))
            ys_.append(cursor - h / 2.0)
            cursor -= (h + gap)
        return per, ys_

    def corner(per, ys_):
        cr = 0.0
        for (h, mag, gw, adv, hw, hh), yc, line in zip(per, ys_, lines):
            maxx = adv * (len(line) - 1) / 2.0 + hw   # outer edge of the outermost letter slot
            cr = max(cr, (maxx * maxx + (abs(yc) + hh) ** 2) ** 0.5)
        return cr

    per, line_y = metrics(1.0)
    cr = corner(per, line_y)
    fit = min(1.0, FIT_MAX_R_UM / cr) if cr > 0 else 1.0
    if fit < 1.0:
        per, line_y = metrics(fit)

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
    centers = []   # (exposed_x_um, exposed_y_um) per letter -> params manifest rows
    summary = []
    for li, line in enumerate(lines):
        gh, mag, gw, advance, half_w, half_h = per[li]
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
            centers.append((cx, y))
            xs += [cx - half_w, cx + half_w]; ys += [y - half_h, y + half_h]
        summary.append("  '%s': x%.2f -> glyph %.2f x %.2f mm, line width %.1f mm, y=%+.1f mm"
                       % (line, LINE_SCALE[li], gw / 1000, gh / 1000, n * advance / 1000, y / 1000))

    ly.write(out)

    # Per-letter etch manifest (all letters identical) for `pflm build --params`.
    params_path = out[:-4] + "_params.csv" if out.endswith(".gds") else out + "_params.csv"
    with open(params_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["exposed_x_um", "exposed_y_um", "type", "passes", "speed_mm_s",
                    "fill_style", "fill_angles_deg", "hatch_mm"])
        for (cx, cy) in centers:
            w.writerow(["%.3f" % cx, "%.3f" % cy, "letter", LETTER_PASSES, LETTER_SPEED_MM_S,
                        LETTER_FILL_STYLE, LETTER_FILL_ANGLES, hatch_mm])

    corner_r = max((x * x + yy * yy) ** 0.5 for x in (min(xs), max(xs)) for yy in (min(ys), max(ys)))
    fitnote = "" if fit >= 1.0 else "  (auto-fit x%.3f to clear the r=%.0f mm ring)" % (fit, FIT_MAX_R_UM / 1000)
    print("wrote %s  (%d letters + %d align crosses on 5/0)%s"
          % (out, n_letters, len(ALIGN_CENTERS_UM), fitnote))
    print("\n".join(summary))
    print("  text extent X[%.1f,%.1f] Y[%.1f,%.1f] mm; farthest corner r=%.1f mm (wafer r=50)"
          % (min(xs) / 1000, max(xs) / 1000, min(ys) / 1000, max(ys) / 1000, corner_r / 1000))
    print("  wrote %s  (%d rows @ %d passes, %.0f mm/s %s)\n"
          % (params_path, len(centers), LETTER_PASSES, LETTER_SPEED_MM_S, LETTER_FILL_STYLE))


if __name__ == "__main__":
    for s in SOUVENIRS:
        build(s["lines"], s["out"], s.get("hatch_mm", LETTER_HATCH_MM))
