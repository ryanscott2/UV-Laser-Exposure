# UV-Laser-Exposure — Architecture & Module Contract

This document is the **authoritative contract** for the codebase. Every module is built
against the interfaces, schemas, coordinate frames, and constants defined here so the
prep half and the laser-PC half interlock. Where a Singulation source file is named, the
new file **reads and adapts** that file (paths are absolute, under
`C:\Users\ryan_\OneDrive - Stanford\SU26\UV Laser Singulation`).

---

## 1. What the tool does

Expose N pinfin/heater arrays on the **backside** of a 100 mm wafer, **one row at a time**,
holding the array currently being exposed at the **center of the laser field**. After each
row, pause so the operator can mask the completed row. Exposure order is **top row first,
working downward** (highest wafer +Y first).

Reference input: `081026_PFLM_Heaters.gds` — top cell `wafer`, 100 mm (dbu = 0.001 µm = 1 nm):

| GDS layer | Contents | Role |
|---|---|---|
| `3/0` | pinfin/heater geometry (39 shapes/array, 546 total) | **pinfin layer** — geometry to expose |
| `4/0` | one 38.7 × 10.5 mm rectangle per array (14 total) | **bbox layer** — one box = one array |
| `5/0` | 8 shapes = **4 marks** at (0,±45) & (±45,0) mm (coincident pairs; extent ±45.6 mm) | reference marks (not exposed) |
| `0/0`, `1/0` | 100 mm outline, 97 mm box | wafer reference |

The production wafer is the **v2 10-cell staggered layout** (`design/build_wafer_v2.py`): 10
cells in 3 rows (top 3 / middle 4 / bottom 3), each cell 10.5 mm wide × 38.7 mm tall, authored
directly in the exposed frame. Each cell fits the 60 mm usable field, so **one cell = one centered
exposure** (10 exposures). Rows expose top→bottom with a mask pause between them; the within-row
stride-2 interleave (§2.2) applies unchanged — it is design-agnostic (data-driven off `plan.json`).

> **Reachability caveat (v2).** The row centers sit at exposed-Y ±19450 µm, which map to stage-Y
> **+394 / −38506 µm** — ~400 µm *outside* the pipe-limited window `Y[−38140, 0]`. So the **top and
> bottom rows are not reachable as-authored** (only the middle row fits); `pflm` marks the set
> `stage.feasible=false` and the laser-PC pre-flight refuses it. Resolving it is a design call:
> field-offset those rows (as align marks already do via `clamp_center`), tighten the row offset by
> ~0.4 mm, or re-center. (The original 14-array/8-row landscape design is retired.)

---

## 2. Coordinate frames & the centering contract

Three frames (all lengths in **microns** in code unless a name ends in `_mm`):

1. **Wafer/GDS frame** — origin at wafer center, +X right, +Y up (away from the primary
   flat). Read via `klayout.db`. Array centers come from this frame.
2. **Field/laser frame** — origin (0,0) = laser field center. Each array's geometry is
   translated by `-(array_bbox_center)` (plus calibration) so the active array's center
   lands on (0,0). **The laser runs with auto-centering OFF**, so the DXF origin is placed
   on the physical field center. This is the whole ballgame — never enable fit-to-field.
3. **Stage frame** — Prior OptiScan III (ES111) absolute microns. Alignment is **mechanical**
   (the jig + wafer flats fix the wafer at a known, repeatable position), so the wafer→stage
   mapping is a **fixed machine constant captured in a ONE-TIME taught stage reference**
   (mirroring Singulation), not a per-wafer teach (§6). The fine calibration lives in that
   taught reference; the DXF **global offset stays 0** (reserved for future small DXF
   corrections), so the correction is never double-applied on the stage. **Travel
   envelope: X = 126 mm, Y = 76 mm**
   (`laser-pc/optiscan.py` `TRAVEL_X_UM=126000`, `TRAVEL_Y_UM=76000`, from the controller's
   STAGE report). Placement verified to 1 µm (`POSITION_TOLERANCE_UM`); max jog step 20 mm.

**Field sizes** (from Singulation, machine constants): full galvo ≈ 78485 µm; usable
central `USABLE_FIELD_HALF_UM = 30000` (±30 mm); qualified/declared `QUALIFIED_FIELD_UM =
54000`. Every centered array must fit within ±30 mm of field center or the build flags it.

**Backside mirror**: exposing the backside means the wafer is flipped. Default convention
(Singulation backside): mirror about **X** (secondary/minor flat to the right), i.e.
`wx -> -wx`. This is a **calibration** (see §6), defaulting to X-mirror but confirmed on-rig.

### 2.1 Stage reachability & design rotation (DESIGN-CRITICAL)

The stage is the binding constraint, not the field. Two limits:

