"""Build the rotated PFLM exposure wafer GDS.

Takes the existing frame (chip footprints 4/0, pin-field boxes 0/0, wafer 1/0,
edge 6/0, align marks 5/0) from a source GDS's `Wafer` cell EXACTLY as-is,
drops the old pinfins (2/0) and heater/pads (3/0), generates the six pinfin
lattices (round pins, {D50/P100, D100/P150, D300/P350} x {square, hex}) filling
the ~1 cm^2 (10 x 10 mm) pin-field box centered in each chip, places one array
type per chip per the row layout, and bakes a +90 deg rotation of the whole
assembly so the long row-stack axis rides stage-X.

Round pin = 32-gon circle on layer 2/0 (matches the source pin style). Lattices
are emitted as regular-array instances of a single pin cell, so the GDS stays
small. Pure klayout.db; run with the standalone wheel.

Usage:
    python design/build_wafer.py SRC.gds [-o OUT.gds] [-m MANIFEST.csv] [--etch-params P.json]

Paths are passed on the command line (see -h); nothing is hardcoded to a
particular machine. --out defaults to PFLM_Wafer_v1_rot90.gds in the current
directory, and the manifest defaults to <out>_manifest.csv beside it.
"""

from __future__ import annotations
import argparse, math, sys, os, json, csv
import klayout.db as db

ETCH_PARAMS = os.path.join(os.path.dirname(__file__), "etch_params.json")
DEFAULT_OUT = "PFLM_Wafer_v1_rot90.gds"   # output GDS name in the CWD; override with --out
DBU = 0.001                      # 1 nm, matches source
FRAME_LAYERS = [(0, 0), (1, 0), (4, 0), (5, 0), (6, 0)]  # kept exactly (drop 2/0 old pins, 3/0 heater/pads)
PIN_LAYER = (2, 0)
FILL_HALF_UM = 5000.0            # 10 x 10 mm pin-field box, centered in each chip
ROT_DEG = 90                     # baked design rotation (+90 CCW): long axis -> stage-X
CIRCLE_SEGS = 512                # round-pin polygon resolution (per request)

# type -> (diameter_um, pitch_um)
SPECS = {"D50": (50.0, 100.0), "D100": (100.0, 150.0), "D300": (300.0, 350.0)}


def circle_polygon(diam_um, dbu, segs=CIRCLE_SEGS):
    r = (diam_um / 2.0) / dbu
    pts = [db.Point(round(r * math.cos(2 * math.pi * k / segs)),
                    round(r * math.sin(2 * math.pi * k / segs))) for k in range(segs)]
    return db.Polygon(pts)


