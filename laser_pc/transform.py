"""Wafer -> stage transform and alignment helpers (laser PC).

PURE MATH + json only. No numpy, no serial, no klayout/ezdxf. Python 3.8 compatible
(no `match`, no runtime `X | Y` unions, no walrus-only tricks). Hardware (`optiscan`)
is imported *lazily* inside `teach_reference` so this module imports on any machine.

This is the laser-PC side of ARCHITECTURE.md section 6. Alignment is mechanical (jig +
wafer flats fix the wafer) plus ONE taught reference; there are no optics in the loop.
The transform consumes each array's ``exposed_center_um`` (the array center in the
design frame *after* ``design_rotation_deg`` -- section 2.1) and returns the absolute
OptiScan stage target (microns) that places that array center on the fixed laser field
center (auto-centering is OFF at the laser, always -- section 8).

`exposure_calibration.json` schema (lives on the laser PC, gitignored):

    {
      "units": "microns",
      "reference": { "wafer_um": [0.0, 0.0], "stage_um": { "x": 5590, "y": -18450, "z": 0 } },
      "axes": { "sx": 1, "sy": 1 },
      "mirror": { "x": true, "y": false },
      "global_offset_um": [0.0, 0.0],
      "per_array_offset_um": { "r00c00": [0.0, 0.0] },
      "travel_um": [126000, 76000],
      "stage_y_max_um": 6950
    }

Default mapping (see NOTE below): stage_X = 5590 - wafer_X, stage_Y = -18450 + wafer_Y,
reproducing Singulation's solved P1..P4 fit as an UNVERIFIED starting point only. Every
calibration number here is a starting point -- re-measure axis signs, mirror, offsets,
and the stage-Y ceiling on THIS rig before trusting an exposure (section 8).

NOTE on the section-6 example values: that example lists ``axes.sx = -1`` together with
``mirror.x = true``. Those two compose (mirror flips wx, then axes.sx multiplies) to
stage_X = 5590 + wafer_X -- the *opposite* of the documented target 5590 - wafer_X. The
backside X-mirror is the physically-documented default (sections 2 and 8) and section 6
itself warns "the signs inverted from the first geometric guess", so the consistent
resolution is ``mirror.x = true`` with ``axes.sx = +1`` (net -1 on wafer_X). That is what
`default_calibration()` uses so the stated default mapping actually holds.

CLI:  python -m laser_pc.transform --selftest      # pure-python asserts, no hardware
      python laser_pc/transform.py --selftest
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

# --------------------------------------------------------------------------- constants
# ES111 stage travel (controller STAGE report): X = 126 mm, Y = 76 mm.
TRAVEL_X_UM = 126_000
TRAVEL_Y_UM = 76_000
# Stage-Y ceiling: the Y the stage reaches at Singulation's far row (its P3/P4). Targets
# above it are refused. Default +6950 um -- RE-MEASURE on this rig (section 2.1 / 8).
STAGE_Y_MAX_UM = 6950

# Singulation's solved reference (starting point ONLY, section 6):
#   stage_X = 5590 - wafer_X ;  stage_Y = -18450 + wafer_Y
_DEFAULT_REF_STAGE_X = 5590
_DEFAULT_REF_STAGE_Y = -18450


def default_calibration_dict() -> dict:
    """Fresh default `exposure_calibration.json` content (see module docstring / section 6)."""
    return {
        "units": "microns",
        "reference": {
            "wafer_um": [0.0, 0.0],
            "stage_um": {"x": _DEFAULT_REF_STAGE_X, "y": _DEFAULT_REF_STAGE_Y, "z": 0},
        },
        # Backside X-mirror on by default (sections 2 / 8). axes.sx = +1 so the net map is
        # stage_X = 5590 - wafer_X (see module NOTE re: the section-6 example's sx = -1).
        "axes": {"sx": 1, "sy": 1},
        "mirror": {"x": True, "y": False},
        "global_offset_um": [0.0, 0.0],
        "per_array_offset_um": {},
        "travel_um": [TRAVEL_X_UM, TRAVEL_Y_UM],
        "stage_y_max_um": STAGE_Y_MAX_UM,
    }


# --------------------------------------------------------------------------- calibration
class Calibration:
    """Structured, attribute-accessible view of `exposure_calibration.json`.

    Attribute access mirrors the section-6 pseudocode (``cal.mirror.x``, ``cal.axes.sx``,
    ``cal.reference.stage.x``, ``cal.global_offset.x`` ...). ``to_dict()`` round-trips back
    to the on-disk schema so teach/edit helpers can persist changes.
    """

    def __init__(self, data: dict):
        self.units = data.get("units", "microns")

        ref = data.get("reference", {})
        wafer_um = ref.get("wafer_um", [0.0, 0.0])
        stage = ref.get("stage_um", {})
        self.reference = SimpleNamespace(
            wafer_um=[float(wafer_um[0]), float(wafer_um[1])],
            stage=SimpleNamespace(
                x=float(stage.get("x", 0.0)),
                y=float(stage.get("y", 0.0)),
                z=float(stage.get("z", 0.0)),
            ),
        )

        axes = data.get("axes", {})
        # Signs are +/-1; coerce to +1 or -1 (0 -> +1) so composition is well-defined.
        self.axes = SimpleNamespace(
            sx=_sign1(axes.get("sx", 1)),
            sy=_sign1(axes.get("sy", 1)),
        )

        mir = data.get("mirror", {})
        self.mirror = SimpleNamespace(x=bool(mir.get("x", False)), y=bool(mir.get("y", False)))

        go = data.get("global_offset_um", [0.0, 0.0])
        self.global_offset = SimpleNamespace(x=float(go[0]), y=float(go[1]))

        pao = data.get("per_array_offset_um", {}) or {}
        self.per_array_offset_um = {
            str(k): [float(v[0]), float(v[1])] for k, v in pao.items()
        }

        tv = data.get("travel_um", [TRAVEL_X_UM, TRAVEL_Y_UM])
        self.travel_um = [float(tv[0]), float(tv[1])]

        self.stage_y_max_um = float(data.get("stage_y_max_um", STAGE_Y_MAX_UM))

        # Optional EXPLICIT reachable window (absolute stage um). Use this when the usable
        # travel is not symmetric about 0 -- e.g. a re-datumed rig whose left/right hard
        # stops and Y stops don't straddle the origin. When present it OVERRIDES the
        # |x|<=travel_x / -travel_y<=y<=stage_y_max model in check_reachable/stage_targets.
        # Keys: x_min, x_max, y_min, y_max (any omitted key falls back to the old model bound).
        r = data.get("reachable_um")
        if isinstance(r, dict):
            self.reachable = SimpleNamespace(
                x_min=float(r.get("x_min", -self.travel_um[0])),
                x_max=float(r.get("x_max", self.travel_um[0])),
                y_min=float(r.get("y_min", -self.travel_um[1])),
                y_max=float(r.get("y_max", self.stage_y_max_um)),
            )
        else:
            self.reachable = None

    def is_reachable(self, sx, sy) -> bool:
        """True if absolute stage target (sx, sy) um is inside the usable travel. Uses the
        explicit reachable window if the calibration defines one, else the legacy symmetric
        model (|x|<=travel_x and -travel_y<=y<=stage_y_max_um)."""
        r = self.reachable
        if r is not None:
            return r.x_min <= sx <= r.x_max and r.y_min <= sy <= r.y_max
        return (abs(sx) <= self.travel_um[0]
                and -self.travel_um[1] <= sy <= self.stage_y_max_um)

    def reach_bounds(self):
        """(x_min, x_max, y_min, y_max) of the usable window, for display/logging."""
        r = self.reachable
        if r is not None:
            return (r.x_min, r.x_max, r.y_min, r.y_max)
        return (-self.travel_um[0], self.travel_um[0], -self.travel_um[1], self.stage_y_max_um)

    def to_dict(self) -> dict:
        return {
            "units": self.units,
            "reference": {
                "wafer_um": [self.reference.wafer_um[0], self.reference.wafer_um[1]],
                "stage_um": {
                    "x": self.reference.stage.x,
                    "y": self.reference.stage.y,
                    "z": self.reference.stage.z,
                },
            },
            "axes": {"sx": int(self.axes.sx), "sy": int(self.axes.sy)},
            "mirror": {"x": bool(self.mirror.x), "y": bool(self.mirror.y)},
            "global_offset_um": [self.global_offset.x, self.global_offset.y],
            "per_array_offset_um": {
                k: [v[0], v[1]] for k, v in self.per_array_offset_um.items()
            },
            "travel_um": [self.travel_um[0], self.travel_um[1]],
            "stage_y_max_um": self.stage_y_max_um,
            **({"reachable_um": {
                "x_min": self.reachable.x_min, "x_max": self.reachable.x_max,
                "y_min": self.reachable.y_min, "y_max": self.reachable.y_max,
            }} if self.reachable is not None else {}),
        }

    def nudge_for(self, array_id):
        """Per-array offset (dx, dy) in microns; (0, 0) if none taught for this id."""
        if array_id is None:
            return (0.0, 0.0)
        off = self.per_array_offset_um.get(str(array_id))
        if not off:
            return (0.0, 0.0)
        return (float(off[0]), float(off[1]))


def _sign1(v) -> int:
    """Coerce to +1/-1 (0 or missing -> +1)."""
    try:
        return -1 if float(v) < 0 else 1
    except (TypeError, ValueError):
        return 1


def default_calibration() -> Calibration:
    return Calibration(default_calibration_dict())


def load_calibration(path) -> Calibration:
    """Load `exposure_calibration.json`; if absent, return the default calibration."""
    p = Path(path)
    if not p.exists():
        return default_calibration()
    data = json.loads(p.read_text(encoding="utf-8"))
    return Calibration(data)


def save_calibration(cal: Calibration, path) -> None:
    """Overwrite the calibration file in place (never rmdir -- OneDrive, section 8)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cal.to_dict(), indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- transform