- **Travel envelope**: X = 126 mm, Y = 76 mm (nominal).
- **Reachable window (binding)**: after the 2026-08 re-datum the usable stage frame is
  **ASYMMETRIC** — `reachable_um` X ∈ [16236, 138529] µm, Y ∈ [−38140, 0] µm. The −38140 floor
  is a **PIPE** at the back of the stage (not the deeper hard stop) and the Y top is 0; targets
  outside this window are refused. (The old symmetric ±travel model and the +6950 µm P3/P4
  ceiling `STAGE_Y_MAX_UM` are demoted — the laser-PC binding limit is now this window, with
  `stage_y_max_um` = 0.)

The ported **definitive** taught mapping (from the dialed-in Singulation nest, confirmed on
this rig): `stage_X = 84433 − wafer_X`, `stage_Y = −18830 + wafer_Y` (µm), reference
`(84355, −19056)`. (Singulation's older P1..P4 fit `stage_X = 5590 − wafer_X`,
`stage_Y = −18450 + wafer_Y` is a superseded fallback only.) The PFLM arrays span
**wafer-Y ∈ [−36.6, +36.9] mm (73.5 mm)** and **wafer-X ∈ [−19.35, +19.35] mm (38.7 mm)**.
Unrotated, the top row (wafer_Y = +36.86 mm) needs stage_Y ≈ +18412 µm — **above the reachable
window's Y-max of 0 → unreachable.**

**Fix: rotate the DESIGN, not the wafer** (operator preference; flat-registered wafer stays
seated normally). A **+90°** design rotation swaps which wafer axis carries the long span:

| | row-stack span → axis | within-row span → axis | max stage-Y | fits? |
|---|---|---|---|---|
| 0° (unrotated) | 73.5 mm → stage-Y (short, ceiling-limited axis) | 38.7 mm → stage-X (126) | +18412 µm | ✗ over Y-max |
| **+90°** | 73.5 mm → **stage-X (126)** | 38.7 mm → **stage-Y** | **+900 µm** | ✓ long span on roomy axis |

*(The `max stage-Y` figures are illustrative, from the pre-re-datum Singulation mapping / +6950
ceiling; the current binding limit is the asymmetric reachable window in the first bullet above,
and the default rotation is now selected by `--jig-flat` (§5.6). The invariant is unchanged: the
73.5 mm row-stack must ride the roomy stage-X axis.)*

`design_rotation_deg` ∈ {0,90,180,270} is applied in prep to both the geometry (so the
centered DXFs carry the rotated features) and the array-center coordinates (so stage targets
use the rotated "exposed frame"). The CLI default is **`--jig-flat back` = 180°**, which
**overrides `--rotation`**: `--jig-flat` names the physical wafer-flat direction on the stage
and maps it to a rotation — front(−Y)=0, right(+X)=90, back(+Y)=180, left(−X)=270 — so the GDS
(authored flat−Y) turns to match the calibrated nest (major-flat +Y). When `--jig-flat` is
omitted, `--rotation auto` picks the rotation that puts the larger row-stack span on stage-X
and keeps every target inside the reachable window. Rotation is explicit, logged, and shown in
the preview — never silent.

**Physical row-by-row (debris redeposition)**: rows are grouped in the **exposed
(post-rotation) frame** — a "row" is a horizontal band at (near-)constant exposed-Y, the band
the operator masks as a unit. Rows are ordered **top→bottom** (exposed-Y descending). Within a
row, arrays are swept along **exposed-X (rides stage-X, the wider ~122 mm axis)** as a checkerboard
(§2.2); between rows the stage advances along **exposed-Y (rides stage-Y, the tight pipe-limited
axis)**. A full row (both phases) is exposed and masked before ANY array of the next row — **never
mix rows**. Grouping MUST happen AFTER any rotation (`group_exposed_rows`): a design column that a
rotation turns into a physical row is ONE row, not one array each from two different rows (grouping
by the pre-rotation design-Y would mix rows). The choose-rotation rule is unchanged: put the longer
array-extent on stage-X so the short extent rides the tighter stage-Y window (v2 is already authored
that way in the exposed frame).