def square_centers(pitch, diam, half=FILL_HALF_UM):
    """1-D symmetric centers at `pitch`, pins staying inside +/-half."""
    limit = half - diam / 2.0
    n = int((2 * limit) // pitch)          # gaps
    start = -n * pitch / 2.0
    return [start + i * pitch for i in range(n + 1)]


def build_layout(src_path, out_path):
    src = db.Layout(); src.read(src_path)
    W = src.cell("Wafer")
    if W is None:
        raise SystemExit("no 'Wafer' cell in source")

    out = db.Layout(); out.dbu = DBU
    top = out.create_cell("Wafer")               # final (rotated) top
    design = out.create_cell("Wafer_design")     # unrotated assembly

    # ---- copy frame layers EXACTLY (flattened into the design cell) ----------
    frame_counts = {}
    for (ln, ldt) in FRAME_LAYERS:
        li_src = src.find_layer(ln, ldt)
        if li_src is None:
            frame_counts[f"{ln}/{ldt}"] = 0
            continue
        li_out = out.layer(ln, ldt)
        reg = db.Region(W.begin_shapes_rec(li_src))   # in Wafer coords, dbu units
        design.shapes(li_out).insert(reg)
        frame_counts[f"{ln}/{ldt}"] = reg.count()

    # ---- chip footprint centers (from 4/0), the 14 placement points ----------
    li_fp = src.find_layer(4, 0)
    fps = []
    for p in db.Region(W.begin_shapes_rec(li_fp)).each():
        b = p.bbox()
        fps.append(((b.left + b.right) / 2.0 * DBU, (b.bottom + b.top) / 2.0 * DBU))
    # snap x to {-19350, 0, +19350}
    def snapx(x): return min((-19350.0, 0.0, 19350.0), key=lambda t: abs(t - x))
    chips = [(snapx(cx), cy) for cx, cy in fps]

    # ---- pin cells + array cells (one per used type) -------------------------
    li_pin = out.layer(*PIN_LAYER)
    pin_cell = {}
    for name, (d, _p) in SPECS.items():
        c = out.create_cell(f"Pin_{name}")
        c.shapes(li_pin).insert(circle_polygon(d, DBU))
        pin_cell[name] = c

    def make_array(kind, lattice):
        d, pitch = SPECS[kind]
        ac = out.create_cell(f"Arr_{kind}_{lattice}")
        pc = pin_cell[kind].cell_index()
        pu = pitch  # um
        if lattice == "sq":
            xs = square_centers(pitch, d)
            x0 = xs[0]; nx = len(xs)
            inst = db.CellInstArray(pc, db.Trans(db.Vector(round(x0 / DBU), round(x0 / DBU))),
                                    db.Vector(round(pu / DBU), 0), db.Vector(0, round(pu / DBU)),
                                    nx, nx)
            ac.insert(inst)
            n = nx * nx
        else:  # hex: centered, bounded, staggered lattice. Even rows x = {i*P}; odd rows
               # x = {(i+/-0.5)*P} (symmetric about 0). Every pin is kept ONLY if it lies
               # fully inside the 10 mm box (|x|,|y| <= half - d/2), so nothing overruns.
            dy = pitch * math.sqrt(3) / 2.0
            limit = FILL_HALF_UM - d / 2.0        # pin-center bound so the pin edge stays inside
            n = 0
            jmax = int(limit / dy)
            for j in range(-jmax, jmax + 1):
                y = j * dy
                if abs(y) > limit + 1e-6:
                    continue
                off = 0.0 if (j % 2 == 0) else pitch / 2.0
                imax = int((limit + off) / pitch) + 1
                for i in range(-imax, imax + 1):
                    x = i * pitch + off           # i-symmetric: {i*P+P/2} == {+/-P/2, +/-3P/2, ...}
                    if abs(x) <= limit + 1e-6:
                        ac.insert(db.CellInstArray(
                            pc, db.Trans(db.Vector(round(x / DBU), round(y / DBU)))))
                        n += 1
        return ac, n

    # ---- layout assignment (design frame) ------------------------------------
    # design_x = +19350 -> TOP exposed row (square); 0 -> middle; -19350 -> BOTTOM (hex)
    # within a column, ordered by design_y DESC -> exposed left->right
    def type_for(cx, cy):
        if abs(cx) < 1000:                       # middle singles
            return ("D50", "sq") if cy > 0 else ("D50", "hex")
        lattice = "sq" if cx > 0 else "hex"
        order = sorted([c for c in chips if abs(c[0] - cx) < 1000], key=lambda t: -t[1])
        idx = [round(c[1]) for c in order].index(round(cy))
        kind = ["D50", "D50", "D100", "D100", "D300", "D300"][idx]
        return (kind, lattice)

    arr_cache = {}
    placed = []
    for cx, cy in chips:
        kind, lattice = type_for(cx, cy)
        key = (kind, lattice)
        if key not in arr_cache:
            arr_cache[key] = make_array(kind, lattice)
        ac, npins = arr_cache[key]
        design.insert(db.CellInstArray(ac.cell_index(),
                      db.Trans(db.Vector(round(cx / DBU), round(cy / DBU)))))
        placed.append((cx, cy, kind, lattice, npins))

    # ---- bake +90 rotation: instance design into top rotated -----------------
    top.insert(db.CellInstArray(design.cell_index(), db.Trans(ROT_DEG // 90, False, 0, 0)))

    out.write(out_path)
    return out, top, frame_counts, placed


def write_manifest(placed, manifest_path, etch_params_path=ETCH_PARAMS):
    """Tie each chip to its type + etch params, in both design and exposed (rotated) frames."""
    etch = json.load(open(etch_params_path))["types"]
    cols = ["type", "lattice", "diameter_um", "pitch_um",
            "design_x_um", "design_y_um", "exposed_x_um", "exposed_y_um",
            "pin_count", "passes", "speed_mm_s", "fill_style", "fill_angles_deg", "hatch_mm"]
    rows = []
    for cx, cy, kind, lat, n in placed:
        key = f"{kind}_{lat}"
        e = etch[key]
        ex, ey = -cy, cx  # +90 CCW rotation (x,y)->(-y,x)
        rows.append({
            "type": key, "lattice": e["lattice"], "diameter_um": e["diameter_um"], "pitch_um": e["pitch_um"],
            "design_x_um": round(cx), "design_y_um": round(cy),
            "exposed_x_um": round(ex), "exposed_y_um": round(ey),
            "pin_count": n, "passes": e["passes"], "speed_mm_s": e["speed_mm_s"],
            "fill_style": "crosshatch", "fill_angles_deg": "/".join(str(a) for a in e["fill_angles_deg"]),
            "hatch_mm": 0.01,
        })
    rows.sort(key=lambda r: (-r["exposed_y_um"], r["exposed_x_um"]))  # exposed top->bottom, left->right
    with open(manifest_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
    return rows


def _default_manifest(out_path):
    """Manifest CSV beside the output GDS: `<out stem>_manifest.csv`."""
    base, _ext = os.path.splitext(out_path)
    return base + "_manifest.csv"


def _parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Build the rotated PFLM exposure wafer GDS from a source GDS.")
    ap.add_argument("src", help="source GDS containing the 'Wafer' cell (frame + old pins)")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT,
                    help="output GDS path (default: %(default)s in the current directory)")
    ap.add_argument("-m", "--manifest", default=None,
                    help="manifest CSV path (default: <out>_manifest.csv beside --out)")
    ap.add_argument("--etch-params", default=ETCH_PARAMS,
                    help="etch-params JSON (default: etch_params.json next to this script)")
    return ap.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    manifest_path = args.manifest or _default_manifest(args.out)

    out, top, frame, placed = build_layout(args.src, args.out)
    print("wrote", args.out)
    print("frame kept (recursive shape counts):", frame)
    print(f"chips placed: {len(placed)}  (pin circle resolution: {CIRCLE_SEGS}-gon)")
    rows = write_manifest(placed, manifest_path, args.etch_params)
    print("wrote manifest", manifest_path)
    print("exposed-frame layout (top->bottom, left->right):")
    for r in rows:
        print(f"   exposed({r['exposed_x_um']:+6d},{r['exposed_y_um']:+6d})  {r['type']:9s}"
              f"  {r['passes']} passes / S{r['speed_mm_s']}  {r['fill_angles_deg']:7s}  ~{r['pin_count']} pins")
    bb = db.CplxTrans(out.dbu) * top.bbox()
    print(f"top (rotated) bbox_um: ({bb.left:.0f},{bb.bottom:.0f},{bb.right:.0f},{bb.top:.0f})")