def wafer_to_stage(exposed_um, cal: Calibration, array_id=None):
    """Exposed-frame array center -> absolute stage target (microns).

    ``exposed_um`` is ``exposed_center_um`` from plan.json (already rotated by
    ``design_rotation_deg``). Applies backside mirror, axis signs, the taught reference,
    the global offset, and (if ``array_id`` is given) the per-array nudge. Returns
    ``(stage_x_um, stage_y_um)`` as floats.

    Defaults reproduce Singulation: stage_X = 5590 - wafer_X, stage_Y = -18450 + wafer_Y.
    """
    wx, wy = float(exposed_um[0]), float(exposed_um[1])
    if cal.mirror.x:
        wx = -wx
    if cal.mirror.y:
        wy = -wy

    rx, ry = cal.reference.wafer_um  # mirror the reference point identically
    if cal.mirror.x:
        rx = -rx
    if cal.mirror.y:
        ry = -ry

    nx, ny = cal.nudge_for(array_id)

    sx = cal.reference.stage.x + cal.axes.sx * (wx - rx) + cal.global_offset.x + nx
    sy = cal.reference.stage.y + cal.axes.sy * (wy - ry) + cal.global_offset.y + ny
    return (sx, sy)


# --------------------------------------------------------------------------- reachability
def check_reachable(plan: dict, cal: Calibration):
    """Pre-flight reachability check over every array in ``plan``.

    For each array compute ``wafer_to_stage(exposed_center_um, cal, array_id)`` and require

        |stage_x| <= travel_x   AND   y_floor <= stage_y <= stage_y_max_um

    where ``travel_x``/``travel_y`` come from ``cal.travel_um``, ``y_floor = -travel_y`` and
    the ceiling is ``cal.stage_y_max_um`` (THE P3/P4 CEILING, section 2.1). Returns
    ``(ok, failures)`` where ``failures`` is the list of offending ``array_id`` strings
    (empty when ``ok`` is True). Called by the laser-PC pre-flight (section 7.3).
    """
    failures = []
    for arr in _iter_arrays(plan):
        array_id = arr.get("array_id")
        exposed = arr.get("exposed_center_um")
        if exposed is None:
            failures.append(array_id)
            continue
        sx, sy = wafer_to_stage(exposed, cal, array_id)
        if not cal.is_reachable(sx, sy):
            failures.append(array_id)

    return (len(failures) == 0, failures)


