"""Build PFLM exposure wafer v2: staggered 10-cell layout, centered on the wafer.

v2 differs from build_wafer.py:
  - explicit 10-cell STAGGERED layout (top 3 / middle 4 / bottom 3), not the 14-cell
    interlock, so every pin-field boundary clears its neighbours by >= ~9.4 mm
    (the 7-10 mm process clearance) while keeping 3 clean exposure rows;
  - geometry is authored directly in the EXPOSED frame (no +90 bake) -- the rows
    already stack along stage-Y and sweep along stage-X, so prep runs at rotation 0;
  - the frame (wafer 1/0, edge 6/0, alignment marks 5/0) is copied from an existing
    built GDS (already in the exposed frame) so the fiducials are unchanged;
  - the layout is centered on the origin, leaving room for the four edge fiducials.

Round pin = 512-gon circle on layer 2/0. Emits GDS + manifest CSV (per-cell type +
etch params) that pflm.cli build --params consumes. Pure klayout.db.

Usage:
    python design/build_wafer_v2.py FRAME_SRC.gds [-o OUT.gds] [-m MANIFEST.csv]
                                    [--etch-params P.json]
"""

from __future__ import annotations
import argparse, math, os, csv, json
import klayout.db as db

import build_wafer as bw   # reuse circle_polygon / square_centers / SPECS / constants

DBU = bw.DBU
SEGS = bw.CIRCLE_SEGS
HALF = bw.FILL_HALF_UM                 # 5000 um -> 10x10 mm pin-field box
PIN_LAYER = bw.PIN_LAYER               # (2, 0)
BOX_LAYER = (0, 0)                     # 10x10 pin-field box
CELL_LAYER = (4, 0)                    # 10.5 x 38.7 mm chip footprint (the "cell")
CUT_LAYER = (10, 0)                    # NEW: dicing cut-lines (not exposed; saw reference)
FRAME_LAYERS = [(1, 0), (5, 0), (6, 0)]  # wafer, alignment marks, edge -- copied as-is
CELL_W_UM, CELL_H_UM = 10500.0, 38700.0
WAFER_R_UM = 50000.0                    # 100 mm wafer -> cut lines run out to this edge
DEFAULT_KERF_UM = 200.0                 # dicing street width between chips

ETCH_PARAMS = os.path.join(os.path.dirname(__file__), "etch_params.json")
DEFAULT_OUT = "081826_UVPFLMv2.gds"

# Staggered 10-cell layout as (column_index kx, row {+1 top, 0 mid, -1 bot}, kind,
# lattice). Columns are pitched (cell_width + kerf); rows split by the kerf at y=0 so a
# dicing street opens between the stacked top/bottom chips. Middle row (odd kx) nestles
# in the top/bottom (even kx) gaps. Symmetric about x=0 -> fiducials at (+-45,0)/(0,+-45)
# stay clear. Type map per request: hex top / mixed middle / square bottom.
BASE = [
    # top row (D50 hex, D100 hex, D300 hex)
    (-2, +1, "D50",  "hex"), (0, +1, "D100", "hex"), (2, +1, "D300", "hex"),
    # middle row (D100 sq, D300 sq, D100 hex, D300 hex)
    (-3, 0, "D100", "sq"), (-1, 0, "D300", "sq"), (1, 0, "D100", "hex"), (3, 0, "D300", "hex"),
    # bottom row (D50 sq, D100 sq, D300 sq)
    (-2, -1, "D50",  "sq"), (0, -1, "D100", "sq"), (2, -1, "D300", "sq"),
]


def layout_cells(kerf_um):
    """Resolve BASE (kx,row) -> (x_um, y_um, kind, lattice) for a given kerf.

    Column pitch = cell width + kerf (a kerf-wide vertical street between columns);
    top/bottom rows shift out by kerf/2 so a kerf-wide horizontal street opens at y=0."""
    col_pitch = CELL_W_UM + kerf_um
    row_off = CELL_H_UM / 2.0 + kerf_um / 2.0   # top/bottom chip center offset from y=0
    out = []
    for kx, row, kind, lat in BASE:
        out.append((kx * col_pitch, row * row_off, kind, lat))
    return out


def _um(v):
    return int(round(v / DBU))


def _box(cx, cy, w, h):
    return db.Box(_um(cx - w / 2), _um(cy - h / 2), _um(cx + w / 2), _um(cy + h / 2))


