"""PySide6 + QML front end for the PFLM exposure-set builder.

    python prep_app/prep_app.py [file.gds]

Pick the pinfin, bbox and align layers, the design rotation and the within-row
mask stride, watch the arrays tile the wafer in the *as-exposed* orientation with
their exposure order and mask phases, then build the set folder.

All geometry lives in the ``pflm`` package (``pflm.layers`` / ``pflm.arrays`` /
``pflm.plan``); this file is presentation and plumbing only, and the preview
calls those same engine functions so it can never drift from the real build.

Named datasets are kept in ``prep_app/.ui_datasets.json`` -- ``{name: {settings}}``,
the same shape the Singulation slicer UI uses.

Adapted from ``UV Laser Singulation/slicing/slicer_app.py``.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import PySide6

# The QML plugins live in PySide6/qml/... and link against the Qt6*.dll files in
# the package root. Windows will not search that root for a DLL loaded from a
# nested directory, so without this the QtQuick.Controls style plugins fail with
# "The specified module could not be found". Must happen before Qt loads plugins.
_PYSIDE_DIR = str(Path(PySide6.__file__).parent)
os.environ["PATH"] = _PYSIDE_DIR + os.pathsep + os.environ.get("PATH", "")
try:
    os.add_dll_directory(_PYSIDE_DIR)
except OSError:
    pass

from PySide6.QtCore import (Property, QAbstractListModel, QByteArray, QModelIndex,  # noqa: E402
                            QObject, QPointF, QProcess, QRectF, Qt, QThread, QTimer,
                            QUrl, Signal, Slot)
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QPainter,  # noqa: E402
                           QPainterPath, QPen, QPolygonF)
from PySide6.QtQml import QmlElement, QQmlApplicationEngine  # noqa: E402
from PySide6.QtQuick import QQuickPaintedItem  # noqa: E402
from PySide6.QtQuickControls2 import QQuickStyle  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
# The pflm package lives at the repo root; make `from pflm import ...` resolve
# whether the app is launched from the repo root or from prep_app/.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The engine functions the preview and the layer combos share with the CLI build.
# Imported eagerly so the app interlocks with the real pflm package; if pflm (or
# its klayout dependency) is not installed yet the app still starts and reports it.
PFLM_IMPORT_ERROR = ""
try:
    from pflm import layers as pflm_layers  # noqa: E402
    from pflm import arrays as pflm_arrays  # noqa: E402
    from pflm import plan as pflm_plan  # noqa: E402
    from pflm import centering as pflm_centering  # noqa: E402
except Exception as exc:  # noqa: BLE001 - surfaced in the UI status line
    pflm_layers = pflm_arrays = pflm_plan = pflm_centering = None
    PFLM_IMPORT_ERROR = str(exc)

QML_IMPORT_NAME = "PflmPrep"
QML_IMPORT_MAJOR_VERSION = 1

DATASETS_JSON = HERE / ".ui_datasets.json"

# ------------------------------------------------------------------ constants
# Mirror the ARCHITECTURE.md contract; the real values still come from pflm at
# build time, these are only the preview's field/stage frame.
WAFER_RADIUS_MM = 50.0
USABLE_HALF_UM = 30000        # +/-30 mm usable field
QUALIFIED_UM = 54000
FULL_UM = 78485
TRAVEL_UM = (126000, 76000)   # X, Y stage travel
STAGE_Y_MAX_UM = 6950         # the P3/P4 ceiling

PHASE_COLORS = {0: "#4cc2ff", 1: "#ffb951"}   # A = blue, B = orange
PHASE_LABEL = {0: "A", 1: "B"}
NO_GEOM_COLOR = "#ff99a4"
ALIGN_COLOR = "#c58cff"       # violet -- alignment marks (final phase)
DEADSPACE_COLOR = "#d9a066"   # amber -- dead-space ablation (first phase)
SELECT_COLOR = "#ffffff"
FIELD_COLOR = "#6ccb5f"
GUIDE_COLOR = "#7a7a7a"
SEAM_COLOR = "#ff6b6b"
TEXT_2 = "#c5c5c5"
TEXT_3 = "#8a8a8a"
SURFACE = "#191919"
FACE = "Segoe UI Variable Text"

# Every field the UI remembers per dataset.
DATASET_FIELDS = ("input", "output", "pinfin", "bbox", "align",
                  "rotation", "withinRowStride", "backside",
                  "globalX", "globalY", "ablateDeadSpace")


def _today_mmddyy() -> str:
    """Today as MMDDYY, matching the repo's file-naming convention."""
    return datetime.date.today().strftime("%m%d%y")


def _dated_name(stem: str) -> str:
    """Give a set name today's date: swap a leading MMDDYY token if present,
    else prepend one."""
    if re.match(r"^\d{6}(_|$)", stem):
        return _today_mmddyy() + stem[6:]
    return f"{_today_mmddyy()}_{stem}"


def _find_manifest(gds_path: Path):
    """The design manifest (per-array etch params: passes + crosshatch angles)
    that goes with a GDS, by build_wafer's naming convention. Prefer the exact
    ``<stem>_manifest.csv`` next to the GDS; otherwise, if the folder holds exactly
    one ``*_manifest.csv``, use it. Returns a Path, or None if absent/ambiguous.

    Without this, every array falls back to the default 0/90 crosshatch (hex must
    be -30/+30) and carries no per-array pass count -- so the caller warns loudly."""
    exact = gds_path.with_name(gds_path.stem + "_manifest.csv")
    if exact.is_file():
        return exact
    candidates = sorted(gds_path.parent.glob("*_manifest.csv"))
    return candidates[0] if len(candidates) == 1 else None


# --------------------------------------------------------------------- models