def stage_targets(plan: dict, cal: Calibration):
    """List of ``(array_id, stage_x, stage_y, reachable)`` for every array (for `--list`)."""
    out = []
    for arr in _iter_arrays(plan):
        array_id = arr.get("array_id")
        exposed = arr.get("exposed_center_um")
        if exposed is None:
            out.append((array_id, None, None, False))
            continue
        sx, sy = wafer_to_stage(exposed, cal, array_id)
        out.append((array_id, sx, sy, cal.is_reachable(sx, sy)))
    return out


def _iter_arrays(plan: dict):
    """Yield each array dict from a plan (supports rows[].arrays[] and a flat arrays[])."""
    if not isinstance(plan, dict):
        return
    rows = plan.get("rows")
    if rows:
        for row in rows:
            for arr in row.get("arrays", []):
                yield arr
        return
    for arr in plan.get("arrays", []):
        yield arr


# --------------------------------------------------------------------------- solve
def solve_transform(pairs):
    """Least-squares wafer(exposed) -> stage transform from taught points.

    ``pairs`` is a sequence of ``((exposed_x, exposed_y), (stage_x, stage_y))`` (>= 2). Fits
    an affine map (captures sign + scale + small rotation + translation)::

        stage_x = a*ex + b*ey + tx
        stage_y = c*ex + d*ey + ty

    via hand-rolled normal equations + Gaussian elimination (no numpy). When the geometry
    is rank-deficient for a full affine (e.g. exactly 2 points), falls back to an
    independent per-axis fit (b = c = 0). Returns a dict with the raw coefficients plus a
    decomposition into the ``axes`` signs, per-axis ``scale`` and the small ``rotation_deg``
    so first-time setup can teach two known arrays and auto-derive ``axes`` (section 6).
    """
    pts = [((float(e[0]), float(e[1])), (float(s[0]), float(s[1]))) for (e, s) in pairs]
    n = len(pts)
    if n < 2:
        raise ValueError("solve_transform needs >= 2 taught (exposed, stage) pairs; got %d" % n)

    # Normal-equation accumulators for design row [ex, ey, 1].
    Sxx = Sxy = Sx = Syy = Sy = 0.0
    bx0 = bx1 = bx2 = 0.0  # RHS for stage_x
    by0 = by1 = by2 = 0.0  # RHS for stage_y
    for (ex, ey), (sx, sy) in pts:
        Sxx += ex * ex
        Sxy += ex * ey
        Sx += ex
        Syy += ey * ey
        Sy += ey
        bx0 += ex * sx
        bx1 += ey * sx
        bx2 += sx
        by0 += ex * sy
        by1 += ey * sy
        by2 += sy

    M = [
        [Sxx, Sxy, Sx],
        [Sxy, Syy, Sy],
        [Sx,  Sy,  float(n)],
    ]

    sol_x = _solve3(M, [bx0, bx1, bx2])
    sol_y = _solve3(M, [by0, by1, by2])

    if sol_x is None or sol_y is None:
        # Rank-deficient full affine -> fall back to independent per-axis lines.
        a, tx = _fit_line([e for (e, _s) in pts], [s for (_e, s) in pts], 0, 0)
        d, ty = _fit_line([e for (e, _s) in pts], [s for (_e, s) in pts], 1, 1)
        b = c = 0.0
        method = "diagonal"
    else:
        a, b, tx = sol_x
        c, d, ty = sol_y
        method = "affine"

    # Residual RMS (microns).
    sq = 0.0
    for (ex, ey), (sx, sy) in pts:
        px = a * ex + b * ey + tx
        py = c * ex + d * ey + ty
        sq += (px - sx) ** 2 + (py - sy) ** 2
    rms = math.sqrt(sq / n)

    scale_x = math.hypot(a, c)
    scale_y = math.hypot(b, d)
    rotation_deg = math.degrees(math.atan2(c, a)) if (a or c) else 0.0

    return {
        "a": a, "b": b, "tx": tx,
        "c": c, "d": d, "ty": ty,
        "axes": {"sx": -1 if a < 0 else 1, "sy": -1 if d < 0 else 1},
        "scale_x": scale_x, "scale_y": scale_y,
        "rotation_deg": rotation_deg,
        "rms_residual_um": rms,
        "n_points": n,
        "method": method,
    }