def hex_offsets(kind):
    """Centered, bounded hex pin centers (um) inside the +-HALF box (same rule as
    build_wafer.make_array: drop any pin whose edge would overrun the 10 mm box)."""
    d, pitch = bw.SPECS[kind]
    dy = pitch * math.sqrt(3) / 2.0
    limit = HALF - d / 2.0
    out = []
    jmax = int(limit / dy)
    for j in range(-jmax, jmax + 1):
        y = j * dy
        if abs(y) > limit + 1e-6:
            continue
        off = 0.0 if (j % 2 == 0) else pitch / 2.0
        imax = int((limit + off) / pitch) + 1
        for i in range(-imax, imax + 1):
            x = i * pitch + off
            if abs(x) <= limit + 1e-6:
                out.append((x, y))
    return out


def _disk_region(r_um, segs=256):
    pts = [db.Point(_um(r_um * math.cos(2 * math.pi * k / segs)),
                    _um(r_um * math.sin(2 * math.pi * k / segs))) for k in range(segs)]
    return db.Region(db.Polygon(pts))


def build_cut_lines(cells, kerf_um):
    """200um (kerf-wide) dicing streets. Vertical streets between columns run full
    height to the wafer edge; horizontal streets are drawn full width then the cells
    are subtracted, so they break into segments wherever a staggered cell blocks the
    path (i.e. only the cuts that CAN reach the edge do). Clipped to the wafer disk.

    Returns a Region on CUT_LAYER coordinates (um-based Boxes in dbu)."""
    hk = kerf_um / 2.0
    col_pitch = CELL_W_UM + kerf_um
    kxs = sorted({kx for kx, *_ in BASE})
    # vertical street centers: midway between every pair of adjacent columns, plus the
    # two outer edges of the outermost columns.
    vcenters = [(k + 0.5) * col_pitch for k in range(min(kxs) - 1, max(kxs) + 1)]
    # horizontal street centers: y=0 (between stacked top/bottom chips) and the middle
    # row's top/bottom chip edges (+-CELL_H/2) so every chip can be released.
    hcenters = [0.0, CELL_H_UM / 2.0, -CELL_H_UM / 2.0]

    cut = db.Region()
    for vc in vcenters:
        cut += db.Region(db.Box(_um(vc - hk), _um(-WAFER_R_UM), _um(vc + hk), _um(WAFER_R_UM)))
    for hc in hcenters:
        cut += db.Region(db.Box(_um(-WAFER_R_UM), _um(hc - hk), _um(WAFER_R_UM), _um(hc + hk)))

    cells_reg = db.Region()
    for (cx, cy, _k, _l) in cells:
        cells_reg += db.Region(_box(cx, cy, CELL_W_UM, CELL_H_UM))
    cut -= cells_reg                        # break lines wherever a cell blocks them
    cut &= _disk_region(WAFER_R_UM)         # clip to the round wafer edge
    return cut


def build(frame_src, out_path, kerf_um=DEFAULT_KERF_UM):
    src = db.Layout(); src.read(frame_src)
    W = src.cell("Wafer") or src.top_cells()[0]

    out = db.Layout(); out.dbu = DBU
    top = out.create_cell("Wafer")

    # ---- copy frame layers (wafer / align marks / edge) exactly, in exposed frame --
    frame_counts = {}
    align_reg = db.Region()
    for (ln, ldt) in FRAME_LAYERS:
        li_src = src.find_layer(ln, ldt)
        if li_src is None:
            frame_counts[f"{ln}/{ldt}"] = 0
            continue
        reg = db.Region(W.begin_shapes_rec(li_src))
        top.shapes(out.layer(ln, ldt)).insert(reg)
        frame_counts[f"{ln}/{ldt}"] = reg.count()
        if (ln, ldt) == (5, 0):
            align_reg = reg.dup()

    li_pin = out.layer(*PIN_LAYER)
    li_box = out.layer(*BOX_LAYER)
    li_cell = out.layer(*CELL_LAYER)

    pin_cell = {}
    for kind, (d, _p) in bw.SPECS.items():
        c = out.create_cell(f"Pin_{kind}")
        c.shapes(li_pin).insert(bw.circle_polygon(d, DBU))
        pin_cell[kind] = c

    cells = layout_cells(kerf_um)
    placed = []
    for (cx, cy, kind, lattice) in cells:
        d, pitch = bw.SPECS[kind]
        pc = pin_cell[kind].cell_index()
        top.shapes(li_cell).insert(_box(cx, cy, CELL_W_UM, CELL_H_UM))
        top.shapes(li_box).insert(_box(cx, cy, 2 * HALF, 2 * HALF))
        if lattice == "sq":
            xs = bw.square_centers(pitch, d)
            x0 = cx + xs[0]; y0 = cy + xs[0]; n = len(xs)
            top.insert(db.CellInstArray(
                pc, db.Trans(db.Vector(_um(x0), _um(y0))),
                db.Vector(_um(pitch), 0), db.Vector(0, _um(pitch)), n, n))
            npins = n * n
        else:
            offs = hex_offsets(kind)
            for (x, y) in offs:
                top.insert(db.CellInstArray(
                    pc, db.Trans(db.Vector(_um(cx + x), _um(cy + y)))))
            npins = len(offs)
        placed.append((cx, cy, kind, lattice, npins))

    # ---- dicing cut lines (keep off the fiducials) ---------------------------
    cut = build_cut_lines(cells, kerf_um)
    if not align_reg.is_empty():
        cut -= align_reg.sized(_um(kerf_um))      # small keep-out around the marks
    top.shapes(out.layer(*CUT_LAYER)).insert(cut)

    out.write(out_path)
    return out, top, frame_counts, placed