**Reachability is checked twice**: (1) prep-side feasibility (machine-independent: after
rotation, row-stack span ≤ X travel, within-row span ≤ usable Y range under the ceiling —
recorded in `plan.json.stage`), and (2) laser-PC pre-flight (given the taught reference:
every array's stage target inside travel AND stage_Y ≤ `STAGE_Y_MAX_UM`, else refuse to run).

### 2.2 Exposure sequence & masking (debris redeposition)

Within each row, arrays are exposed in a **stride-2 interleave** ("every other"), not
straight sequential, so freshly-exposed unmasked arrays stay maximally spaced:

1. **Phase A** — expose arrays at col 0, 2, 4, … (every other).
2. **Mask pause** — operator masks the Phase-A arrays.
3. **Phase B** — expose the skipped arrays at col 1, 3, 5, … (each now flanked by masked
   neighbors).
4. **Mask pause** — operator masks the Phase-B arrays.
5. Advance to the next row (monotonic sweep, top→bottom).

`mask_strategy = { "within_row_stride": 2, "mask_between_groups": true }`. A **group** is one
non-empty phase of one row. The tool emits an explicit **`schedule`** (list of steps) into
`plan.json`: EXPOSE steps for a group's arrays, then a MASK pause **before any subsequent
EXPOSE**. So the number of mask pauses = (#non-empty groups) − 1; there is no pointless mask
after the final array. For rows with 1–2 arrays this yields expose→mask→expose→mask…; for a
wide production row it yields a real checkerboard (Phase A of N/2 arrays, one mask, Phase B).
`within_row_stride=1` degrades gracefully to "expose the whole row, then one mask".

The run loop (§7.3) executes the `schedule` verbatim; a MASK step is a controlled pause that
honors the STOP flag and resumes on operator confirmation (keypress / UI button / resume
flag file). The prep-app preview (§7.5) renders the schedule: arrays numbered in exposure
order, colored by phase, with mask-pause boundaries called out.

---

## 3. Repository layout

```
pflm/                     # PREP package (design PC, py3.11+, klayout+ezdxf). NO hardware imports.
  __init__.py
  layers.py               # layer inspection + selector parsing
  arrays.py               # per-array bbox detection, row grouping, top-down ordering  [NEW]
  centering.py            # clip pinfin region to an array bbox, translate center->origin [NEW/adapt]
  dxf_writer.py           # write_dxf_r2010 (adapt from split_klayout)
  plan.py                 # build_set(): GDS -> set folder (plan.json + jobs/*.dxf + manifest) [NEW]
  cli.py                  # `python -m pflm.cli inspect|build`
prep_app/
  prep_app.py             # PySide6/QML app (adapt slicing/slicer_app.py)
  qml/Main.qml            # (adapt slicing/qml/Main.qml)
laser_pc/                 # RUN package (offline laser PC, py3.8, pyserial+pywin32+tkinter). 3.8-COMPATIBLE.
  optiscan.py             # ~verbatim copy of laser-pc/optiscan.py
  transform.py            # wafer->stage transform + teach helpers (pure math, no deps) [NEW]
  winlase_build_jobs.py   # one .wlj per array (adapt laser-pc/winlase_build_jobs.py)
  expose_wafer.py         # row-by-row run loop (adapt laser-pc/dice_wafer.py)
  expose_ui.py            # Tkinter launcher (adapt laser-pc/dice_ui.py)
  run_ui.bat
output/sets/<name>/       # generated sets (gitignored)
docs/ARCHITECTURE.md
```

**Import rule:** `pflm/` never imports hardware libs; `laser_pc/` never imports
klayout/ezdxf/PySide6 (the laser PC doesn't have them). The only thing crossing between
halves is the **set folder** on disk.

---

## 4. The set folder (contract between the two halves)

`output/sets/<set_name>/`:
```
plan.json          # exposure plan (schema below)
jobs/<array_id>.dxf  # one centered DXF per array (R2010, mm, layer '0', closed LWPOLYLINE)
manifest.csv       # per-array audit row
prep_log.txt       # human log of the build (incl. dropped-geometry warnings)
WinLaseJobs/        # (added on laser PC) <set>_<array_id>.wlj — gitignored, rebuilt each run
```

### 4.1 `plan.json` schema (v1)
```json
{
  "schema_version": 1,
  "set_name": "081026_PFLM_Heaters",
  "source_gds": "081026_PFLM_Heaters.gds",
  "dbu_um": 0.001,
  "layers": { "pinfin": "3/0", "bbox": "4/0", "align": "5/0" },
  "wafer": { "diameter_mm": 100.0, "radius_um": 50000 },
  "field": { "usable_half_um": 30000, "qualified_um": 54000, "full_um": 78485 },
  "backside": true,
  "design_rotation_deg": 180,
  "exposure_order": "top_to_bottom",
  "mask_strategy": { "within_row_stride": 2, "mask_between_groups": true },
  "align_marks_um": [[x0,y0], ...],
  "stage": {
    "travel_um": [126000, 76000],
    "stage_y_max_um": 6950,
    "sweep_axis": "stage_x",
    "row_advance_axis": "stage_y",
    "sweep_span_um": 73500,
    "row_advance_span_um": 38700,
    "max_stage_y_um": 900,
    "feasible": true,
    "notes": "within-row sweep rides stage-X; rows advance along stage-Y (under +6950 ceiling)"
  },
  "rows": [
    {
      "row_index": 0,
      "exposed_y_center_um": 19350.0,
      "arrays": [
        {
          "array_id": "r00c00",
          "row_index": 0,
          "col_index": 0,
          "bbox_center_um": [19350.0, 26362.0],
          "exposed_center_um": [-26362.0, 19350.0],
          "bbox_um": [0.0, 21112.0, 38700.0, 31612.0],
          "polygon_count": 39,
          "has_geometry": true,
          "fits_field": true,
          "job_dxf": "jobs/r00c00.dxf"
        }
      ]
    }
  ],
  "schedule": [
    { "step": 0, "action": "expose", "array_id": "r00c00", "row_index": 0, "phase": 0 },
    { "step": 1, "action": "mask",   "label": "row 0 phase A complete (1 array) — mask, then Resume" },
    { "step": 2, "action": "expose", "array_id": "r01c00", "row_index": 1, "phase": 0 },
    { "step": 3, "action": "mask",   "label": "row 1 phase A complete — mask, then Resume" },
    { "step": 4, "action": "expose", "array_id": "r01c01", "row_index": 1, "phase": 1 }
  ]
}
```
Rules:
- `schedule` is the authoritative exposure sequence the laser PC executes: `expose` steps
  reference an `array_id` (→ `jobs/<id>.dxf` → `WinLaseJobs/<set>_<id>.wlj`); `mask` steps are
  controlled pauses. It is generated from `rows` + `mask_strategy` (§2.2): within each row,
  group arrays by `col_index % within_row_stride` (phase A = 0,2,4…; phase B = 1,3,5…),
  emit each non-empty group's exposes in `col_index` order, and insert one `mask` pause
  **before any subsequent `expose`** (so #masks = #non-empty groups − 1, no trailing mask).
- `rows` are **physical rows in the exposed frame** (bands at constant exposed-Y), grouped by
  `group_exposed_rows` AFTER the rotation and pre-sorted top→bottom: `row_index` 0 = highest
  exposed-Y band = exposed first. Within a row, arrays are sorted left→right by exposed-X;
  `col_index` reflects that order. `exposed_y_center_um` is the band's exposed-Y center.
  A full row is exposed+masked before the next — rows are never interleaved.
- `bbox_center_um` / `bbox_um` are in the **design frame** (`[left, bottom, right, top]`,
  wafer microns). `exposed_center_um` is the center **after** `design_rotation_deg`, in the
  exposed frame — this is what the laser-PC transform (§6) consumes to compute stage targets.
- The centered DXF in `jobs/<id>.dxf` contains the array's geometry **rotated by
  `design_rotation_deg`** and translated so its center is at (0,0).
- `array_id` = `r{row:02d}c{col:02d}`.
- `has_geometry=false` (no pinfin shapes inside the bbox) still writes a placed file but is
  flagged — an empty job looks identical to a real one at the machine (Singulation gotcha).
- `fits_field=false` if the centered bbox exceeds ±`usable_half_um`.
- `stage.feasible=false` if the rotated spans can't fit travel/ceiling — the build still
  writes but warns loudly; the laser-PC pre-flight will refuse to run.

### 4.1b Per-array etch params + circle export (design-driven)

When `build_set` is given a design manifest (`--params <manifest.csv>`), each detected array
is joined by nearest exposed center and its record gains
`etch = {passes, speed_mm_s, fill_style, fill_angles_deg, hatch_mm}`; each `expose` schedule
step also gains `passes`. This is how the row layout's per-type laser recipe (e.g. D50 sq =
44 passes, crosshatch 0°/90°) reaches the laser PC. `winlase_build_jobs` reads `fill_angles_deg`
(crosshatch, two angles) + `hatch_mm` + `speed_mm_s` per array; `expose_wafer` reads `passes`
per array and marks each array's job that many times — **one array = one job, run to
completion before the stage advances** — while power/freq stay fixed by the WinLase profile
(the read-only safety gate is unchanged). `--circles` exports round pins as true DXF `CIRCLE`
entities (compact/exact; the laser-PC `dxf_bounds_mm` parses CIRCLE center±radius) instead of
polygonizing many-gon pins — essential for dense arrays (a 512-gon × 10 000-pin array is
~5 M vertices as polygons).

**Laser-PC editable etch table** (`laser_pc/etch_params.py` + `etch_params.json`): the same
per-type table (defaults to the design values), editable in the run launcher `expose_ui.py`
(a passes grid + per-lattice crosshatch angles + hatch). `winlase_build_jobs --etch-params`
and `expose_wafer --etch-params` load it and apply passes/angles/speed/hatch **by array
`type`, overriding the plan's baked values** — so passes/angles can be tuned on the laser PC
between wafers without re-prepping. `--passes` still forces a uniform passes override. Power/
frequency are never in this table; they stay on the WinLase profile behind the read-only gate.

### 4.2 `manifest.csv` columns
`step,phase,array_id,row_index,col_index,bbox_center_x_um,bbox_center_y_um,exposed_center_x_um,exposed_center_y_um,bbox_left_um,bbox_bottom_um,bbox_right_um,bbox_top_um,polygon_count,has_geometry,fits_field,job_dxf`
(rows in `schedule` exposure order; `step`/`phase` from the schedule.)

---

## 5. Prep-half module contracts (`pflm/`)

### 5.1 `pflm/layers.py`  (adapt `slicing/run_splitter.py::inspect_layers` + `split_klayout.py` selectors)
```python
@dataclass(frozen=True)
class LayerInfo:
    selector: str        # "3/0"
    name: str            # "" if unnamed
    layer: int
    datatype: int
    polygons: int
    paths: int
    widths_um: tuple     # observed path widths
    area_mm2: float
    bbox_mm: tuple       # (l,b,r,t) or None

def inspect_layers(path: str) -> list[LayerInfo]: ...   # every layer, merged, area-sorted desc
def parse_layer_spec(spec: str) -> tuple:               # "" | "7" | "7/2" | name -> (layer|None,datatype|None,name|None)
def layer_matches(info, spec) -> bool: ...
def best_pinfin_row(layers) -> int: ...                 # heuristic: most polygons (pinfins are many small shapes)
def best_bbox_row(layers) -> int: ...                   # heuristic: few large equal-area rectangles
```
Load GDS via `pya.Layout().read(path)`; DXF via `LoadLayoutOptions().dxf_unit = 1000.0`.

### 5.2 `pflm/arrays.py`  [NEW — the heart of PFLM]
```python
@dataclass(frozen=True)
class ArrayBox:
    bbox_um: tuple          # (l,b,r,t) wafer microns
    center_um: tuple        # (cx,cy)
    width_um: float
    height_um: float
    polygon_count: int      # pinfin polygons inside (filled later)
    has_geometry: bool

@dataclass(frozen=True)
class Row:
    row_index: int
    y_center_um: float
    arrays: tuple           # ArrayBox, sorted left->right

def detect_arrays(layout, bbox_spec: str) -> list[ArrayBox]:
    """One ArrayBox per shape on the bbox layer (iterate shapes; do NOT merge into one Region).
       Recursive from top cell so array-instanced boxes are expanded (array copies each yield a box)."""

def detect_arrays_from_pinfins(layout, pinfin_spec: str, ...) -> list[ArrayBox]:
    """Fallback when no bbox layer: cluster pinfin shapes into arrays (grid/gap clustering)."""

def count_pinfins_in(layout, pinfin_spec, bbox_um) -> int:  # shapes of pinfin layer inside a bbox

def rotate_point_um(pt, deg) -> tuple:            # exact k*90 rotation about wafer origin
def rotation_feasibility(boxes, deg, *, travel_um=(126000,76000), stage_y_max_um=6950) -> dict:
    """Extent-based stage feasibility for a rotation. Returns {'deg','feasible',
       'sweep_span_um' (exposed-X extent, rides stage-X), 'row_advance_span_um'
       (exposed-Y extent, rides stage-Y), 'max_stage_y_um', 'sweep_axis','row_advance_axis',
       'long_on_x'}."""
def choose_rotation(boxes, *, travel_um=(126000,76000), stage_y_max_um=6950) -> dict:
    """Pick a k*90 rotation: feasible + longer array-extent on stage-X (short extent rides the
       ceiling-limited stage-Y). Returns the rotation_feasibility dict. Ties -> smallest positive deg."""

def group_exposed_rows(boxes, deg, row_tol_um: float = None) -> list[Row]:
    """Group into PHYSICAL rows in the EXPOSED (post-rotation) frame: cluster by exposed-Y
       band (default tol = half the median gap between distinct exposed-Y), order rows
       top->bottom (exposed-Y desc => row_index 0 highest), sort each row left->right by
       exposed-X. This — not design-Y grouping — is what prevents mixing rows after rotation."""
```
Grouping MUST run after the rotation. A design column that the +90° turns into a physical row
is ONE row; grouping by the pre-rotation design-Y would split each physical row across every
column and interleave rows during exposure (the row-mixing bug). Robust to the 1/2/2/2/2/2/2/1
layout (bands hold 1 or 2 arrays) and to wider production rows.

### 5.3 `pflm/centering.py`  (adapt `split_klayout.py` centering + clip)
```python
def clip_and_center(layout, pinfin_spec, bbox_um, *, global_offset_um=(0,0)) -> "pya.Region":
    """Region of pinfin shapes clipped to bbox, then translated by
       (-center_x + global_x, -center_y + global_y) so the array center is at (0,0)."""
def region_bbox_um(layout, region): ...
def fits_field(region, usable_half_um=30000) -> bool: ...
```
Same math as Singulation's `translate = -field_center + global_offset(+nudge)`, but the
"field center" is now each array's own bbox center.

### 5.4 `pflm/dxf_writer.py`  (adapt `split_klayout.py::write_dxf_r2010`)
```python
def write_dxf_r2010(path, region, dbu): ...   # AutoCAD R2010, $INSUNITS=4 (mm), closed LWPOLYLINE on layer '0', via ezdxf
```
KLayout's own DXF writer can't set version/units — must go through ezdxf.

### 5.5 `pflm/plan.py`  [NEW — orchestrator]
```python
def build_set(gds_path, set_dir, *, pinfin="3/0", bbox="4/0", align="5/0",
              backside=True, rotation_deg="auto", within_row_stride=2,
              travel_um=(126000,76000), stage_y_max_um=6950,
              global_offset_um=GLOBAL_OFFSET_UM, row_tol_um=None,
              overwrite_in_place=True) -> dict:
    # GLOBAL_OFFSET_UM = (0.0, 0.0): the old Singulation -3447/+460 slicer offset is RETIRED.
    # Bulk placement now lives in the TAUGHT stage reference (mirroring Singulation); this
    # zeroed knob is reserved for FUTURE small DXF corrections (a field-frame nudge applied
    # after rotation/centering). Keep the laser-PC calibration global_offset at 0 too, so the
    # correction is baked once in the DXF, not double-applied (§8).
    """Full prep pipeline:
       read GDS -> detect_arrays (design frame) -> choose_rotation (extent-based) ->
       group_exposed_rows (physical rows = exposed-Y bands, top->bottom; never mixed) ->
       for each array: count pinfins, clip_and_center WITH rotation, write jobs/<id>.dxf,
         record bbox_center_um (design) and exposed_center_um (rotated) ->
       build `schedule` from rows + within_row_stride (§2.2) ->
       write plan.json + manifest.csv + prep_log.txt. Returns the plan dict.
       Warn (don't silently drop) on empty arrays, geometry outside all bboxes, and
       stage.feasible=false. overwrite_in_place=True: never rmdir (OneDrive) — overwrite."""

def build_schedule(rows, within_row_stride=2) -> list:  # the §2.2 group+mask algorithm
```

### 5.6 `pflm/cli.py`
`python -m pflm.cli inspect <gds>` → print the layer table.
`python -m pflm.cli build <gds> [--pinfin 3/0] [--bbox 4/0] [--align 5/0] [--set NAME]
[--rotation auto|0|90|180|270] [--jig-flat front|right|back|left] [--stride N]
[--global-x UM] [--global-y UM] [--params CSV] [--circles] [--ablate-dead-space]
[--cell 4/0] [--no-dead-space-wash] [--no-backside] [--output DIR]` → `build_set(...)`.
(`--jig-flat` defaults to `back` = 180° and **overrides** `--rotation`; front/right/back/left → 0/90/180/270. See §2.1.)

---

## 6. Wafer→stage transform & alignment (`laser_pc/transform.py`)  [NEW, pure math, py3.8]

Alignment is **mechanical** (jig + flats fix the wafer) plus **ONE taught stage reference — no
optics**. The wafer→stage mapping is a fixed machine constant that lives in that taught
reference: `transform.default_calibration()` is the fallback, but the dialed-in
`exposure_calibration.json` config carries the definitive reference (a one-time site teach,
NOT a per-wafer teach). The fine calibration lives in the taught reference; the DXF
`global_offset` (§5.5) is held at `(0, 0)` and the stage `global_offset_um` stays `(0, 0)` too,
so the correction is never double-applied. `teach_reference` / `solve_transform` are the
OPTIONAL one-time helpers used to establish that reference; the per-wafer run path never
re-teaches.

The transform consumes each array's **`exposed_center_um`** (design frame already rotated by
`design_rotation_deg`, §2.1). `exposure_calibration.json` (lives on the laser PC, gitignored):
```json
{
  "units": "microns",
  "reference": { "wafer_um": [0.0, 0.0], "stage_um": { "x": 84355, "y": -19056, "z": 0 } },
  "axes": { "sx": 1, "sy": 1 },
  "mirror": { "x": true, "y": false },
  "global_offset_um": [0.0, 0.0],
  "per_array_offset_um": { "r00c00": [0.0, 0.0] },
  "travel_um": [126000, 76000],
  "stage_y_max_um": 0,
  "reachable_um": { "x_min": 16236, "x_max": 138529, "y_min": -38140, "y_max": 0 }
}
```
Transform (exposed-frame center of an array → absolute stage target that puts it at field center):
```python
def wafer_to_stage(exposed_um, cal) -> (x,y):
    wx, wy = exposed_um
    if cal.mirror.x: wx = -wx
    if cal.mirror.y: wy = -wy
    rx, ry = cal.reference.wafer_um  # apply same mirror to the reference point
    if cal.mirror.x: rx = -rx
    if cal.mirror.y: ry = -ry
    sx = cal.reference.stage.x + cal.axes.sx * (wx - rx) + cal.global_offset.x (+nudge)
    sy = cal.reference.stage.y + cal.axes.sy * (wy - ry) + cal.global_offset.y (+nudge)
    return sx, sy
```
The example above carries the ported **definitive** mapping (`stage_X = 84433 − wafer_X`,
`stage_Y = −18830 + wafer_Y`, reference `(84355, −19056)`); Singulation's older
`stage_X = 5590 − wafer_X`, `stage_Y = −18450 + wafer_Y` fit is a superseded fallback. Note the
X inversion comes from `mirror.x = true` (the backside flip), so `axes.sx = +1`; do not also
set `axes.sx = −1` or the two cancel.
- `axes.sx/sy` and `mirror` are **empirically determined on this rig** — Singulation warns
  the signs inverted from the first geometric guess. Verify with a test exposure.
- `solve_transform(pairs)`: given ≥2 taught `(exposed_um, stage_um)` points, least-squares
  solve sign+scale (and small rotation) so first-time setup can teach two known arrays and
  auto-derive `axes`.
- `teach_reference(optiscan)`: jog + record `reference.stage_um` (reuse `optiscan.cmd_jog`).
- `check_reachable(plan, cal) -> (ok, failures)`: for every array compute the stage target and
  assert it lies inside the calibration's **reachable window** (`reachable_um`, e.g.
  X ∈ [16236, 138529], Y ∈ [−38140, 0] with the −38140 pipe floor); when no explicit window is
  set it falls back to the legacy `|x| ≤ travel_x` / `−travel_y ≤ y ≤ stage_y_max_um` model.
  Returns the offending `array_id`s. Called by the laser-PC pre-flight (§7.3) and surfaced in
  the prep app so infeasible sets are caught before the wafer is on the machine.

---

## 7. Laser-PC module contracts (`laser_pc/`) — Python 3.8, adapt, keep safety verbatim

### 7.1 `optiscan.py` — **copy ~verbatim** from `laser-pc/optiscan.py`
`OptiScan(port='COM5')`: `goto(x,y)`, `goto_z(z)`, `move_rel`, `wait_idle` (two consecutive
idle `$`), `stage_position`, `_check_target` (soft limits), `_verify_at` (±1 µm,
`POSITION_TOLERANCE_UM`). Keep the
`COMP,0`/`ERROR,0` connect handshake and serial drain. Keep `cmd_jog` (teach) but write to
`exposure_calibration.json`'s `reference`/pairs instead of P1..P4.

### 7.2 `winlase_build_jobs.py` — adapt `laser-pc/winlase_build_jobs.py`
One `.wlj` per array (drop the Horizontal/Vertical two-graphic assumption — pinfin arrays
are a single graphic). Reuse verbatim: dynamic COM (`win32com.client.dynamic`,
`Winlase.Automate`), the leading-`[out]`-index calling convention for `NewJob`/
`NewVectorGraphic`, `GetLensCalFactor(0,0)` → bits/mm, `mm_to_bits`, `GetObjRect`+`OffsetObj`
to seat the graphic at its DXF-centered bbox, `SetObjFill(spacingBits, angle, angle, style)`,
`SetObjMarkFillFlag/OutlineFlag`, `SetObjNumPasses`, speed-only `SetObjProfile(obj,0,...)`,
`dxf_bounds_mm` (ENTITIES-only 10/20 parser). Keep the read-only **profile safety gate**
(`GetObjProfile` idx 0/5/9 = speed/power/freq; require power=100 %, freq=30 kHz, speed=400
mm/s within tolerance; NEVER write power/freq). Discover jobs from the set folder's `jobs/`
+ `plan.json` (not P1..P4 folders).
Constants: `FILL_SPACING_MM=0.01`, `FILL_ANGLE_DEG=0` (single angle), `NUM_PASSES=1`,
`MARK_SPEED_MM_S=400`, `FIELD_BIT_LIMIT=32768`, `IsObjOutOfBounds` check.

### 7.3 `expose_wafer.py` — adapt `laser-pc/dice_wafer.py`  [row-by-row is the new part]
Reuse wholesale: `WinLaseMarker` (`mark_job` async `MarkAllObj(0)` + `GetBusyStatus` poll to
two-idle, `TerminateMark` on timeout/abort), `verify_job_params`/`_check_active_params`,
abortable `countdown`, `.expose_stop` flag polling, `EtaTracker`, SIMULATE-by-default +
`--arm` gate (+ typed `EXPOSE` confirmation unless `--yes`).
Replace the fixed `STATION_ORDER=(P1..P4)` plan with the `schedule` from `plan.json`:
```
load plan.json + exposure_calibration.json; index arrays by array_id
PRE-FLIGHT (before any motion / arming):
  for every array: tgt = wafer_to_stage(array.exposed_center_um, cal)
    assert |tgt.x| within X travel and y_floor <= tgt.y <= STAGE_Y_MAX_UM   # the ceiling!
  if any target fails or plan.stage.feasible == false: REFUSE (print which arrays, exit)
  verify laser profile (power/freq/speed) read-only gate
countdown (skipped in SIMULATE unless --arm)
for step in plan.schedule (in order, from --start-step):
    if step.action == "expose":
        a = arrays[step.array_id]
        stage.goto(wafer_to_stage(a.exposed_center_um, cal)); (optional goto_z/focus)
        marker.mark_job(WinLaseJobs/<set>_<array_id>.wlj, passes, abort)
    elif step.action == "mask":
        print("[mask] " + step.label); wait_for_resume(stop_flag, resume_flag)  # controlled pause
```
`wait_for_resume` honors the STOP flag (controlled stop) and blocks until the operator
resumes (keypress in CLI, Resume button → resume flag file from the UI). CLI:
`expose_wafer.py <set_dir> [--arm] [--passes N|--passes-file] [--port COM5] [--focus]
[--stop-flag .expose_stop] [--resume-flag .expose_resume] [--yes] [--start-step N] [--list]`.
`--start-step` resumes mid-wafer. `--list` prints the schedule with computed stage targets
and the pre-flight verdict (reachable? under ceiling?) without moving. Passes per set via
`expose_passes.csv`.

### 7.4 `expose_ui.py` — adapt `laser-pc/dice_ui.py` (Tkinter, stdlib only)
Buttons: Pick set · Teach reference (jog) · Build jobs · Dry run · **EXPOSE (arm)** · STOP ·
Home/Extract. Shell out to the CLI scripts and stream stdout into a log pane (Queue+thread),
parse `[eta]` lines. Add a **row progress** readout and a modal "Mask row N, then Resume"
prompt driven by the run loop's pause (e.g. the loop prints `[mask] row N complete` and waits
on a resume flag file, mirroring the `.expose_stop` pattern). `run_ui.bat` → venv `pythonw
expose_ui.py`.