def _fit_line(exposed_list, stage_list, e_idx, s_idx):
    """Simple 1-D least-squares slope/intercept: stage[s_idx] = m*exposed[e_idx] + k."""
    xs = [e[e_idx] for e in exposed_list]
    ys = [s[s_idx] for s in stage_list]
    n = len(xs)
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-12:
        # No spread on this axis -> slope 1, intercept = mean offset (best we can do).
        return (1.0, (sy - sx) / n if n else 0.0)
    m = (n * sxy - sx * sy) / denom
    k = (sy - m * sx) / n
    return (m, k)


def _solve3(M, b):
    """Solve a 3x3 linear system by Gaussian elimination with partial pivoting.

    Returns ``[x0, x1, x2]`` or ``None`` if the matrix is (near-)singular.
    """
    # Work on a copy augmented with b.
    a = [[M[r][0], M[r][1], M[r][2], b[r]] for r in range(3)]
    for col in range(3):
        # Partial pivot: largest magnitude in this column at/below the diagonal.
        piv = col
        best = abs(a[col][col])
        for r in range(col + 1, 3):
            if abs(a[r][col]) > best:
                best = abs(a[r][col])
                piv = r
        if best < 1e-9:
            return None
        if piv != col:
            a[col], a[piv] = a[piv], a[col]
        pivot = a[col][col]
        for r in range(3):
            if r == col:
                continue
            factor = a[r][col] / pivot
            if factor == 0.0:
                continue
            for k in range(col, 4):
                a[r][k] -= factor * a[col][k]
    return [a[0][3] / a[0][0], a[1][3] / a[1][1], a[2][3] / a[2][2]]