def write_manifest(placed, manifest_path, etch_params_path=ETCH_PARAMS):
    etch = json.load(open(etch_params_path))["types"]
    cols = ["type", "lattice", "diameter_um", "pitch_um",
            "design_x_um", "design_y_um", "exposed_x_um", "exposed_y_um",
            "pin_count", "passes", "speed_mm_s", "fill_style", "fill_angles_deg", "hatch_mm"]
    rows = []
    for cx, cy, kind, lat, n in placed:
        key = f"{kind}_{lat}"
        e = etch[key]
        rows.append({
            "type": key, "lattice": e["lattice"], "diameter_um": e["diameter_um"],
            "pitch_um": e["pitch_um"],
            "design_x_um": round(cx), "design_y_um": round(cy),
            "exposed_x_um": round(cx), "exposed_y_um": round(cy),   # no rotation in v2
            "pin_count": n, "passes": e["passes"], "speed_mm_s": e["speed_mm_s"],
            "fill_style": "crosshatch",
            "fill_angles_deg": "/".join(str(a) for a in e["fill_angles_deg"]),
            "hatch_mm": 0.01,
        })
    rows.sort(key=lambda r: (-r["exposed_y_um"], r["exposed_x_um"]))
    with open(manifest_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
    return rows


def _default_manifest(out_path):
    base, _ = os.path.splitext(out_path)
    return base + "_manifest.csv"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build PFLM wafer v2 (staggered 10-cell, centered).")
    ap.add_argument("frame_src", help="existing GDS to copy the frame (wafer/align/edge) from")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT, help="output GDS (default: %(default)s)")
    ap.add_argument("-m", "--manifest", default=None, help="manifest CSV (default: <out>_manifest.csv)")
    ap.add_argument("--etch-params", default=ETCH_PARAMS, help="etch-params JSON")
    ap.add_argument("--kerf", type=float, default=DEFAULT_KERF_UM / 1000.0,
                    help="dicing street width in mm between chips (default: %(default)s)")
    args = ap.parse_args()
    man = args.manifest or _default_manifest(args.out)
    kerf_um = args.kerf * 1000.0

    out, top, frame, placed = build(args.frame_src, args.out, kerf_um=kerf_um)
    print("wrote", args.out)
    print(f"frame copied (recursive shape counts): {frame}   kerf = {args.kerf:g} mm")
    print(f"cells placed: {len(placed)}  (pin circle resolution: {SEGS}-gon)")
    rows = write_manifest(placed, man, args.etch_params)
    print("wrote manifest", man)
    for r in rows:
        print(f"   exposed({r['exposed_x_um']:+6d},{r['exposed_y_um']:+6d})  {r['type']:9s}"
              f"  {r['passes']} passes  {r['fill_angles_deg']:7s}  ~{r['pin_count']} pins")
    bb = db.CplxTrans(out.dbu) * top.bbox()
    print(f"top bbox_um: ({bb.left:.0f},{bb.bottom:.0f},{bb.right:.0f},{bb.top:.0f})")