### 7.5 `prep_app/` — adapt `slicing/slicer_app.py` + `slicing/qml/Main.qml`
PySide6/QML. Keep the `Bridge` (QObject) + `LayerModel` + `PreviewItem`(QQuickPaintedItem) +
`PreviewWorker`(QThread) + QProcess-runs-CLI + `.ui_datasets.json` pattern and the DLL-path
shim / FluentWinUI3 dark style. Changes for PFLM:
- **Two** layer combos: pinfin layer (default = `best_pinfin_row`) and bbox layer
  (default = `best_bbox_row`), plus an align-layer combo.
- A **rotation** control (auto / 0 / 90 / 180 / 270) and a **within-row stride** control,
  with a live **stage-feasibility** readout (required row-stack span vs 126 mm X, within-row
  span vs the usable Y under the +6950 ceiling; red if infeasible).
- Preview drawn in the **as-exposed orientation** (after `design_rotation_deg`): round wafer,
  all detected array bboxes, numbered in **schedule/exposure order (1..N)** and colored by
  **phase** (A/B), with mask-pause boundaries marked; highlight the currently-selected array
  centered in the field box; show the ±30 mm usable-field square. A "step" control walks the
  `schedule`. Preview reuses the `pflm` engine functions directly so it can't drift.
- Run button → `python -m pflm.cli build ...` via QProcess.