# --------------------------------------------------------------------------- teach
def teach_reference(optiscan, cal=None, wafer_um=(0.0, 0.0), out_path=None, step_um=1000):
    """Interactively jog the stage and record ``reference.stage_um`` (section 6 / 7.1).

    ``optiscan`` is a connected ``OptiScan`` device (from ``laser_pc.optiscan``). This adapts
    the jog loop from ``optiscan.cmd_jog`` but records ONE point -- the reference -- into the
    calibration instead of the four P1..P4 stations. ``wafer_um`` is the exposed-frame
    coordinate that the jogged stage position corresponds to (default the wafer/design
    origin ``(0, 0)``, i.e. the field-center array). The device and ``msvcrt`` are the only
    hardware touchpoints and both are used/imported lazily so this module imports anywhere.

    Returns the updated ``Calibration``; if ``out_path`` is given the file is overwritten
    in place (never rmdir -- OneDrive, section 8).
    """
    try:
        import msvcrt  # Windows-only; the laser PC is Windows.
    except ImportError as exc:
        raise SystemExit("teach_reference needs Windows (msvcrt) for interactive jog.") from exc

    if cal is None:
        cal = default_calibration()

    step = int(step_um)
    recorded = False
    print(
        "\nTEACH REFERENCE  (small, watched moves -- keep a hand on the controller)\n"
        "  a/d = -X/+X   s/w = -Y/+Y   f/r = focus -/+\n"
        "  [ / ] = smaller/larger step        p = print position\n"
        "  SPACE or ENTER = record current stage position as the reference\n"
        "  q = save + quit                     Esc = quit without saving\n"
        "  reference wafer coordinate = (%g, %g) um\n" % (wafer_um[0], wafer_um[1])
    )
    while True:
        x, y = optiscan.stage_position()
        sys.stdout.write("\rX=%-8d Y=%-8d  step=%-6d  recorded=%s        "
                         % (x, y, step, "yes" if recorded else "no"))
        sys.stdout.flush()
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):  # arrow-key prefix -> map to a/d/w/s
            ch2 = msvcrt.getch()
            ch = {b"K": b"a", b"M": b"d", b"H": b"w", b"P": b"s"}.get(ch2, b"")
        key = ch.decode("ascii", "ignore").lower()
        if key == "q":
            break
        if ch == b"\x1b":  # Esc
            print("\nquit without saving")
            return cal
        if key == "d":
            optiscan.move_rel(step, 0)
        elif key == "a":
            optiscan.move_rel(-step, 0)
        elif key == "w":
            optiscan.move_rel(0, step)
        elif key == "s":
            optiscan.move_rel(0, -step)
        elif key == "r":
            optiscan.move_rel_z(step)
        elif key == "f":
            optiscan.move_rel_z(-step)
        elif key == "]":
            step = min(step * 2, 20_000)
        elif key == "[":
            step = max(step // 2, 1)
        elif key == "p":
            z = None
            try:
                z = optiscan.z_position()
            except Exception:
                pass
            print("\n  X=%d Y=%d%s" % (x, y, "" if z is None else " Z=%d" % z))
        elif ch in (b" ", b"\r", b"\n"):
            z = 0
            try:
                z = optiscan.z_position()
            except Exception:
                pass
            cal.reference.wafer_um = [float(wafer_um[0]), float(wafer_um[1])]
            cal.reference.stage.x = float(x)
            cal.reference.stage.y = float(y)
            cal.reference.stage.z = float(z)
            recorded = True
            print("\n  recorded reference: wafer=(%g,%g) -> stage X=%d Y=%d Z=%d"
                  % (wafer_um[0], wafer_um[1], x, y, z))

    if recorded and out_path is not None:
        save_calibration(cal, out_path)
        print("\nwrote reference to %s" % out_path)
    elif not recorded:
        print("\nno reference recorded")
    return cal


# --------------------------------------------------------------------------- self-test
def _known_array_centers_um():
    """The 10 PFLM v2 array centers in the EXPOSED frame (design/build_wafer_v2.py).

    v2 is a 10-cell STAGGERED layout (top 3 / middle 4 / bottom 3), cells 10.5 x 38.7 mm,
    authored directly in the exposed frame (NO +90 bake). Column pitch = cell_w + kerf
    (10500 + 200 um); top/bottom row centers at y = +/-(cell_h/2 + kerf/2) = +/-19450 um.
    Returns [(row_index, col_index, (x_um, y_um)), ...] with row 0 = top, 1 = middle, 2 = bottom.
    """
    col_pitch = 10500.0 + 200.0          # cell width + kerf
    row_off = 38700.0 / 2.0 + 200.0 / 2.0  # cell height/2 + kerf/2 = 19450 um
    row_kx = {0: [-2, 0, 2], 1: [-3, -1, 1, 3], 2: [-2, 0, 2]}
    row_y = {0: +row_off, 1: 0.0, 2: -row_off}
    centers = []
    for ri in (0, 1, 2):
        for ci, kx in enumerate(row_kx[ri]):
            centers.append((ri, ci, (kx * col_pitch, row_y[ri])))
    return centers


def _definitive_cal():
    """The ported/confirmed exposure calibration (for the self-test): taught reference
    (84355, -19056), backside X-mirror, and the asymmetric pipe-limited reachable window."""
    return Calibration({
        "units": "microns",
        "reference": {"wafer_um": [0.0, 0.0], "stage_um": {"x": 84355.0, "y": -19056.0, "z": 0}},
        "axes": {"sx": 1, "sy": 1}, "mirror": {"x": True, "y": False},
        "global_offset_um": [0.0, 0.0], "per_array_offset_um": {},
        "reachable_um": {"x_min": 16236.0, "x_max": 138529.0, "y_min": -38140.0, "y_max": 0.0},
    })


def _rotate90(pt):
    """Exact +90 deg rotation about the wafer origin: (x, y) -> (-y, x) (section 5.2)."""
    x, y = pt
    return (-y, x)


def _make_plan_from_centers(centers, rotate=True):
    """Build a minimal plan dict (rows[].arrays[].exposed_center_um) from array centers."""
    rows_map = {}
    for row_index, col_index, center in centers:
        exposed = _rotate90(center) if rotate else (float(center[0]), float(center[1]))
        aid = "r%02dc%02d" % (row_index, col_index)
        rows_map.setdefault(row_index, []).append({
            "array_id": aid,
            "row_index": row_index,
            "col_index": col_index,
            "bbox_center_um": [center[0], center[1]],
            "exposed_center_um": [exposed[0], exposed[1]],
        })
    rows = [{"row_index": ri, "arrays": rows_map[ri]} for ri in sorted(rows_map)]
    return {"rows": rows}


def _selftest() -> int:
    print("=== transform.py self-test (pure python, no hardware) ===\n")
    cal = default_calibration()

    # --- 0. defaults reproduce the Singulation mapping ------------------------------
    sx, sy = wafer_to_stage((0.0, 0.0), cal)
    assert abs(sx - 5590.0) < 1e-9 and abs(sy - (-18450.0)) < 1e-9, (sx, sy)
    sx, sy = wafer_to_stage((1000.0, 2000.0), cal)  # stage_X=5590-wx, stage_Y=-18450+wy
    assert abs(sx - (5590.0 - 1000.0)) < 1e-9, sx
    assert abs(sy - (-18450.0 + 2000.0)) < 1e-9, sy
    print("[ok] defaults reproduce stage_X = 5590 - wafer_X, stage_Y = -18450 + wafer_Y")
    print("     wafer_to_stage((0,0))          = (%.1f, %.1f)" % wafer_to_stage((0.0, 0.0), cal))
    print("     wafer_to_stage((1000,2000))    = (%.1f, %.1f)" % wafer_to_stage((1000.0, 2000.0), cal))

    # --- 1. v2 layout vs the DEFINITIVE asymmetric reachable window -----------------
    # v2 is authored in the exposed frame (no +90 bake). Against the pipe-limited window its
    # top/bottom row centers overflow by ~400 um (v2 is ~0.4 mm too tall on each side).
    dcal = _definitive_cal()
    centers = _known_array_centers_um()
    plan_v2 = _make_plan_from_centers(centers, rotate=False)
    ok, failures = check_reachable(plan_v2, dcal)
    tgts = stage_targets(plan_v2, dcal)
    max_sy = max(t[2] for t in tgts)
    min_sy = min(t[2] for t in tgts)
    _rx0, _rx1, _ry0, _ry1 = dcal.reach_bounds()
    assert not ok, "v2 top/bottom rows should fall OUTSIDE the pipe-limited window"
    assert max_sy > _ry1 and min_sy < _ry0, (min_sy, max_sy, _ry0, _ry1)
    assert len(failures) == 6, ("expected top+bottom (6 arrays) unreachable", failures)
    print("\n[ok] v2 (10-cell, exposed frame) vs window X[%.0f,%.0f] Y[%.0f,%.0f]:"
          % (_rx0, _rx1, _ry0, _ry1))
    print("     stage_Y span [%+.0f, %+.0f] um -> %d/%d array centers OUTSIDE (top/bottom overflow "
          "the %+.0f pipe floor / %+.0f front limit by ~%.0f um)"
          % (min_sy, max_sy, len(failures), len(tgts), _ry0, _ry1,
             max(max_sy - _ry1, _ry0 - min_sy)))
    print("     -> the PREP clamps each center into the window and bakes the ~%.0f um residual as a "
          "galvo field offset (<= the 2.5 mm array tolerance), so v2 still exposes at true locations."
          % max(max_sy - _ry1, _ry0 - min_sy))

    # --- 2. the v2 MIDDLE row alone is fully reachable ------------------------------
    mid = [c for c in centers if c[0] == 1]
    plan_mid = _make_plan_from_centers(mid, rotate=False)
    ok_mid, fail_mid = check_reachable(plan_mid, dcal)
    assert ok_mid, ("v2 middle row should be reachable", fail_mid)
    print("\n[ok] v2 middle row (%d arrays) is fully inside the window" % len(mid))

    # --- 3. solve_transform round-trip on synthetic pairs ---------------------------
    exposed_pts = [(0.0, 0.0), (10000.0, 0.0), (0.0, 10000.0),
                   (10000.0, 10000.0), (-5000.0, 7000.0)]
    pairs = [(e, wafer_to_stage(e, cal)) for e in exposed_pts]
    sol = solve_transform(pairs)
    assert sol["rms_residual_um"] < 1e-6, sol["rms_residual_um"]
    assert abs(sol["a"] - (-1.0)) < 1e-9, sol["a"]     # d(stage_x)/d(ex) = -1
    assert abs(sol["b"] - 0.0) < 1e-9, sol["b"]
    assert abs(sol["c"] - 0.0) < 1e-9, sol["c"]
    assert abs(sol["d"] - 1.0) < 1e-9, sol["d"]        # d(stage_y)/d(ey) = +1
    assert abs(sol["tx"] - 5590.0) < 1e-6, sol["tx"]
    assert abs(sol["ty"] - (-18450.0)) < 1e-6, sol["ty"]
    assert sol["axes"] == {"sx": -1, "sy": 1}, sol["axes"]
    # apply solved map back and confirm it reproduces every stage point
    for (ex, ey), (tx, ty) in pairs:
        px = sol["a"] * ex + sol["b"] * ey + sol["tx"]
        py = sol["c"] * ex + sol["d"] * ey + sol["ty"]
        assert abs(px - tx) < 1e-6 and abs(py - ty) < 1e-6, ((ex, ey), (px, py), (tx, ty))
    print("\n[ok] solve_transform round-trip: rms=%.3e um  net axes=%s  rotation=%.4f deg"
          % (sol["rms_residual_um"], sol["axes"], sol["rotation_deg"]))
    print("     recovered a=%.6f b=%.6f tx=%.3f | c=%.6f d=%.6f ty=%.3f (%s, n=%d)"
          % (sol["a"], sol["b"], sol["tx"], sol["c"], sol["d"], sol["ty"],
             sol["method"], sol["n_points"]))

    # --- 3b. two-point (rank-deficient) fallback still fits --------------------------
    sol2 = solve_transform([((0.0, 0.0), (5590.0, -18450.0)),
                            ((10000.0, 20000.0), (5590.0 - 10000.0, -18450.0 + 20000.0))])
    assert sol2["rms_residual_um"] < 1e-6, sol2["rms_residual_um"]
    assert sol2["method"] == "diagonal", sol2["method"]
    print("     two-point fallback: method=%s rms=%.3e um axes=%s"
          % (sol2["method"], sol2["rms_residual_um"], sol2["axes"]))

    # --- 4. calibration JSON round-trips ---------------------------------------------
    rt = Calibration(cal.to_dict())
    assert wafer_to_stage((1234.0, -567.0), rt) == wafer_to_stage((1234.0, -567.0), cal)
    print("\n[ok] Calibration <-> dict round-trip preserves the transform")

    print("\n=== ALL SELF-TESTS PASSED ===")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Wafer->stage transform (laser PC, pure math).")
    p.add_argument("--selftest", action="store_true", help="run pure-python self-test")
    p.add_argument("--emit-default", metavar="PATH",
                   help="write a default exposure_calibration.json to PATH")
    args = p.parse_args(argv)
    if args.emit_default:
        save_calibration(default_calibration(), args.emit_default)
        print("wrote default calibration to %s" % args.emit_default)
        return 0
    # default action is the self-test (safe: no hardware)
    return _selftest()


if __name__ == "__main__":
    raise SystemExit(main())