class LayerModel(QAbstractListModel):
    """Layers found in the source GDS, shared by the pinfin/bbox/align combos."""

    SelectorRole = Qt.UserRole + 1
    LabelRole = Qt.UserRole + 2
    DetailRole = Qt.UserRole + 3

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict] = []

    def roleNames(self) -> dict:
        return {
            self.SelectorRole: QByteArray(b"selector"),
            self.LabelRole: QByteArray(b"label"),
            self.DetailRole: QByteArray(b"detail"),
        }

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: B008
        return len(self._rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        return {
            self.SelectorRole: row["selector"],
            self.LabelRole: row["label"],
            self.DetailRole: row["detail"],
        }.get(role)

    def set_layers(self, entries) -> None:
        self.beginResetModel()
        self._rows = []
        for entry in entries:
            bits = []
            if entry.polygons:
                bits.append(f"{entry.polygons} polygon{'s' if entry.polygons != 1 else ''}")
            if entry.paths:
                widths = ", ".join(f"{w:g}" for w in entry.widths_um[:3])
                bits.append(f"{entry.paths} path{'s' if entry.paths != 1 else ''} at {widths} um")
            if not bits:
                bits.append("empty")
            self._rows.append({
                "selector": entry.selector,
                "label": entry.name or f"{entry.layer}/{entry.datatype}",
                "detail": f"{'  ·  '.join(bits)}   ·   {entry.area_mm2:.3f} mm²",
            })
        self.endResetModel()

    def selector_at(self, row: int) -> str:
        return self._rows[row]["selector"] if 0 <= row < len(self._rows) else ""

    def row_of(self, selector: str) -> int:
        for index, row in enumerate(self._rows):
            if row["selector"] == selector:
                return index
        return -1


# ------------------------------------------------------------- preview engine


@dataclass
class ArrayView:
    """One array, ready to draw -- geometry from pflm, order/phase from pflm."""

    array_id: str
    row_index: int
    col_index: int
    step: int                       # 1..N exposure order (expose steps only)
    phase: int                      # 0 = A, 1 = B
    exposed_corners_mm: list        # 4 (x, y) corners, rotated into exposed frame
    exposed_center_mm: tuple        # (x, y) center after rotation
    exposed_center_um: tuple
    bbox_center_um: tuple
    exposed_w_mm: float
    exposed_h_mm: float
    polygon_count: int
    has_geometry: bool
    fits_field: bool
    # Alignment-mark extras (pinfin arrays keep these defaults).
    is_align: bool = False
    align_index: int = 0
    field_offset_mm: tuple = (0.0, 0.0)
    passes: int | None = None
    is_deadspace: bool = False
    ds_index: int = 0


@dataclass
class PflmPreview:
    arrays: list                    # ArrayView, in exposure order
    schedule: list                  # full schedule incl. mask steps (dicts)
    design_rotation_deg: int
    rotation_is_auto: bool
    auto_rotation_deg: int
    wafer_radius_mm: float
    usable_half_mm: float
    feasible: bool
    row_stack_axis: str
    required_row_span_mm: float
    required_withinrow_span_mm: float
    travel_mm: tuple
    stage_y_max_um: float
    within_row_stride: int
    n_masks: int
    n_align_marks: int = 0
    align_tol_mm: float = 25.0
    n_deadspace: int = 0
    notes: list = field(default_factory=list)


def _load_layout(path: Path):
    """Read a GDS/OAS/DXF into a klayout layout. Kept alive by the caller while
    its Regions are used (KLayout iterator-lifetime gotcha)."""
    import klayout.db as pya
    layout = pya.Layout()
    if path.suffix.lower() == ".dxf":
        opts = pya.LoadLayoutOptions()
        opts.dxf_unit = 1000.0
        layout.read(str(path), opts)
    else:
        layout.read(str(path))
    return layout


def build_pflm_preview(*, input_path, pinfin_spec, bbox_spec, align_spec,
                       rotation, within_row_stride,
                       travel_um=TRAVEL_UM, stage_y_max_um=STAGE_Y_MAX_UM,
                       usable_half_um=USABLE_HALF_UM, row_tol_um=None,
                       align_tol_um=None, ablate_dead_space=False,
                       cell_spec="4/0") -> PflmPreview:
    """Build the preview by calling the pflm engine directly: detect arrays,
    group into rows top->bottom, choose/apply the design rotation, and expand
    the mask schedule. Returns a draw-ready, klayout-free structure."""
    if pflm_arrays is None:
        raise RuntimeError(PFLM_IMPORT_ERROR or "pflm package is not importable")

    notes: list[str] = []
    layout = _load_layout(Path(input_path))

    boxes = list(pflm_arrays.detect_arrays(layout, bbox_spec))
    if not boxes:
        notes.append(f"No shapes on bbox layer '{bbox_spec}'; "
                     f"falling back to clustering pinfins on '{pinfin_spec}'.")
        boxes = list(pflm_arrays.detect_arrays_from_pinfins(layout, pinfin_spec))
    if not boxes:
        raise RuntimeError("No arrays detected on the bbox or pinfin layers.")

    # Rotation is chosen from the boxes (extent-based); a forced rotation is
    # re-checked with rotation_feasibility (contract 2.1).
    choice = pflm_arrays.choose_rotation(boxes, travel_um=travel_um,
                                         stage_y_max_um=stage_y_max_um)
    auto_deg = int(choice.get("deg", 0))

    rotation_is_auto = (str(rotation).lower() == "auto")
    deg = auto_deg if rotation_is_auto else (int(rotation) % 360)
    feas = (choice if (rotation_is_auto or deg == auto_deg)
            else pflm_arrays.rotation_feasibility(boxes, deg, travel_um=travel_um,
                                                  stage_y_max_um=stage_y_max_um))
    feasible = bool(feas.get("feasible", False))
    row_axis = str(feas.get("row_advance_axis", "stage_y"))
    sweep_um = float(feas.get("sweep_span_um", 0.0))          # within a row (stage-X)
    advance_um = float(feas.get("row_advance_span_um", 0.0))  # between rows (stage-Y)

    # Physical rows = exposed-Y bands, top->bottom; arrays left->right within a row.
    # Grouping AFTER rotation is what stops rows being mixed (contract 2.1/2.2).
    rows = list(pflm_arrays.group_exposed_rows(boxes, deg, row_tol_um))

    if not feasible:
        notes.append("Stage-INFEASIBLE at this rotation: the run will be refused "
                     "at the laser-PC pre-flight. Rotation that fits: "
                     f"{auto_deg}°.")

    # Reconstruct array_id -> box independent of build_schedule internals.
    id_to_box: dict[str, tuple] = {}
    for row in rows:
        for col, box in enumerate(row.arrays):
            aid = f"r{row.row_index:02d}c{col:02d}"
            id_to_box[aid] = (row.row_index, col, box)

    schedule = list(pflm_plan.build_schedule(rows, within_row_stride))

    half_um = float(usable_half_um)
    arrays: list[ArrayView] = []
    expose_seen = 0
    empty_ids: list[str] = []
    for entry in schedule:
        if entry.get("action") != "expose":
            continue
        expose_seen += 1
        aid = entry["array_id"]
        row_index, col, box = id_to_box[aid]
        l, b, r, t = box.bbox_um
        cx, cy = box.center_um

        count = pflm_arrays.count_pinfins_in(layout, pinfin_spec, box.bbox_um)
        has_geom = count > 0
        if not has_geom:
            empty_ids.append(aid)

        corners_um = [pflm_arrays.rotate_point_um((l, b), deg),
                      pflm_arrays.rotate_point_um((r, b), deg),
                      pflm_arrays.rotate_point_um((r, t), deg),
                      pflm_arrays.rotate_point_um((l, t), deg)]
        corners_mm = [(x / 1000.0, y / 1000.0) for x, y in corners_um]
        ex, ey = pflm_arrays.rotate_point_um((cx, cy), deg)

        xs = [x for x, _ in corners_um]
        ys = [y for _, y in corners_um]
        w_um = max(xs) - min(xs)
        h_um = max(ys) - min(ys)
        fits = (w_um <= 2 * half_um) and (h_um <= 2 * half_um)

        arrays.append(ArrayView(
            array_id=aid, row_index=row_index, col_index=col,
            step=expose_seen, phase=int(entry.get("phase", 0)),
            exposed_corners_mm=corners_mm,
            exposed_center_mm=(ex / 1000.0, ey / 1000.0),
            exposed_center_um=(ex, ey), bbox_center_um=(cx, cy),
            exposed_w_mm=w_um / 1000.0, exposed_h_mm=h_um / 1000.0,
            polygon_count=count, has_geometry=has_geom, fits_field=fits,
        ))

    n_masks = sum(1 for s in schedule if s.get("action") == "mask")
    if empty_ids:
        notes.append(f"{len(empty_ids)} array(s) have NO pinfin geometry "
                     f"({', '.join(empty_ids[:6])}"
                     f"{'…' if len(empty_ids) > 6 else ''}) -- placed but empty.")
    bad_fit = [a.array_id for a in arrays if not a.fits_field]
    if bad_fit:
        notes.append(f"{len(bad_fit)} array(s) exceed the ±{half_um/1000:g} mm "
                     f"usable field ({', '.join(bad_fit[:6])}).")

    # ---- FINAL PHASE: alignment marks (mirror pflm.plan.build_set) ------------
    # Etched last, after a wash/mask pause. Each mark centers on the closest
    # reachable point (clamp to the stage envelope) and lands off-center in the
    # field by `off`, exposed at its true wafer location. Appended to the schedule
    # + arrays so the preview's step list matches the built plan.json step-for-step.
    tol_um = float(align_tol_um) if align_tol_um is not None else float(pflm_arrays.ALIGN_TOL_UM)
    n_align = 0
    if align_spec:
        try:
            marks = list(pflm_arrays.detect_align_marks(layout, align_spec))
        except Exception as exc:  # noqa: BLE001 - surfaced as a preview note
            marks = []
            notes.append(f"Alignment layer '{align_spec}' not read: {exc}")
        align_row_index = len(rows)
        if marks and any(s.get("action") == "expose" for s in schedule):
            schedule.append({"step": len(schedule), "action": "mask",
                             "label": "pinfins complete -- wash + mask, then etch alignment marks"})
        unreachable: list[str] = []
        for mi, mb in enumerate(marks):
            exposed_mark = pflm_arrays.rotate_point_um(mb.center_um, deg)
            eff, off = pflm_arrays.clamp_center(exposed_mark, travel_um=tuple(travel_um),
                                                stage_y_max_um=stage_y_max_um)
            l, b, r, t = mb.bbox_um
            corners_um = [pflm_arrays.rotate_point_um((l, b), deg),
                          pflm_arrays.rotate_point_um((r, b), deg),
                          pflm_arrays.rotate_point_um((r, t), deg),
                          pflm_arrays.rotate_point_um((l, t), deg)]
            xs = [x for x, _ in corners_um]
            ys = [y for _, y in corners_um]
            within = max(abs(off[0]), abs(off[1])) <= tol_um
            expose_seen += 1
            aid = "align%02d" % mi
            arrays.append(ArrayView(
                array_id=aid, row_index=align_row_index, col_index=mi,
                step=expose_seen, phase=0,
                exposed_corners_mm=[(x / 1000.0, y / 1000.0) for x, y in corners_um],
                exposed_center_mm=(exposed_mark[0] / 1000.0, exposed_mark[1] / 1000.0),
                exposed_center_um=(exposed_mark[0], exposed_mark[1]),
                bbox_center_um=(mb.center_um[0], mb.center_um[1]),
                exposed_w_mm=(max(xs) - min(xs)) / 1000.0,
                exposed_h_mm=(max(ys) - min(ys)) / 1000.0,
                polygon_count=0, has_geometry=True, fits_field=within,
                is_align=True, align_index=mi,
                field_offset_mm=(off[0] / 1000.0, off[1] / 1000.0),
                passes=int(pflm_plan.ALIGN_ETCH["passes"]),
            ))
            schedule.append({"step": len(schedule), "action": "expose", "array_id": aid,
                             "row_index": align_row_index, "phase": 0,
                             "passes": int(pflm_plan.ALIGN_ETCH["passes"])})
            n_align += 1
            if not within:
                unreachable.append(aid)
        if marks:
            notes.append(f"Alignment marks: {n_align} mark(s), "
                         f"{pflm_plan.ALIGN_ETCH['passes']} passes, etched last "
                         f"(within ±{tol_um/1000:g} mm of field center).")
        if unreachable:
            notes.append(f"{len(unreachable)} alignment mark(s) land > ±{tol_um/1000:g} mm "
                         f"off field center ({', '.join(unreachable)}) -- unreachable.")

    # ---- PRE-PHASE: dead-space ablation (mirror pflm.plan.build_set), prepended --
    # Ablate each chip's cell footprint minus its pin-field box, before the pinfins,
    # one continuous phase (no masks), then a single wash pause. Prepended to the
    # schedule + arrays so the preview's step list matches the built plan.json.
    n_deadspace = 0
    if ablate_dead_space and cell_spec:
        try:
            cell_boxes = list(pflm_arrays.detect_arrays(layout, cell_spec))
        except Exception as exc:  # noqa: BLE001 - surfaced as a preview note
            cell_boxes = []
            notes.append(f"Cell layer '{cell_spec}' not read: {exc}")
        ds_passes = int(pflm_plan.DEAD_SPACE_ETCH["passes"])
        pin_avs = [a for a in arrays if not a.is_align and not a.is_deadspace]
        ds_steps: list = []
        for av in pin_avs:
            bcx, bcy = av.bbox_center_um
            cellbox = min(cell_boxes,
                          key=lambda b: abs(b.center_um[0] - bcx) + abs(b.center_um[1] - bcy),
                          default=None)
            if cellbox is None:
                continue
            l, b, r, t = cellbox.bbox_um
            corners_um = [pflm_arrays.rotate_point_um((l, b), deg),
                          pflm_arrays.rotate_point_um((r, b), deg),
                          pflm_arrays.rotate_point_um((r, t), deg),
                          pflm_arrays.rotate_point_um((l, t), deg)]
            xs = [x for x, _ in corners_um]
            ys = [y for _, y in corners_um]
            n_deadspace += 1
            ds_aid = "ds_" + av.array_id
            arrays.append(ArrayView(
                array_id=ds_aid, row_index=-1, col_index=av.col_index,
                step=n_deadspace, phase=0,
                exposed_corners_mm=[(x / 1000.0, y / 1000.0) for x, y in corners_um],
                exposed_center_mm=av.exposed_center_mm,
                exposed_center_um=av.exposed_center_um,
                bbox_center_um=av.bbox_center_um,
                exposed_w_mm=(max(xs) - min(xs)) / 1000.0,
                exposed_h_mm=(max(ys) - min(ys)) / 1000.0,
                polygon_count=0, has_geometry=True, fits_field=True,
                is_deadspace=True, ds_index=n_deadspace, passes=ds_passes,
            ))
            ds_steps.append({"step": 0, "action": "expose", "array_id": ds_aid,
                             "row_index": -1, "phase": 0, "type": "deadspace",
                             "passes": ds_passes})
        if ds_steps:
            ds_steps.append({"step": 0, "action": "mask",
                             "label": "dead-space removal complete -- wash + clean, then Resume"})
            schedule = ds_steps + schedule        # dead-space phase runs FIRST
            for i, s in enumerate(schedule):      # renumber the whole schedule
                s["step"] = i
            notes.append(f"Dead-space ablation: {n_deadspace} chip(s), {ds_passes} passes @ "
                         f"{pflm_plan.DEAD_SPACE_ETCH['speed_mm_s']:.0f} mm/s, crosshatch "
                         f"{pflm_plan.DEAD_SPACE_ETCH['fill_angles_deg']} deg -- prepended "
                         "(no masks, then one wash pause).")
        elif ablate_dead_space:
            notes.append(f"Dead-space requested but no cell footprints on '{cell_spec}'.")
    n_masks = sum(1 for s in schedule if s.get("action") == "mask")

    return PflmPreview(
        arrays=arrays, schedule=schedule,
        design_rotation_deg=deg, rotation_is_auto=rotation_is_auto,
        auto_rotation_deg=auto_deg,
        wafer_radius_mm=WAFER_RADIUS_MM, usable_half_mm=half_um / 1000.0,
        feasible=feasible, row_stack_axis=row_axis,
        required_row_span_mm=advance_um / 1000.0,
        required_withinrow_span_mm=sweep_um / 1000.0,
        travel_mm=(travel_um[0] / 1000.0, travel_um[1] / 1000.0),
        stage_y_max_um=stage_y_max_um, within_row_stride=within_row_stride,
        n_masks=n_masks, n_align_marks=n_align, align_tol_mm=tol_um / 1000.0,
        n_deadspace=n_deadspace, notes=notes,
    )


# ------------------------------------------------------------------- painting


@dataclass
class Viewport:
    """Maps millimetres to item pixels, y flipped, aspect preserved."""

    scale: float
    offset_x: float
    offset_y: float

    def point(self, x_mm: float, y_mm: float) -> QPointF:
        return QPointF(self.offset_x + x_mm * self.scale, self.offset_y - y_mm * self.scale)

    def rect(self, left: float, bottom: float, right: float, top: float) -> QRectF:
        return QRectF(self.point(left, top), self.point(right, bottom))


@QmlElement
class PreviewItem(QQuickPaintedItem):
    """Wafer view (all arrays in the as-exposed orientation) or field view
    (the selected array centered in the ±30 mm usable field)."""

    modeChanged = Signal()
    captionChanged = Signal()
    waferGuideChanged = Signal()
    stepChanged = Signal()
    stepCountChanged = Signal()
    stepLabelChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setRenderTarget(QQuickPaintedItem.FramebufferObject)
        self.setAntialiasing(True)
        self._preview: PflmPreview | None = None
        self._mode = "wafer"
        self._wafer_guide = True
        self._caption = ""
        self._step = 0

    # ---- mode
    def _get_mode(self) -> str:
        return self._mode

    def _set_mode(self, value: str) -> None:
        if value != self._mode:
            self._mode = value
            self.modeChanged.emit()
            self.update()

    mode = Property(str, _get_mode, _set_mode, notify=modeChanged)

    # ---- wafer guide
    def _get_guide(self) -> bool:
        return self._wafer_guide

    def _set_guide(self, value: bool) -> None:
        if value != self._wafer_guide:
            self._wafer_guide = value
            self.waferGuideChanged.emit()
            self.update()

    waferGuide = Property(bool, _get_guide, _set_guide, notify=waferGuideChanged)

    # ---- caption
    def _get_caption(self) -> str:
        return self._caption

    caption = Property(str, _get_caption, notify=captionChanged)

    # ---- step (index into the full schedule, expose + mask)
    def _get_step(self) -> int:
        return self._step

    def _set_step(self, value: int) -> None:
        count = self._get_step_count()
        value = max(0, min(int(value), max(count - 1, 0)))
        if value != self._step:
            self._step = value
            self.stepChanged.emit()
            self.stepLabelChanged.emit()
            self.update()

    step = Property(int, _get_step, _set_step, notify=stepChanged)

    def _get_step_count(self) -> int:
        return len(self._preview.schedule) if self._preview else 0

    stepCount = Property(int, _get_step_count, notify=stepCountChanged)

    def _get_step_label(self) -> str:
        if not self._preview or not self._preview.schedule:
            return ""
        if self._step >= len(self._preview.schedule):
            return ""
        entry = self._preview.schedule[self._step]
        if entry.get("action") == "mask":
            return f"MASK · {entry.get('label', 'mask, then Resume')}"
        av = self._array_for_step(self._step)
        if av is None:
            return f"expose {entry.get('array_id', '')}"
        pv = self._preview
        if av.is_deadspace:
            spd = int(pflm_plan.DEAD_SPACE_ETCH["speed_mm_s"]) if pflm_plan else 1000
            return (f"Dead-space #{av.ds_index}/{pv.n_deadspace} · {av.array_id} · "
                    f"{av.passes} passes @ {spd} mm/s · ablate cell minus pin box")
        if av.is_align:
            off = max(abs(av.field_offset_mm[0]), abs(av.field_offset_mm[1]))
            return (f"Align mark #{av.align_index + 1}/{pv.n_align_marks} · {av.array_id} · "
                    f"{av.passes} passes · lands {off:.1f} mm off center"
                    + ("" if av.fits_field else f" · OUT OF ±{pv.align_tol_mm:g} mm"))
        n_pinfin = len(pv.arrays) - pv.n_align_marks - pv.n_deadspace
        return (f"Expose #{av.step}/{n_pinfin} · {av.array_id} · "
                f"phase {PHASE_LABEL.get(av.phase, '?')} · row {av.row_index}"
                + ("" if av.has_geometry else " · EMPTY")
                + ("" if av.fits_field else " · OUT OF FIELD"))

    stepLabel = Property(str, _get_step_label, notify=stepLabelChanged)

    # ---- wiring
    def set_preview(self, preview) -> None:
        self._preview = preview
        self._step = 0
        if preview is None:
            self._caption = ""
        else:
            fit = "feasible" if preview.feasible else "INFEASIBLE"
            rot = (f"auto → {preview.design_rotation_deg}°"
                   if preview.rotation_is_auto else f"{preview.design_rotation_deg}°")
            n_align = preview.n_align_marks
            n_ds = preview.n_deadspace
            n_pinfin = len(preview.arrays) - n_align - n_ds
            arrays_txt = (f"{n_pinfin} arrays"
                          + (f" + {n_ds} dead-space" if n_ds else "")
                          + (f" + {n_align} align marks" if n_align else ""))
            self._caption = (
                f"{arrays_txt} · {preview.n_masks} mask pauses · "
                f"rotation {rot} · sweep {preview.required_withinrow_span_mm:.1f} mm on "
                f"stage-X · rows {preview.required_row_span_mm:.1f} mm on stage-Y · "
                f"stage {fit}")
        self.captionChanged.emit()
        self.stepChanged.emit()
        self.stepCountChanged.emit()
        self.stepLabelChanged.emit()
        self.update()

    def _array_for_step(self, idx: int):
        if not self._preview or idx >= len(self._preview.schedule):
            return None
        entry = self._preview.schedule[idx]
        if entry.get("action") != "expose":
            return None
        aid = entry.get("array_id")
        for av in self._preview.arrays:
            if av.array_id == aid:
                return av
        return None

    def _selected_array(self):
        """The array at the current step, or the nearest preceding expose step."""
        if not self._preview:
            return None
        idx = self._step
        while idx >= 0:
            av = self._array_for_step(idx)
            if av is not None:
                return av
            idx -= 1
        return self._preview.arrays[0] if self._preview.arrays else None

    # -------------------------------------------------------------- helpers

    def _font(self, painter: QPainter, size: int, bold: bool = False) -> None:
        font = QFont(FACE)
        font.setPixelSize(size)
        font.setBold(bold)
        painter.setFont(font)

    # ---------------------------------------------------------------- paint

    def paint(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        rect = QRectF(0, 0, self.width(), self.height())
        painter.fillRect(rect, QColor(SURFACE))
        if self._preview is None:
            self._font(painter, 13)
            painter.setPen(QPen(QColor(TEXT_3)))
            painter.drawText(rect, Qt.AlignCenter,
                             "Choose a source GDS to see the exposure preview")
            return
        if self._mode == "field":
            self._draw_field(painter, rect)
        else:
            self._draw_wafer(painter, rect)

    # The wafer with every array in the as-exposed orientation.
    def _draw_wafer(self, painter: QPainter, rect: QRectF) -> None:
        pv = self._preview
        radius = pv.wafer_radius_mm
        span = radius * 1.10
        usable = min(rect.width(), rect.height()) - 30
        view = Viewport(max(usable, 40.0) / (2 * span), rect.center().x(), rect.center().y())

        # round wafer
        if self._wafer_guide:
            pen = QPen(QColor(GUIDE_COLOR))
            pen.setWidthF(1.4)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            center = view.point(0.0, 0.0)
            painter.drawEllipse(center, radius * view.scale, radius * view.scale)

        # ±usable-half field square + crosshair
        half = pv.usable_half_mm
        pen = QPen(QColor(FIELD_COLOR))
        pen.setWidthF(1.2)
        pen.setCosmetic(True)
        pen.setDashPattern([5, 4])
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(view.rect(-half, -half, half, half))

        pen = QPen(QColor("#454545"))
        pen.setWidthF(1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawLine(view.point(-radius, 0), view.point(radius, 0))
        painter.drawLine(view.point(0, -radius), view.point(0, radius))

        selected = self._selected_array()
        for av in pv.arrays:
            self._draw_array_box(painter, view, av, av is selected)

        self._draw_legend(painter, rect, pv)

    def _draw_array_box(self, painter: QPainter, view: Viewport, av: ArrayView,
                        selected: bool) -> None:
        poly = QPolygonF([view.point(x, y) for x, y in av.exposed_corners_mm])
        if av.is_deadspace:
            # cell footprint outline framing the chip; amber dashed, no fill/number
            pen = QPen(QColor(SELECT_COLOR) if selected else QColor(DEADSPACE_COLOR))
            pen.setWidthF(2.4 if selected else 1.1)
            pen.setCosmetic(True)
            pen.setDashPattern([5, 3])
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPolygon(poly)
            return
        base = (ALIGN_COLOR if av.is_align
                else NO_GEOM_COLOR if not av.has_geometry
                else PHASE_COLORS.get(av.phase, "#8a8a8a"))
        colour = QColor(base)
        painter.setBrush(QColor(colour.red(), colour.green(), colour.blue(),
                                70 if selected else 26))
        pen = QPen(QColor(SELECT_COLOR) if selected else colour)
        pen.setWidthF(2.4 if selected else 1.5)
        pen.setCosmetic(True)
        if not av.fits_field:
            pen.setColor(QColor(SEAM_COLOR))
            pen.setDashPattern([4, 3])
        painter.setPen(pen)
        painter.drawPolygon(poly)

        # exposure-order number (align marks: A1..An) at the center
        c = view.point(*av.exposed_center_mm)
        self._font(painter, 14 if selected else 12, bold=selected)
        painter.setPen(QPen(QColor(SELECT_COLOR if selected else TEXT_2)))
        label = ("A%d" % (av.align_index + 1)) if av.is_align else str(av.step)
        painter.drawText(QRectF(c.x() - 20, c.y() - 10, 40, 20),
                         Qt.AlignCenter, label)

    def _draw_legend(self, painter: QPainter, rect: QRectF, pv: PflmPreview) -> None:
        self._font(painter, 11, bold=True)
        x = rect.left() + 12
        y = rect.top() + 14
        items = [("Phase A", PHASE_COLORS[0]), ("Phase B", PHASE_COLORS[1]),
                 ("empty", NO_GEOM_COLOR)]
        if pv.n_deadspace:
            items.append(("dead space", DEADSPACE_COLOR))
        if pv.n_align_marks:
            items.append(("align mark", ALIGN_COLOR))
        items.append(("field ±%g mm" % pv.usable_half_mm, FIELD_COLOR))
        for label, colour in items:
            painter.setBrush(QColor(colour))
            painter.setPen(Qt.NoPen)
            painter.drawRect(QRectF(x, y, 10, 10))
            painter.setPen(QPen(QColor(TEXT_2)))
            painter.drawText(QRectF(x + 14, y - 3, 120, 16), Qt.AlignLeft | Qt.AlignVCenter, label)
            y += 18

    # The selected array centered in the ±usable-half field, as it is exposed.
    def _draw_field(self, painter: QPainter, rect: QRectF) -> None:
        pv = self._preview
        av = self._selected_array()
        half = pv.usable_half_mm
        span = half * 1.25
        usable = min(rect.width(), rect.height()) - 30
        view = Viewport(max(usable, 40.0) / (2 * span), rect.center().x(), rect.center().y())

        # field square
        pen = QPen(QColor(FIELD_COLOR))
        pen.setWidthF(1.6)
        pen.setCosmetic(True)
        pen.setDashPattern([6, 4])
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(view.rect(-half, -half, half, half))

        pen = QPen(QColor("#454545"))
        pen.setWidthF(1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        reach = half * 0.12
        painter.drawLine(view.point(-reach, 0), view.point(reach, 0))
        painter.drawLine(view.point(0, -reach), view.point(0, reach))

        if av is None:
            self._font(painter, 13)
            painter.setPen(QPen(QColor(TEXT_3)))
            painter.drawText(rect, Qt.AlignCenter, "Mask pause — no array exposed")
            return

        # Pinfin arrays center on the field origin (the stage drives the array
        # center to field zero). Alignment marks center on the closest reachable
        # point and land off-center by field_offset_mm -- draw them there.
        hw, hh = av.exposed_w_mm / 2.0, av.exposed_h_mm / 2.0
        ox, oy = av.field_offset_mm if av.is_align else (0.0, 0.0)
        base = (DEADSPACE_COLOR if av.is_deadspace
                else ALIGN_COLOR if av.is_align
                else NO_GEOM_COLOR if not av.has_geometry
                else PHASE_COLORS.get(av.phase, "#8a8a8a"))
        colour = QColor(base)
        painter.setBrush(QColor(colour.red(), colour.green(), colour.blue(), 40))
        pen = QPen(colour if av.fits_field else QColor(SEAM_COLOR))
        pen.setWidthF(2.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawRect(view.rect(ox - hw, oy - hh, ox + hw, oy + hh))
        if av.is_deadspace:
            # the 10x10 pin-field box is NOT ablated -- draw it as an untouched cut-out
            painter.setBrush(QColor(SURFACE))
            hpen = QPen(QColor(PHASE_COLORS[0]))
            hpen.setWidthF(1.4)
            hpen.setCosmetic(True)
            painter.setPen(hpen)
            painter.drawRect(view.rect(-5.0, -5.0, 5.0, 5.0))
            self._font(painter, 10)
            painter.setPen(QPen(QColor(PHASE_COLORS[0])))
            painter.drawText(view.rect(-5.0, -5.0, 5.0, 5.0), Qt.AlignCenter, "pin box\n(untouched)")
        if av.is_align and (abs(ox) > 1e-6 or abs(oy) > 1e-6):
            # guide line from field center to the off-center mark
            gpen = QPen(QColor(colour.red(), colour.green(), colour.blue(), 150))
            gpen.setWidthF(1.0)
            gpen.setCosmetic(True)
            gpen.setDashPattern([3, 3])
            painter.setPen(gpen)
            painter.drawLine(view.point(0, 0), view.point(ox, oy))

        self._font(painter, 13, bold=True)
        painter.setPen(QPen(QColor(SELECT_COLOR)))
        if av.is_deadspace:
            head = f"{av.array_id}  ·  dead-space ablation  ·  {av.passes} passes"
        elif av.is_align:
            head = (f"{av.array_id}  ·  alignment mark {av.align_index + 1}"
                    f"  ·  {av.passes} passes")
        else:
            head = (f"{av.array_id}  ·  exposure #{av.step}  ·  phase "
                    f"{PHASE_LABEL.get(av.phase, '?')}")
        painter.drawText(rect.adjusted(0, 10, 0, 0), Qt.AlignHCenter | Qt.AlignTop, head)
        self._font(painter, 11)
        painter.setPen(QPen(QColor(TEXT_3 if av.fits_field else SEAM_COLOR)))
        if av.is_deadspace:
            spd = int(pflm_plan.DEAD_SPACE_ETCH["speed_mm_s"]) if pflm_plan else 1000
            sub = (f"ablate {av.exposed_w_mm:.1f} × {av.exposed_h_mm:.1f} mm cell "
                   f"minus 10 × 10 pin box  ·  {spd} mm/s")
        elif av.is_align:
            offmag = max(abs(ox), abs(oy))
            sub = (f"lands {offmag:.1f} mm off field center  ·  "
                   + (("within ±%g mm" % pv.align_tol_mm) if av.fits_field
                      else ("EXCEEDS ±%g mm" % pv.align_tol_mm)))
        else:
            sub = (f"{av.exposed_w_mm:.1f} × {av.exposed_h_mm:.1f} mm  ·  "
                   f"{av.polygon_count} polygons  ·  "
                   + ("fits field" if av.fits_field else "EXCEEDS ±%g mm field" % half))
        painter.drawText(rect.adjusted(0, 28, 0, 0), Qt.AlignHCenter | Qt.AlignTop, sub)


# -------------------------------------------------------------------- backend


class PreviewWorker(QThread):
    done = Signal(object, str)

    def __init__(self, params: dict) -> None:
        super().__init__()
        self._params = params

    def run(self) -> None:
        try:
            self.done.emit(build_pflm_preview(**self._params), "")
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            self.done.emit(None, str(exc))


class Bridge(QObject):
    statusChanged = Signal()
    busyChanged = Signal()
    logAppended = Signal(str)
    logCleared = Signal()
    previewReady = Signal()
    layersChanged = Signal()
    datasetsChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._status = ("pflm import failed: " + PFLM_IMPORT_ERROR
                        if PFLM_IMPORT_ERROR else
                        "Choose a source GDS, or load a saved dataset.")
        self._busy = False
        self._layers = LayerModel()
        self._entries: list = []
        self._item: PreviewItem | None = None
        self._worker: PreviewWorker | None = None
        self._process: QProcess | None = None
        self._notes: list[str] = []
        self._feasible = True
        self._datasets: dict[str, dict] = self._read_datasets()

    # ------------------------------------------------------------ datasets

    def _read_datasets(self) -> dict:
        if DATASETS_JSON.exists():
            try:
                loaded = json.loads(DATASETS_JSON.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    return loaded
            except (OSError, ValueError):
                pass
        return {}

    def _write_datasets(self) -> None:
        try:
            DATASETS_JSON.write_text(json.dumps(self._datasets, indent=2, sort_keys=True),
                                     encoding="utf-8")
        except OSError as exc:
            self._set_status(f"Could not save datasets: {exc}")

    def _get_dataset_names(self) -> list:
        return sorted(self._datasets)

    datasetNames = Property(list, _get_dataset_names, notify=datasetsChanged)

    @Slot(str, "QVariantMap")
    def saveDataset(self, name: str, params) -> None:
        name = name.strip()
        if not name:
            self._set_status("Give the dataset a name first.")
            return
        self._datasets[name] = {key: params.get(key) for key in DATASET_FIELDS}
        self._write_datasets()
        self.datasetsChanged.emit()
        self._set_status(f"Saved dataset '{name}'.")

    @Slot(str, result="QVariantMap")
    def loadDataset(self, name: str) -> dict:
        stored = self._datasets.get(name)
        if not stored:
            return {"ok": False}
        result = dict(stored)
        result["ok"] = True
        source = Path(str(stored.get("input") or ""))
        if source.is_file():
            try:
                self._entries = pflm_layers.inspect_layers(str(source))
                self._layers.set_layers(self._entries)
                self.layersChanged.emit()
                result["pinfinRow"] = self._layers.row_of(str(stored.get("pinfin") or ""))
                result["bboxRow"] = self._layers.row_of(str(stored.get("bbox") or ""))
                result["alignRow"] = self._layers.row_of(str(stored.get("align") or ""))
            except Exception as exc:  # noqa: BLE001
                self._set_status(f"Dataset '{name}': could not read source: {exc}")
        else:
            self._set_status(f"Dataset '{name}': source file is missing.")
        return result

    @Slot(str)
    def deleteDataset(self, name: str) -> None:
        if self._datasets.pop(name, None) is not None:
            self._write_datasets()
            self.datasetsChanged.emit()
            self._set_status(f"Deleted dataset '{name}'.")

    # ---------------------------------------------------------- properties

    def _get_status(self) -> str:
        return self._status

    status = Property(str, _get_status, notify=statusChanged)

    def _set_status(self, text: str) -> None:
        self._status = text
        self.statusChanged.emit()

    def _get_busy(self) -> bool:
        return self._busy

    busy = Property(bool, _get_busy, notify=busyChanged)

    def _set_busy(self, value: bool) -> None:
        if value != self._busy:
            self._busy = value
            self.busyChanged.emit()

    def _get_geometry_summary(self) -> str:
        return (f"field ±{USABLE_HALF_UM / 1000:g} mm usable  ·  "
                f"qualified {QUALIFIED_UM / 1000:g} mm  ·  "
                f"stage {TRAVEL_UM[0] / 1000:g}×{TRAVEL_UM[1] / 1000:g} mm  ·  "
                f"Y ceiling +{STAGE_Y_MAX_UM / 1000:g} mm")

    geometrySummary = Property(str, _get_geometry_summary, constant=True)

    def _get_layers(self) -> QObject:
        return self._layers

    layerModel = Property(QObject, _get_layers, notify=layersChanged)

    def _get_notes(self) -> list:
        return self._notes

    notes = Property(list, _get_notes, notify=previewReady)

    def _get_feasible(self) -> bool:
        return self._feasible

    feasible = Property(bool, _get_feasible, notify=previewReady)

    # --------------------------------------------------------------- slots

    @Slot(QObject)
    def attachPreview(self, item) -> None:
        self._item = item

    @Slot(QUrl, result="QVariantMap")
    def loadFile(self, url: QUrl) -> dict:
        path = Path(url.toLocalFile() if url.isLocalFile() else url.toString())
        return self.loadPath(str(path))

    @Slot(str, result="QVariantMap")
    def loadPath(self, text: str) -> dict:
        if pflm_layers is None:
            self._set_status("pflm not importable: " + PFLM_IMPORT_ERROR)
            return {"ok": False}
        path = Path(text)
        if not path.is_file():
            self._set_status(f"Not a file: {path}")
            return {"ok": False}
        try:
            self._entries = pflm_layers.inspect_layers(str(path))
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Could not read: {exc}")
            return {"ok": False}
        self._layers.set_layers(self._entries)
        self.layersChanged.emit()
        self._set_status(f"{len(self._entries)} layer(s) in {path.name}")
        return {
            "ok": True,
            "path": str(path),
            "pinfinRow": self._safe(pflm_layers.best_pinfin_row),
            "bboxRow": self._safe(pflm_layers.best_bbox_row),
            "alignRow": self._align_row(),
            "suggestedOutput": _dated_name(path.stem),
        }

    def _safe(self, fn) -> int:
        try:
            return int(fn(self._entries))
        except Exception:  # noqa: BLE001
            return -1

    def _align_row(self) -> int:
        """No best_align heuristic in the contract; prefer the conventional 5/0
        alignment layer, else the smallest non-empty layer."""
        row = self._layers.row_of("5/0")
        if row >= 0:
            return row
        best, best_area = -1, None
        for i, e in enumerate(self._entries):
            if e.polygons and (best_area is None or e.area_mm2 < best_area):
                best, best_area = i, e.area_mm2
        return best

    @Slot(int, result=str)
    def selectorAt(self, row: int) -> str:
        return self._layers.selector_at(row)

    def _resolve_spec(self, selector: str) -> str:
        """Numeric 'layer/datatype' for a selector, even if the combo shows a name."""
        sel = str(selector or "").strip()
        for entry in self._entries:
            if getattr(entry, "selector", None) == sel:
                return f"{entry.layer}/{entry.datatype}"
        return sel

    @Slot("QVariantMap")
    def refreshPreview(self, params) -> None:
        path = Path(str(params.get("input", "")))
        if not path.is_file():
            return
        if pflm_arrays is None:
            self._set_status("pflm not importable: " + PFLM_IMPORT_ERROR)
            return
        if self._worker is not None and self._worker.isRunning():
            return
        self._set_busy(True)
        self._worker = PreviewWorker({
            "input_path": str(path),
            "pinfin_spec": self._resolve_spec(params.get("pinfin", "")),
            "bbox_spec": self._resolve_spec(params.get("bbox", "")),
            "align_spec": self._resolve_spec(params.get("align", "")),
            "rotation": str(params.get("rotation", "auto")),
            "within_row_stride": int(params.get("withinRowStride", 2) or 2),
            "ablate_dead_space": bool(params.get("ablateDeadSpace", False)),
            "cell_spec": (self._resolve_spec(params.get("cell", "")) or "4/0"),
        })
        self._worker.done.connect(self._preview_done)
        self._worker.start()

    def _preview_done(self, preview, error: str) -> None:
        self._set_busy(False)
        if error:
            self._notes = [error]
            self._feasible = True
            self._set_status("Preview failed.")
        else:
            self._notes = list(preview.notes)
            self._feasible = preview.feasible
            n_align = preview.n_align_marks
            n_ds = preview.n_deadspace
            n_pinfin = len(preview.arrays) - n_align - n_ds
            self._set_status(
                f"{n_pinfin} arrays"
                + (f" + {n_ds} dead-space" if n_ds else "")
                + (f" + {n_align} align marks" if n_align else "")
                + f" · rotation {preview.design_rotation_deg}° · "
                + ("stage feasible" if preview.feasible else "STAGE INFEASIBLE"))
        if self._item is not None:
            self._item.set_preview(preview)
        self.previewReady.emit()

    @Slot("QVariantMap")
    def runBuild(self, params) -> None:
        if self._process is not None and self._process.state() != QProcess.NotRunning:
            return
        path = Path(str(params.get("input", "")))
        if not path.is_file():
            self._set_status("Choose a source GDS first.")
            return

        pinfin = self._resolve_spec(params.get("pinfin", "")) or "3/0"
        bbox = self._resolve_spec(params.get("bbox", "")) or "4/0"
        align = self._resolve_spec(params.get("align", "")) or "5/0"
        set_name = str(params.get("output", "")).strip() or _dated_name(path.stem)
        global_x = float(params.get("globalX", 0.0) or 0.0)
        global_y = float(params.get("globalY", 0.0) or 0.0)
        rotation = str(params.get("rotation", "auto")).strip().lower() or "auto"
        manifest = _find_manifest(path)   # per-array etch params (passes + fill angles)

        # Match pflm.cli build flags exactly -- no invented flags.
        # --circles: these are round-pin arrays; CIRCLE export is exact and fast,
        # where polygonizing 512-gon x ~10k pins x 14 arrays is minutes-slow.
        arguments = [
            "-m", "pflm.cli", "build", str(path),
            "--pinfin", pinfin,
            "--bbox", bbox,
            "--align", align,
            "--set", set_name,
            "--rotation", rotation,
            "--circles",
            "--global-x", f"{global_x:g}",
            "--global-y", f"{global_y:g}",
        ]
        if manifest is not None:
            arguments += ["--params", str(manifest)]
        ablate = bool(params.get("ablateDeadSpace", False))
        if ablate:
            cell = self._resolve_spec(params.get("cell", "")) or "4/0"
            arguments += ["--ablate-dead-space", "--cell", cell]
        if not bool(params.get("backside", True)):
            arguments.append("--no-backside")

        self.logCleared.emit()
        self.logAppended.emit("> python " + " ".join(arguments) + "\n\n")
        if manifest is not None:
            self.logAppended.emit(f"[etch] per-array params from {manifest.name} "
                                  "(square 0/90, hex -30/+30).\n")
            if rotation not in ("auto", "0"):
                self.logAppended.emit(
                    f"[etch] WARNING: the manifest is written in the rotation-0 frame; at "
                    f"rotation {rotation} the per-array join may miss and arrays fall back "
                    "to the default 0/90 fill. Use rotation 0 (or auto) for a pre-baked design.\n")
            self.logAppended.emit("\n")
        else:
            self.logAppended.emit(
                "[etch] WARNING: no design manifest (<name>_manifest.csv) beside the GDS -- "
                "every array will use the DEFAULT 0/90 crosshatch and NO per-array pass "
                "counts. Put the manifest next to the GDS and rebuild to fix.\n\n")
        if ablate:
            self.logAppended.emit(
                "[dead-space] phase 1: ablate each chip's cell minus its pin box "
                "(%d passes @ %.0f mm/s), per chip, no masks, then a wash pause.\n\n"
                % (pflm_plan.DEAD_SPACE_ETCH["passes"],
                   pflm_plan.DEAD_SPACE_ETCH["speed_mm_s"]))
        self._set_busy(True)
        self._set_status("Building set...")

        self._process = QProcess(self)
        self._process.setWorkingDirectory(str(REPO_ROOT))
        self._process.setProcessChannelMode(QProcess.MergedChannels)
        self._process.readyReadStandardOutput.connect(self._drain_process)
        self._process.finished.connect(self._process_finished)
        self._process.start(sys.executable, arguments)

    def _drain_process(self) -> None:
        if self._process is None:
            return
        chunk = bytes(self._process.readAllStandardOutput()).decode("utf-8", "replace")
        if chunk:
            self.logAppended.emit(chunk)

    def _process_finished(self, code: int, _status) -> None:
        self._set_busy(False)
        if code == 0:
            self.logAppended.emit("\nDone.\n")
            self._set_status("Finished.")
        else:
            self.logAppended.emit(f"\nFailed with exit code {code}.\n")
            self._set_status("Failed. See the log.")
        self._process = None


def main() -> int:
    QGuiApplication.setApplicationName("UV Laser Exposure")
    QGuiApplication.setOrganizationName("UV-Laser-Exposure")

    # Qt's own Windows 11 style. Override with PFLM_QML_STYLE=Basic if it misbehaves.
    QQuickStyle.setStyle(os.environ.get("PFLM_QML_STYLE", "FluentWinUI3"))

    app = QGuiApplication(sys.argv)
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)

    engine = QQmlApplicationEngine()
    bridge = Bridge()
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.addImportPath(str(HERE / "qml"))
    engine.load(QUrl.fromLocalFile(str(HERE / "qml" / "Main.qml")))
    if not engine.rootObjects():
        print("Failed to load the QML interface.", file=sys.stderr)
        return 1

    window = engine.rootObjects()[0]
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    if positional and Path(positional[0]).is_file():
        window.setProperty("initialFile", str(Path(positional[0])))

    # Development aid: render to a PNG and quit, so the layout can be reviewed
    # without a person holding the window open.
    if "--screenshot" in sys.argv:
        target = Path(sys.argv[sys.argv.index("--screenshot") + 1])

        def capture() -> None:
            image = window.grabWindow()
            target.parent.mkdir(parents=True, exist_ok=True)
            saved = image.save(str(target))
            print(f"{'saved' if saved else 'FAILED to save'} {target} "
                  f"({image.width()}x{image.height()})", flush=True)
            app.quit()

        QTimer.singleShot(4000, capture)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