---

## 8. Carry-over gotchas (do not relearn the hard way)

- **Auto-centering OFF** at the laser, always. A content-centered import once displaced a
  pass ~8 mm.
- **Calibration is machine-specific and volatile.** Don't trust any Singulation number
  (LASER_ZERO, GLOBAL_*_OFFSET, taught positions, axis signs). Re-measure on this rig.
- **Backside**: wafer is flipped; X is calibrated on the round OD, not the unreliable
  secondary flat. Mirror axis is a calibration (default X).
- **OneDrive blocks rmdir** (repo lives under a OneDrive path with spaces) — overwrite set
  files in place, never remove dirs.
- **Two Python versions**: prep 3.11+, laser PC 3.8.10 — keep `laser_pc/` 3.8-compatible
  (no `match`, no `X | Y` type unions at runtime, etc.).
- **WinLase GUI vs COM**: close the WinLase GUI before any build/armed run or COM wedges.
- **Empty-array trap**: a bbox with no pinfins still writes a placed file identical to a real
  one — flag `has_geometry=false` loudly.
- **KLayout iterator lifetime**: keep `Layout` objects alive while their `Region`s are used;
  don't mutate shapes mid-iteration.
- **DXF must go through ezdxf** for R2010/$INSUNITS; KLayout's DXF writer can't set them.
- **STOP is a controlled stop** (between arrays/mask-pauses), never an e-stop. Say so in the UI.
- **Reachable window** (post 2026-08 re-datum, asymmetric): the binding stage limit is
  `reachable_um` X ∈ [16236, 138529] µm, Y ∈ [−38140, 0] µm — the −38140 floor is a PIPE at the
  back (not the deeper hard stop) and the Y top is 0 (the old +6950 µm P3/P4 ceiling,
  `STAGE_Y_MAX_UM`, is retired as the laser-PC limit). The unrotated PFLM design needs a large
  positive stage-Y at the top row → design rotation exists specifically to keep every target
  inside this window. The laser-PC pre-flight refuses to move if any target is outside it.
- **Monotonic sweep**: exposure must proceed monotonically along the physical sweep axis
  (top design row first). Never interleave arrays across non-adjacent stripes — a completed,
  masked stripe must stay "behind" the sweep so later ejecta can't reach unmasked work.
- **Rotation is applied to geometry AND centers together**: the centered DXF carries the
  rotated features and the stage target uses `exposed_center_um`; if one is rotated and the
  other isn't, the pattern lands mirrored/rotated wrong. Keep them consistent in `plan.py`.
