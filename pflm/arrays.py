"""Per-array bbox detection, row grouping, rotation choice (§5.2) — the heart of PFLM.

``detect_arrays`` works in the **design frame** (wafer microns, +X right, +Y up).
Rotation is chosen by ``choose_rotation`` (extent-based, to fit the stage), and
rows are grouped by ``group_exposed_rows`` in the **exposed (post-rotation) frame**:
a "row" is a physical horizontal band the operator masks as a unit. Grouping MUST
happen after rotation — a design column that the rotation turns into a physical
row must be exposed as a unit, never interleaved with another row (§2.1 / §2.2).

No hardware imports.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

try:
    import pya  # type: ignore
except ImportError:
    import klayout.db as pya

from .layers import layer_indices_for_spec, parse_layer_spec

# NOMINAL taught mapping + usable window for the prep feasibility PRE-CHECK. These mirror the
# definitive laser-PC calibration (laser_pc/exposure_calibration.json) so the prep's reachability
# verdict matches transform.is_reachable; the laser PC re-checks with the real taught reference at
# run time, so treat these as an unverified nominal pre-check.
DEFAULT_STAGE_REF_UM = (84355.0, -19056.0)  # stage target for wafer (0, 0) [definitive]
DEFAULT_AXES = (-1, 1)                       # (sx, sy): stage_X = ref_x - wx (backside mirror), stage_Y = ref_y + wy
# ASYMMETRIC usable stage window (a pipe caps -Y at -38140); matches exposure_calibration.json
# reachable_um. Feasibility projects each array center to an absolute stage target and requires it
# inside this box -- NOT the old symmetric +/-126000 x +/-76000 envelope (which over-stated -Y room).
REACHABLE_UM = (16236.0, 138529.0, -38140.0, 0.0)  # (x_min, x_max, y_min, y_max)
# An array whose ideal stage center lands just beyond REACHABLE_UM is still exposable: the stage
# clamps to the window edge and the galvo covers the residual, so the array lands this far off the
# field center (still at its true wafer location). Feasible only while that field offset stays <=
# this tol, keeping arrays near center. Matches clamp_center / the align-mark handling. (v2's
# top/bottom rows land ~0.4 mm out -- well inside 2.5 mm.)
ARRAY_FIELD_TOL_UM = 2500.0

# Candidate rotations, ordered so ties resolve to the SMALLEST rotation: 0 first
# (no needless flip), then 90 before 270. A design that must rotate to fit still
# scores 90/270 above 0/180 on `long_on_x`, so this order only decides 0-vs-180 and
# 90-vs-270 ties -- and there the smaller rotation wins. Keeping a pre-baked design
# at 0 matters: a per-array etch manifest is written in the rotation-0 frame, so an
# auto flip to 180 would move every array and the manifest join would silently miss.
_ROTATION_CANDIDATES = (0, 90, 180, 270)


@dataclass(frozen=True)
class ArrayBox:
    bbox_um: tuple          # (l, b, r, t) wafer microns
    center_um: tuple        # (cx, cy)
    width_um: float
    height_um: float
    polygon_count: int      # pinfin polygons inside (filled later; 0 at detection)
    has_geometry: bool


@dataclass(frozen=True)
class Row:
    row_index: int
    y_center_um: float
    arrays: tuple           # ArrayBox, sorted left->right (ascending x)


def _dbu_to_um(layout, value_dbu: int) -> float:
    return value_dbu * layout.dbu


def detect_arrays(layout, bbox_spec: str) -> list[ArrayBox]:
    """One ArrayBox per shape on the bbox layer.

    Iterates shapes recursively from the top cell (so array-instanced boxes are
    expanded: each array copy yields its own box). Boxes are NOT merged into a
    single Region — one shape == one array.
    """
    spec = parse_layer_spec(bbox_spec)
    indices = layer_indices_for_spec(layout, spec)
    boxes: list[ArrayBox] = []
    for top in layout.top_cells():
        for index in indices:
            it = top.begin_shapes_rec(index)
            while not it.at_end():
                box = it.shape().bbox().transformed(it.trans())
                left = _dbu_to_um(layout, box.left)
                bottom = _dbu_to_um(layout, box.bottom)
                right = _dbu_to_um(layout, box.right)
                top_um = _dbu_to_um(layout, box.top)
                boxes.append(ArrayBox(
                    bbox_um=(left, bottom, right, top_um),
                    center_um=((left + right) / 2.0, (bottom + top_um) / 2.0),
                    width_um=right - left,
                    height_um=top_um - bottom,
                    polygon_count=0,
                    has_geometry=False,
                ))
                it.next()
    return boxes


def count_pinfins_in(layout, pinfin_spec, bbox_um) -> int:
    """Count pinfin-layer shapes whose center lies inside ``bbox_um``.

    Center-in-box is robust to bboxes that abut (rows are 10.5 mm tall and pitched
    10.5 mm, so adjacent array bboxes touch, but each array's shapes sit inside its
    own box).
    """
    spec = parse_layer_spec(pinfin_spec)
    indices = layer_indices_for_spec(layout, spec)
    left, bottom, right, top = bbox_um
    count = 0
    for topcell in layout.top_cells():
        for index in indices:
            it = topcell.begin_shapes_rec(index)
            while not it.at_end():
                box = it.shape().bbox().transformed(it.trans())
                cx = _dbu_to_um(layout, box.left + box.right) / 2.0
                cy = _dbu_to_um(layout, box.bottom + box.top) / 2.0
                if left <= cx <= right and bottom <= cy <= top:
                    count += 1
                it.next()
    return count


def detect_arrays_from_pinfins(layout, pinfin_spec, *, gap_um: float = 3000.0,
                               row_tol_um: float = None) -> list[ArrayBox]:
    """Fallback when there is no bbox layer: cluster pinfin shapes into arrays.

    Groups shapes into y-bands (rows), then within each band splits into arrays
    wherever the x-gap between consecutive shape bounding boxes exceeds ``gap_um``.
    Each resulting cluster's union bounding box becomes one ArrayBox.
    """
    spec = parse_layer_spec(pinfin_spec)
    indices = layer_indices_for_spec(layout, spec)
    shapes: list[tuple] = []  # (cx, cy, l, b, r, t)
    for topcell in layout.top_cells():
        for index in indices:
            it = topcell.begin_shapes_rec(index)
            while not it.at_end():
                box = it.shape().bbox().transformed(it.trans())
                l = _dbu_to_um(layout, box.left)
                b = _dbu_to_um(layout, box.bottom)
                r = _dbu_to_um(layout, box.right)
                t = _dbu_to_um(layout, box.top)
                shapes.append(((l + r) / 2.0, (b + t) / 2.0, l, b, r, t))
                it.next()
    if not shapes:
        return []

    heights = [t - b for _, _, _, b, _, t in shapes]
    med_h = statistics.median(heights) if heights else 0.0
    if row_tol_um is None:
        row_tol_um = max(0.25 * med_h, 1.0)

    # Bucket into y-bands.
    shapes.sort(key=lambda s: -s[1])
    bands: list[list] = []
    for s in shapes:
        placed = False
        for band in bands:
            if abs(s[1] - band[0][1]) <= max(row_tol_um, med_h * 0.6):
                band.append(s)
                placed = True
                break
        if not placed:
            bands.append([s])

    boxes: list[ArrayBox] = []
    for band in bands:
        band.sort(key=lambda s: s[0])
        cluster: list = []
        prev_right = None
        for s in band:
            if prev_right is not None and (s[2] - prev_right) > gap_um:
                boxes.append(_cluster_box(cluster))
                cluster = []
            cluster.append(s)
            prev_right = s[4] if prev_right is None else max(prev_right, s[4])
        if cluster:
            boxes.append(_cluster_box(cluster))
    return boxes


def _cluster_box(cluster: list) -> ArrayBox:
    left = min(s[2] for s in cluster)
    bottom = min(s[3] for s in cluster)
    right = max(s[4] for s in cluster)
    top = max(s[5] for s in cluster)
    return ArrayBox(
        bbox_um=(left, bottom, right, top),
        center_um=((left + right) / 2.0, (bottom + top) / 2.0),
        width_um=right - left,
        height_um=top - bottom,
        polygon_count=len(cluster),
        has_geometry=len(cluster) > 0,
    )


ALIGN_TOL_UM = 35_000.0   # an alignment mark must land within +/- this of field center


def detect_align_marks(layout, align_spec) -> list:
    """One ArrayBox per DISTINCT alignment-mark position (coincident shapes merged).

    The align layer holds fiducials (e.g. crosses) etched last, for downstream process
    steps. Shapes at the same location (coincident pairs) are merged into one target.
    """
    spec = parse_layer_spec(align_spec)
    indices = layer_indices_for_spec(layout, spec)
    by_pos = {}
    for top in layout.top_cells():
        for index in indices:
            it = top.begin_shapes_rec(index)
            while not it.at_end():
                b = it.shape().bbox().transformed(it.trans())
                l = _dbu_to_um(layout, b.left); bo = _dbu_to_um(layout, b.bottom)
                r = _dbu_to_um(layout, b.right); t = _dbu_to_um(layout, b.top)
                key = (round((l + r) / 2.0 / 1000.0), round((bo + t) / 2.0 / 1000.0))
                if key in by_pos:
                    e = by_pos[key]
                    e[0] = min(e[0], l); e[1] = min(e[1], bo); e[2] = max(e[2], r); e[3] = max(e[3], t)
                else:
                    by_pos[key] = [l, bo, r, t]
                it.next()
    boxes = []
    for key in sorted(by_pos, key=lambda k: (-k[1], k[0])):   # top->bottom, left->right
        l, bo, r, t = by_pos[key]
        boxes.append(ArrayBox(bbox_um=(l, bo, r, t), center_um=((l + r) / 2.0, (bo + t) / 2.0),
                              width_um=r - l, height_um=t - bo, polygon_count=0, has_geometry=True))
    return boxes


def clamp_center(exposed_center, *, reachable_um=REACHABLE_UM,
                 ref=DEFAULT_STAGE_REF_UM, axes=DEFAULT_AXES):
    """Closest reachable field-center for a target beyond the stage window.

    Uses the nominal mapping (`ref`/`axes`) and the asymmetric usable window `reachable_um`
    (x_min, x_max, y_min, y_max). Returns (eff_center_um, field_offset_um): drive the stage to
    center on eff_center (inside the window), and the target then lands off-center in the field
    by field_offset = target - eff_center (so it still exposes at its true location)."""
    mx, my = exposed_center
    rx, ry = ref
    sx, sy = axes
    x_min, x_max, y_min, y_max = reachable_um
    s_ideal_x = rx + sx * mx
    s_ideal_y = ry + sy * my
    s_cl_x = min(max(s_ideal_x, x_min), x_max)
    s_cl_y = min(max(s_ideal_y, y_min), y_max)
    eff_x = (s_cl_x - rx) / sx
    eff_y = (s_cl_y - ry) / sy
    return (eff_x, eff_y), (mx - eff_x, my - eff_y)


def rotate_point_um(pt, deg) -> tuple:
    """Exact k*90 rotation about the wafer origin (CCW positive).

    +90 maps (x, y) -> (-y, x), matching pya.Trans(1) and the exposed-frame
    convention in the plan schema.
    """
    x, y = pt
    k = int(round(deg / 90.0)) % 4
    if k == 0:
        return (float(x), float(y))
    if k == 1:
        return (float(-y), float(x))
    if k == 2:
        return (float(-x), float(-y))
    return (float(y), float(-x))


def exposed_centers(boxes, deg):
    """Rotated (exposed-frame) centers for every box."""
    return [rotate_point_um(b.center_um, deg) for b in boxes]


def rotation_feasibility(boxes, deg, *, reachable_um=REACHABLE_UM,
                         ref=DEFAULT_STAGE_REF_UM, axes=DEFAULT_AXES,
                         field_tol_um=ARRAY_FIELD_TOL_UM) -> dict:
    """Stage feasibility for a k*90 design rotation (§2.1), against the asymmetric window.

    Each array's rotated (exposed-frame) center is projected to an absolute stage target and
    CLAMPED to ``reachable_um`` (clamp_center); the residual field offset (how far the array lands
    off the field center, covered by galvo deflection) must stay within ``field_tol_um``. So an
    array whose ideal target is up to field_tol_um outside the window is still exposable -- the same
    clamp-and-offset the align marks / the laser PC use. Sweep/advance spans are reported so
    choose_rotation can prefer the long extent on stage-X (the tighter -Y axis). The nominal mapping
    is unverified; the laser PC re-checks with the real reference.
    """
    cs = exposed_centers(boxes, deg) or [(0.0, 0.0)]
    xs = [c[0] for c in cs]
    ys = [c[1] for c in cs]
    sweep_span = max(xs) - min(xs)          # along stage-X (within a row)
    advance_span = max(ys) - min(ys)        # along stage-Y (between rows)
    rx, ry = ref
    sx, sy = axes
    stage = [(rx + sx * x, ry + sy * y) for x, y in cs]   # ideal (unclamped) targets, for reporting
    stage_xs = [p[0] for p in stage]
    stage_ys = [p[1] for p in stage]
    offs = [clamp_center(c, reachable_um=reachable_um, ref=ref, axes=axes)[1] for c in cs]
    max_off = max((max(abs(o[0]), abs(o[1])) for o in offs), default=0.0)
    feasible = max_off <= field_tol_um
    return {
        "deg": int(deg) % 360,
        "feasible": bool(feasible),
        "sweep_span_um": float(sweep_span),
        "row_advance_span_um": float(advance_span),
        "max_field_offset_um": float(max_off),
        "field_tol_um": float(field_tol_um),
        "max_stage_y_um": float(max(stage_ys)),
        "min_stage_y_um": float(min(stage_ys)),
        "max_stage_x_um": float(max(stage_xs)),
        "min_stage_x_um": float(min(stage_xs)),
        "sweep_axis": "stage_x",
        "row_advance_axis": "stage_y",
        "long_on_x": bool(sweep_span >= advance_span),
    }


def choose_rotation(boxes, *, reachable_um=REACHABLE_UM,
                    ref=DEFAULT_STAGE_REF_UM, axes=DEFAULT_AXES,
                    field_tol_um=ARRAY_FIELD_TOL_UM) -> dict:
    """Pick a k*90 design rotation (§2.1).

    Prefer a stage-feasible rotation (every array's clamped field offset within field_tol_um)
    that puts the longer array-extent on stage-X (so the short extent rides the tighter -Y axis).
    Deterministic; ties resolve to the smallest rotation (candidate order 0, 90, 180, 270), so a
    design already at the right orientation stays at 0.
    Returns a ``rotation_feasibility`` dict for the chosen degree.
    """
    best = None
    for deg in _ROTATION_CANDIDATES:
        f = rotation_feasibility(boxes, deg, reachable_um=reachable_um, ref=ref, axes=axes,
                                 field_tol_um=field_tol_um)
        score = (1 if f["feasible"] else 0, 1 if f["long_on_x"] else 0)
        if best is None or score > best[0]:
            best = (score, f)
    return best[1]


def group_exposed_rows(boxes, deg, row_tol_um: float = None) -> list:
    """Group arrays into PHYSICAL rows in the exposed (post-rotation) frame.

    A "row" is a horizontal band at (near-)constant exposed-Y — the band the
    operator masks as a unit. Rows are ordered top->bottom (exposed-Y desc);
    arrays within a row are ordered left->right (exposed-X asc). This is what
    prevents mixing rows after rotation: a design column that the rotation turns
    into a physical row is exposed as a unit, never interleaved with another row.

    ``row_tol_um`` clusters exposed-Y centers; default = half the median gap
    between consecutive distinct exposed-Y values (robust for grid layouts).
    """
    if not boxes:
        return []
    ex = [(b, rotate_point_um(b.center_um, deg)) for b in boxes]
    if row_tol_um is None:
        ys = sorted(c[1] for _, c in ex)
        gaps = [b - a for a, b in zip(ys, ys[1:]) if (b - a) > 1.0]
        pitch = statistics.median(gaps) if gaps else 0.0
        row_tol_um = 0.5 * pitch if pitch > 0 else 2000.0
    ex.sort(key=lambda t: -t[1][1])         # top (highest exposed-y) first
    bands: list = []
    band_y: list = []
    for b, c in ex:
        cy = c[1]
        placed = False
        for i, y in enumerate(band_y):
            if abs(cy - y) <= row_tol_um:
                bands[i].append((b, c))
                band_y[i] = sum(t[1][1] for t in bands[i]) / len(bands[i])
                placed = True
                break
        if not placed:
            bands.append([(b, c)])
            band_y.append(cy)
    order = sorted(range(len(bands)), key=lambda i: -band_y[i])
    rows: list = []
    for row_index, bi in enumerate(order):
        arrs = sorted(bands[bi], key=lambda t: t[1][0])   # left->right by exposed-x
        y_center = sum(t[1][1] for t in arrs) / len(arrs)
        rows.append(Row(row_index=row_index, y_center_um=y_center,
                        arrays=tuple(b for b, _ in arrs)))
    return rows
