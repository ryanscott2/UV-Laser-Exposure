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

# Singulation's solved taught mapping (§2.1 / §6), same wafer/stage/jig family.
# Used ONLY as a machine-independent default to project design coordinates onto a
# nominal stage-Y so ``choose_rotation`` can test the P3/P4 ceiling. Treated as an
# unverified starting point; the laser PC re-checks with the real taught reference.
DEFAULT_STAGE_REF_UM = (5590.0, -18450.0)  # stage target for wafer (0, 0)
DEFAULT_AXES = (-1, 1)                      # (sx, sy): stage_X = 5590 - wx, stage_Y = -18450 + wy

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


def group_rows(boxes, row_tol_um: float = None) -> list[Row]:
    """Bucket ArrayBoxes into y-bands, order rows top->bottom, arrays left->right.

    Default tolerance = 25% of the median box height. ``row_index`` 0 is the
    highest design-y row (first physical stripe after rotation).
    """
    if not boxes:
        return []
    heights = [b.height_um for b in boxes]
    med_h = statistics.median(heights)
    if row_tol_um is None:
        row_tol_um = 0.25 * med_h

    ordered = sorted(boxes, key=lambda b: -b.center_um[1])
    bands: list[list[ArrayBox]] = []
    band_y: list[float] = []
    for box in ordered:
        cy = box.center_um[1]
        placed = False
        for i, y in enumerate(band_y):
            if abs(cy - y) <= row_tol_um:
                bands[i].append(box)
                # running mean keeps the band center stable
                band_y[i] = sum(b.center_um[1] for b in bands[i]) / len(bands[i])
                placed = True
                break
        if not placed:
            bands.append([box])
            band_y.append(cy)

    # Sort bands top->bottom by their mean y.
    order = sorted(range(len(bands)), key=lambda i: -band_y[i])
    rows: list[Row] = []
    for row_index, bi in enumerate(order):
        arrays = tuple(sorted(bands[bi], key=lambda b: b.center_um[0]))
        y_center = sum(b.center_um[1] for b in arrays) / len(arrays)
        rows.append(Row(row_index=row_index, y_center_um=y_center, arrays=arrays))
    return rows


ALIGN_TOL_UM = 25_000.0   # an alignment mark must land within +/- this of field center


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


def clamp_center(exposed_center, *, travel_um=(126000, 76000), stage_y_max_um=6950):
    """Closest reachable field-center for a target beyond the stage envelope.

    Uses the fixed default mapping (DEFAULT_STAGE_REF_UM / DEFAULT_AXES). Returns
    (eff_center_um, field_offset_um): drive the stage to center on eff_center (which
    is inside travel + under the ceiling), and the target then lands off-center in the
    field by field_offset = target - eff_center (so it exposes at its true location)."""
    mx, my = exposed_center
    rx, ry = DEFAULT_STAGE_REF_UM
    sx, sy = DEFAULT_AXES
    tx, ty = travel_um
    s_ideal_x = rx + sx * mx
    s_ideal_y = ry + sy * my
    s_cl_x = min(max(s_ideal_x, -tx), tx)
    s_cl_y = min(max(s_ideal_y, -ty), stage_y_max_um)
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


def rotation_feasibility(boxes, deg, *, travel_um=(126000, 76000),
                         stage_y_max_um=6950) -> dict:
    """Extent-based stage feasibility for a k*90 design rotation (§2.1).

    After rotation, exposure sweeps WITHIN a physical row along exposed-X (rides
    stage-X) and ADVANCES between rows along exposed-Y (rides stage-Y). So the
    exposed-X extent must fit stage-X travel, the exposed-Y extent must fit
    stage-Y travel, and every array's projected stage-Y must stay at/below the
    P3/P4 ceiling. Projection uses the default taught mapping (unverified; the
    laser PC re-checks with the real reference).
    """
    cs = exposed_centers(boxes, deg) or [(0.0, 0.0)]
    xs = [c[0] for c in cs]
    ys = [c[1] for c in cs]
    sweep_span = max(xs) - min(xs)          # along stage-X (within a row)
    advance_span = max(ys) - min(ys)        # along stage-Y (between rows)
    ref_y = DEFAULT_STAGE_REF_UM[1]
    sy = DEFAULT_AXES[1]
    stage_ys = [ref_y + sy * y for y in ys]
    max_sy, min_sy = max(stage_ys), min(stage_ys)
    tx, ty = travel_um
    feasible = (sweep_span <= tx and advance_span <= ty
                and max_sy <= stage_y_max_um and min_sy >= -ty)
    return {
        "deg": int(deg) % 360,
        "feasible": bool(feasible),
        "sweep_span_um": float(sweep_span),
        "row_advance_span_um": float(advance_span),
        "max_stage_y_um": float(max_sy),
        "sweep_axis": "stage_x",
        "row_advance_axis": "stage_y",
        "long_on_x": bool(sweep_span >= advance_span),
    }


def choose_rotation(boxes, *, travel_um=(126000, 76000), stage_y_max_um=6950) -> dict:
    """Pick a k*90 design rotation (§2.1).

    Prefer a stage-feasible rotation that puts the longer array-extent on stage-X
    (so the short extent rides the ceiling-limited stage-Y). Deterministic; ties
    resolve to the smallest rotation (candidate order 0, 90, 180, 270), so a design
    already at the right orientation stays at 0.
    Returns a ``rotation_feasibility`` dict for the chosen degree.
    """
    best = None
    for deg in _ROTATION_CANDIDATES:
        f = rotation_feasibility(boxes, deg, travel_um=travel_um,
                                 stage_y_max_um=stage_y_max_um)
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
