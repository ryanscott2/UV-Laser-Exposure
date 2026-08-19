"""Prep orchestrator (§5.5): GDS -> set folder (plan.json + jobs/*.dxf + manifest).

Pipeline:
  read GDS -> detect_arrays (design frame) -> choose_rotation (extent-based) ->
  group_exposed_rows (physical rows = exposed-Y bands, top->bottom; never mixed) ->
  per array: count pinfins, clip_and_center WITH rotation, write jobs/<id>.dxf,
    record bbox_center_um (design) and exposed_center_um (rotated) ->
  build `schedule` from rows + within_row_stride (§2.2) ->
  write plan.json + manifest.csv + prep_log.txt.

Warns loudly (never silently drops) on empty arrays, geometry outside all
bboxes, and stage-infeasible sets. Never rmdir (OneDrive, §8): overwrite in place.
No hardware imports.
"""

from __future__ import annotations

import csv
import dataclasses
import json
from pathlib import Path

try:
    import pya  # type: ignore
except ImportError:
    import klayout.db as pya

from . import schema_version
from .arrays import (
    ALIGN_TOL_UM,
    choose_rotation,
    clamp_center,
    count_pinfins_in,
    detect_align_marks,
    detect_arrays,
    detect_arrays_from_pinfins,
    group_exposed_rows,
    rotate_point_um,
    rotation_feasibility,
)

# Fixed etch recipe for the alignment-mark layer (etched last, for downstream steps).
ALIGN_ETCH = {"passes": 15, "speed_mm_s": 400.0, "fill_style": "crosshatch",
              "fill_angles_deg": [0, 90], "hatch_mm": 0.01}
from .centering import (
    array_circles,
    clip_and_center,
    dead_space_rects_um,
    fits_field,
    region_bbox_um,
)
from .dxf_writer import write_circles_r2010, write_dxf_r2010, write_rects_r2010

# Dead-space ablation recipe (etched FIRST, per chip, to make the chips mate cleanly).
# Removes the cell footprint minus the pin-field box; nothing inside the pin box is
# touched. Faster + shallower than the pin etch; operator-tunable numbers.
DEAD_SPACE_ETCH = {"passes": 10, "speed_mm_s": 1000.0, "fill_style": "bidirectional",
                   "fill_angles_deg": [90], "hatch_mm": 0.02}
from .layers import layer_indices_for_spec, parse_layer_spec, read_layout

# Machine constants (§2, §2.1).
USABLE_FIELD_HALF_UM = 30_000
QUALIFIED_FIELD_UM = 54_000
FULL_FIELD_UM = 78_485
WAFER_DIAMETER_MM = 100.0
WAFER_RADIUS_UM = 50_000
TRAVEL_UM = (126_000, 76_000)
STAGE_Y_MAX_UM = 6_950
# Singulation's baked-in slicer global offset (split_klayout.py), retained by
# request as the prep default. Field-frame nudge applied AFTER rotation/centering.
# UNVERIFIED for this rig — re-measure; keep the laser-PC exposure_calibration
# global_offset at 0 to avoid double-correction (§8).
GLOBAL_OFFSET_UM = (-3447.0, 460.0)


def array_id(row_index: int, col_index: int) -> str:
    return f"r{row_index:02d}c{col_index:02d}"


def build_schedule(rows, within_row_stride=2) -> list:
    """The §2.2 group + mask algorithm.

    Within each row, group arrays by ``col_index % within_row_stride`` (phase 0 =
    cols 0,2,4...; phase 1 = cols 1,3,5...). Emit each non-empty group's exposes in
    col order, inserting one ``mask`` pause **before any subsequent expose** — so
    #masks = (#non-empty groups) - 1, with no trailing mask.
    ``within_row_stride=1`` degrades to expose-whole-row-then-mask.
    """
    stride = max(int(within_row_stride), 1)
    schedule: list = []
    step = 0
    prev = None  # (row_index, phase, count) of the last emitted group
    for row in rows:
        ncols = len(row.arrays)
        for phase in range(stride):
            cols = [c for c in range(ncols) if c % stride == phase]
            if not cols:
                continue
            if prev is not None:
                schedule.append({
                    "step": step,
                    "action": "mask",
                    "label": _mask_label(*prev),
                })
                step += 1
            for c in cols:
                schedule.append({
                    "step": step,
                    "action": "expose",
                    "array_id": array_id(row.row_index, c),
                    "row_index": row.row_index,
                    "phase": phase,
                })
                step += 1
            prev = (row.row_index, phase, len(cols))
    return schedule


def _mask_label(row_index: int, phase: int, count: int) -> str:
    letter = chr(ord("A") + phase)
    noun = "array" if count == 1 else "arrays"
    return (f"row {row_index} phase {letter} complete "
            f"({count} {noun}) -- mask, then Resume")


def _clear_existing_dxf(jobs_dir: Path) -> None:
    """Remove stale per-array DXFs (file unlink only; never rmdir — OneDrive, §8)."""
    if jobs_dir.is_dir():
        for old in jobs_dir.glob("*.dxf"):
            try:
                old.unlink()
            except OSError:
                pass


def build_set(gds_path, set_dir, *, pinfin="3/0", bbox="4/0", align="5/0",
              backside=True, rotation_deg="auto", within_row_stride=2,
              travel_um=TRAVEL_UM, stage_y_max_um=STAGE_Y_MAX_UM,
              global_offset_um=GLOBAL_OFFSET_UM, row_tol_um=None,
              params_csv=None, pin_mode="polygon", expose_align=True,
              align_tol_um=ALIGN_TOL_UM, ablate_dead_space=False, cell="4/0",
              dead_space_etch=None, dead_space_wash=True,
              overwrite_in_place=True) -> dict:
    """Full prep pipeline; returns the plan dict and writes the set folder.

    ``params_csv`` (optional): a design manifest with per-chip etch params keyed
    by exposed position (columns exposed_x_um, exposed_y_um, type, passes,
    speed_mm_s, fill_style, fill_angles_deg, hatch_mm). Each detected array is
    joined to it by nearest exposed center, so plan.json carries per-array
    passes + crosshatch for the laser-PC job builder."""
    gds_path = Path(gds_path)
    set_dir = Path(set_dir)
    jobs_dir = set_dir / "jobs"
    set_dir.mkdir(parents=True, exist_ok=True)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    if overwrite_in_place:
        _clear_existing_dxf(jobs_dir)

    warnings: list[str] = []
    log: list[str] = []

    def note(msg: str) -> None:
        log.append(msg)

    def warn(msg: str) -> None:
        warnings.append(msg)
        log.append("WARNING: " + msg)
        print("WARNING: " + msg)

    # ---- read + detect (design frame) ----------------------------------------
    layout = read_layout(gds_path)  # keep alive while Regions are used (§8)
    dbu = layout.dbu
    note(f"Input: {gds_path}")
    note(f"Layout DBU (um): {dbu}")
    note(f"Top cells: {', '.join(c.name for c in layout.top_cells())}")

    boxes = detect_arrays(layout, bbox)
    detection = f"bbox layer {bbox}"
    if not boxes:
        warn(f"no bbox shapes on layer {bbox}; falling back to pinfin clustering")
        boxes = detect_arrays_from_pinfins(layout, pinfin)
        detection = f"pinfin clustering on {pinfin} (no bbox layer)"
    note(f"Array detection: {detection}; {len(boxes)} array box(es) found")
    if not boxes:
        warn("no arrays detected at all — the plan will be empty")

    etch = _load_params_csv(params_csv) if params_csv else None
    if params_csv:
        note(f"Etch params: {params_csv} ({len(etch or {})} rows)")
        if not etch:
            warn(f"params file {params_csv} loaded no rows")

    # ---- rotation choice (extent-based, to fit the stage) (§2.1) -------------
    if isinstance(rotation_deg, str) and rotation_deg.strip().lower() == "auto":
        chosen = choose_rotation(boxes, travel_um=tuple(travel_um),
                                 stage_y_max_um=stage_y_max_um)
        deg = int(chosen["deg"])
        note(f"design_rotation_deg = auto -> {deg} (feasible={chosen['feasible']})")
    else:
        deg = int(rotation_deg) % 360
        chosen = rotation_feasibility(boxes, deg, travel_um=tuple(travel_um),
                                      stage_y_max_um=stage_y_max_um)
        note(f"design_rotation_deg = {deg} (explicit; feasible={chosen['feasible']})")

    # ---- group into PHYSICAL rows in the EXPOSED frame (never mix rows, §2.2)-
    rows = group_exposed_rows(boxes, deg, row_tol_um=row_tol_um)
    note(f"Grouped {len(boxes)} arrays into {len(rows)} physical row(s) "
         f"(exposed-Y bands, top->bottom); sizes = {[len(r.arrays) for r in rows]}")

    stage = {
        "travel_um": [int(travel_um[0]), int(travel_um[1])],
        "stage_y_max_um": int(stage_y_max_um),
        "sweep_axis": chosen["sweep_axis"],            # within a row (stage-X)
        "row_advance_axis": chosen["row_advance_axis"],  # between rows (stage-Y)
        "sweep_span_um": round(chosen["sweep_span_um"], 3),
        "row_advance_span_um": round(chosen["row_advance_span_um"], 3),
        "max_stage_y_um": round(chosen["max_stage_y_um"], 3),
        "feasible": bool(chosen["feasible"]),
        "notes": ("within-row sweep rides stage-X; rows advance along stage-Y "
                  f"(kept at/below the +{stage_y_max_um} um ceiling)"),
    }
    if not stage["feasible"]:
        warn("stage.feasible=false — rotated spans do not fit travel/ceiling; "
             "the laser-PC pre-flight will refuse to run this set")

    # ---- per-array centering + DXF -------------------------------------------
    total_pinfins_in_boxes = 0
    rows_out: list[dict] = []
    array_records: dict[str, dict] = {}
    for row in rows:
        arrays_out: list[dict] = []
        for col_index, box in enumerate(row.arrays):
            aid = array_id(row.row_index, col_index)
            job_rel = f"jobs/{aid}.dxf"
            if pin_mode == "circle":
                # efficient: round pins -> CIRCLE entities (dense arrays; §perf)
                circles, cbb = array_circles(
                    layout, pinfin, box.bbox_um,
                    design_rotation_deg=deg, global_offset_um=tuple(global_offset_um))
                count = len(circles)
                write_circles_r2010(jobs_dir / f"{aid}.dxf", circles)
                fits = (cbb is None) or (max(abs(cbb[0]), abs(cbb[1]),
                                             abs(cbb[2]), abs(cbb[3])) <= USABLE_FIELD_HALF_UM)
            else:
                count = count_pinfins_in(layout, pinfin, box.bbox_um)
                region = clip_and_center(
                    layout, pinfin, box.bbox_um,
                    design_rotation_deg=deg, global_offset_um=tuple(global_offset_um))
                write_dxf_r2010(jobs_dir / f"{aid}.dxf", region, dbu)
                fits = fits_field(region, USABLE_FIELD_HALF_UM, dbu=dbu)
            total_pinfins_in_boxes += count
            has_geom = count > 0

            bbox_center = [round(box.center_um[0], 3), round(box.center_um[1], 3)]
            exposed_center = list(rotate_point_um(box.center_um, deg))
            exposed_center = [round(exposed_center[0], 3), round(exposed_center[1], 3)]

            if not has_geom:
                warn(f"{aid}: bbox has no pinfin shapes (has_geometry=false) — "
                     "a placed empty job looks identical to a real one at the machine")
            if not fits:
                warn(f"{aid}: centered geometry exceeds +/-{USABLE_FIELD_HALF_UM} um "
                     "usable field (fits_field=false)")

            rec = {
                "array_id": aid,
                "row_index": row.row_index,
                "col_index": col_index,
                "bbox_center_um": bbox_center,
                "exposed_center_um": exposed_center,
                "bbox_um": [round(v, 3) for v in box.bbox_um],
                "polygon_count": count,
                "has_geometry": has_geom,
                "fits_field": bool(fits),
                "job_dxf": job_rel,
            }
            if etch is not None:
                key, dist = _nearest_param(etch, exposed_center[0], exposed_center[1])
                if key is not None and dist <= 1000.0:
                    p = etch[key]
                    rec["type"] = p["type"]
                    rec["etch"] = {"passes": p["passes"], "speed_mm_s": p["speed_mm_s"],
                                   "fill_style": p["fill_style"],
                                   "fill_angles_deg": p["fill_angles_deg"],
                                   "hatch_mm": p["hatch_mm"]}
                else:
                    rec["type"] = None
                    rec["etch"] = None
                    warn(f"{aid}: no etch params within 1 mm of exposed center "
                         f"{exposed_center} (nearest dist {dist:.0f} um)")
            arrays_out.append(rec)
            array_records[aid] = rec
        rows_out.append({
            "row_index": row.row_index,
            "exposed_y_center_um": round(row.y_center_um, 3),
            "arrays": arrays_out,
        })

    # geometry accounting: any pinfin shape outside every bbox is dropped silently
    total_pinfins = _count_all(layout, pinfin)
    dropped = total_pinfins - total_pinfins_in_boxes
    note(f"Pinfin shapes: {total_pinfins} total, {total_pinfins_in_boxes} inside array bboxes")
    if dropped > 0:
        warn(f"{dropped} pinfin shape(s) lie outside every array bbox and are not "
             "exposed by any job (dropped geometry)")

    # ---- schedule (§2.2) ------------------------------------------------------
    schedule = build_schedule(rows, within_row_stride=within_row_stride)
    n_expose = sum(1 for s in schedule if s["action"] == "expose")
    n_mask = sum(1 for s in schedule if s["action"] == "mask")
    note(f"Schedule: {n_expose} expose step(s), {n_mask} mask pause(s)")
    # carry per-array passes onto the expose steps (each array = one job, run to completion)
    for s in schedule:
        if s["action"] == "expose":
            e = array_records[s["array_id"]].get("etch")
            s["passes"] = (e["passes"] if e else None)

    # ---- PRE-PHASE: dead-space ablation (before the pinfins, per chip) --------
    # Ablate the chip footprint (cell layer) MINUS the pin-field box so the chips mate
    # cleanly; nothing inside the pin-field box is touched. One continuous phase, no
    # masks between chips (the field is smaller than the wafer, so each chip is centered
    # in turn and its dead space removed), then a single wash/clean pause before pinfins.
    if ablate_dead_space:
        dse = dead_space_etch or DEAD_SPACE_ETCH
        cell_boxes = detect_arrays(layout, cell)
        note(f"Dead-space ablation: {len(cell_boxes)} cell footprint(s) on {cell}, "
             f"{dse['passes']} passes @ {dse['speed_mm_s']:.0f} mm/s, crosshatch "
             f"{dse['fill_angles_deg']} deg; wash-after={dead_space_wash}")
        if not cell_boxes:
            warn(f"no cell footprints on layer {cell}; dead-space phase skipped")
        pin_order = [s["array_id"] for s in schedule if s["action"] == "expose"]
        ds_steps: list = []
        ds_row = {"row_index": -1, "exposed_y_center_um": 0.0, "arrays": []}
        for aid in pin_order:
            rec0 = array_records.get(aid)
            if rec0 is None:
                continue
            bcx, bcy = rec0["bbox_center_um"]
            cellbox = min(cell_boxes,
                          key=lambda b: abs(b.center_um[0] - bcx) + abs(b.center_um[1] - bcy),
                          default=None)
            if cellbox is None:
                warn(f"{aid}: no {cell} cell footprint near chip center; dead-space skipped")
                continue
            rects = dead_space_rects_um(
                cellbox.bbox_um, tuple(rec0["bbox_um"]),
                design_rotation_deg=deg, global_offset_um=tuple(global_offset_um))
            ds_aid = "ds_" + aid
            write_rects_r2010(jobs_dir / f"{ds_aid}.dxf", rects)
            dsrec = {
                "array_id": ds_aid, "type": "deadspace",
                "row_index": -1, "col_index": rec0["col_index"],
                "bbox_center_um": rec0["bbox_center_um"],
                "exposed_center_um": rec0["exposed_center_um"],
                "bbox_um": [round(v, 3) for v in cellbox.bbox_um],
                "polygon_count": len(rects),
                "has_geometry": bool(rects),
                "fits_field": True,
                "etch": dict(dse),
                "job_dxf": f"jobs/{ds_aid}.dxf",
            }
            array_records[ds_aid] = dsrec
            ds_row["arrays"].append(dsrec)
            ds_steps.append({"step": 0, "action": "expose", "array_id": ds_aid,
                             "row_index": -1, "phase": 0, "type": "deadspace",
                             "passes": dse["passes"]})
        if ds_steps:
            if dead_space_wash:
                ds_steps.append({"step": 0, "action": "mask",
                                 "label": "dead-space removal complete -- wash + clean, then Resume"})
            schedule = ds_steps + schedule       # dead-space phase runs FIRST
            rows_out.insert(0, ds_row)
            for i, s in enumerate(schedule):     # renumber the whole schedule
                s["step"] = i
            note(f"Dead-space phase prepended: {len(ds_row['arrays'])} ablation step(s)"
                 + (" + 1 wash pause" if dead_space_wash else ""))

    # ---- FINAL PHASE: etch the alignment-mark layer (§ align) ----------------
    # After all pinfins (+ one wash/mask pause), etch each fiducial. Marks only need to be
    # within +/-align_tol_um of field center (not perfectly centered): a mark past the stage
    # envelope is centered on the closest reachable point and lands off-center in the field
    # (still at its true wafer location). Etched last; used to align downstream process steps.
    if expose_align:
        marks = detect_align_marks(layout, align)
        if marks:
            note(f"Alignment-mark etch: {len(marks)} mark(s), {ALIGN_ETCH['passes']} passes "
                 f"crosshatch {ALIGN_ETCH['fill_angles_deg']} deg, +/-{align_tol_um/1000:.0f} mm tol")
            step = len(schedule)
            align_row = {"row_index": len(rows_out), "exposed_y_center_um": 0.0, "arrays": []}
            if any(s["action"] == "expose" for s in schedule):
                schedule.append({"step": step, "action": "mask",
                                 "label": "pinfins complete -- wash + mask, then etch alignment marks"})
                step += 1
            for mi, mb in enumerate(marks):
                aid = "align%02d" % mi
                exposed_mark = rotate_point_um(mb.center_um, deg)
                eff, off = clamp_center(exposed_mark, travel_um=tuple(travel_um),
                                        stage_y_max_um=stage_y_max_um)
                region = clip_and_center(layout, align, mb.bbox_um, design_rotation_deg=deg,
                                         global_offset_um=tuple(global_offset_um),
                                         center_override=eff)
                write_dxf_r2010(jobs_dir / (aid + ".dxf"), region, dbu)
                within = max(abs(off[0]), abs(off[1])) <= align_tol_um
                if not within:
                    warn(f"{aid}: alignment mark lands {max(abs(off[0]), abs(off[1]))/1000:.1f} mm "
                         f"off field center (> {align_tol_um/1000:.0f} mm tol) -- unreachable")
                rec = {
                    "array_id": aid, "type": "align",
                    "row_index": align_row["row_index"], "col_index": mi,
                    "bbox_center_um": [round(mb.center_um[0], 3), round(mb.center_um[1], 3)],
                    "exposed_center_um": [round(eff[0], 3), round(eff[1], 3)],
                    "field_offset_um": [round(off[0], 3), round(off[1], 3)],
                    "bbox_um": [round(v, 3) for v in mb.bbox_um],
                    "polygon_count": int(region.count()),
                    "has_geometry": bool(region.count() > 0),
                    "fits_field": bool(within),
                    "etch": dict(ALIGN_ETCH),
                    "job_dxf": "jobs/%s.dxf" % aid,
                }
                array_records[aid] = rec
                align_row["arrays"].append(rec)
                schedule.append({"step": step, "action": "expose", "array_id": aid,
                                 "row_index": align_row["row_index"], "phase": 0,
                                 "passes": ALIGN_ETCH["passes"]})
                step += 1
            if align_row["arrays"]:
                rows_out.append(align_row)
            n_expose = sum(1 for s in schedule if s["action"] == "expose")
            n_mask = sum(1 for s in schedule if s["action"] == "mask")

    align_marks = _align_marks_um(layout, align)

    # final tallies over the complete schedule (dead-space + pinfins + align)
    n_expose = sum(1 for s in schedule if s["action"] == "expose")
    n_mask = sum(1 for s in schedule if s["action"] == "mask")

    plan = {
        "schema_version": schema_version,
        "set_name": set_dir.name,
        "source_gds": gds_path.name,
        "dbu_um": dbu,
        "layers": {"pinfin": str(pinfin), "bbox": str(bbox), "align": str(align)},
        "wafer": {"diameter_mm": WAFER_DIAMETER_MM, "radius_um": WAFER_RADIUS_UM},
        "field": {"usable_half_um": USABLE_FIELD_HALF_UM,
                  "qualified_um": QUALIFIED_FIELD_UM, "full_um": FULL_FIELD_UM},
        "backside": bool(backside),
        "design_rotation_deg": deg,
        "exposure_order": "top_to_bottom",
        "mask_strategy": {"within_row_stride": int(within_row_stride),
                          "mask_between_groups": True},
        "etch": ({"source": Path(params_csv).name, "fill_style": "crosshatch",
                  "hatch_mm": 0.01, "note": "per-array passes + fill_angles under rows[].arrays[].etch"}
                 if etch else None),
        "align_marks_um": align_marks,
        "stage": stage,
        "rows": rows_out,
        "schedule": schedule,
    }

    # ---- write set folder -----------------------------------------------------
    (set_dir / "plan.json").write_text(
        json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    _write_manifest(set_dir / "manifest.csv", schedule, array_records)
    _write_log(set_dir / "prep_log.txt", log, warnings)

    print(f"Wrote set '{set_dir.name}': {n_expose} arrays, {n_mask} mask pauses -> {set_dir}")
    if warnings:
        print(f"  {len(warnings)} warning(s) — see prep_log.txt")
    return plan


def _count_all(layout, pinfin) -> int:
    spec = parse_layer_spec(pinfin)
    indices = layer_indices_for_spec(layout, spec)
    total = 0
    for top in layout.top_cells():
        for index in indices:
            it = top.begin_shapes_rec(index)
            while not it.at_end():
                total += 1
                it.next()
    return total


def _align_marks_um(layout, align) -> list:
    spec = parse_layer_spec(align)
    indices = layer_indices_for_spec(layout, spec)
    marks: list = []
    for top in layout.top_cells():
        for index in indices:
            it = top.begin_shapes_rec(index)
            while not it.at_end():
                box = it.shape().bbox().transformed(it.trans())
                cx = layout.dbu * (box.left + box.right) / 2.0
                cy = layout.dbu * (box.bottom + box.top) / 2.0
                marks.append([round(cx, 3), round(cy, 3)])
                it.next()
    return marks


def _load_params_csv(path):
    """Design manifest -> {(exposed_x_um,exposed_y_um): {type,passes,speed_mm_s,fill_style,fill_angles_deg,hatch_mm}}."""
    if not path or not Path(path).is_file():
        return {}
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                key = (round(float(r["exposed_x_um"])), round(float(r["exposed_y_um"])))
                out[key] = {
                    "type": r.get("type", ""),
                    "passes": int(float(r["passes"])),
                    "speed_mm_s": float(r["speed_mm_s"]),
                    "fill_style": r.get("fill_style", "crosshatch"),
                    "fill_angles_deg": [float(a) for a in str(r["fill_angles_deg"]).replace(",", "/").split("/") if a != ""],
                    "hatch_mm": float(r.get("hatch_mm", 0.01)),
                }
            except (KeyError, ValueError):
                continue
    return out


def _nearest_param(etch, ex, ey):
    """Nearest manifest key to (ex,ey); returns (key, manhattan_distance_um) or (None, inf)."""
    best, bestd = None, float("inf")
    for k in etch:
        d = abs(k[0] - ex) + abs(k[1] - ey)
        if d < bestd:
            best, bestd = k, d
    return best, bestd


def _write_manifest(path: Path, schedule, records) -> None:
    columns = [
        "step", "phase", "array_id", "type", "row_index", "col_index",
        "bbox_center_x_um", "bbox_center_y_um",
        "exposed_center_x_um", "exposed_center_y_um",
        "bbox_left_um", "bbox_bottom_um", "bbox_right_um", "bbox_top_um",
        "polygon_count", "has_geometry", "fits_field",
        "passes", "speed_mm_s", "fill_style", "fill_angles_deg", "hatch_mm", "job_dxf",
    ]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for step in schedule:
            if step["action"] != "expose":
                continue
            rec = records[step["array_id"]]
            e = rec.get("etch") or {}
            writer.writerow({
                "step": step["step"],
                "phase": step["phase"],
                "array_id": rec["array_id"],
                "type": rec.get("type", ""),
                "row_index": rec["row_index"],
                "col_index": rec["col_index"],
                "bbox_center_x_um": rec["bbox_center_um"][0],
                "bbox_center_y_um": rec["bbox_center_um"][1],
                "exposed_center_x_um": rec["exposed_center_um"][0],
                "exposed_center_y_um": rec["exposed_center_um"][1],
                "bbox_left_um": rec["bbox_um"][0],
                "bbox_bottom_um": rec["bbox_um"][1],
                "bbox_right_um": rec["bbox_um"][2],
                "bbox_top_um": rec["bbox_um"][3],
                "polygon_count": rec["polygon_count"],
                "has_geometry": rec["has_geometry"],
                "fits_field": rec["fits_field"],
                "passes": e.get("passes", ""),
                "speed_mm_s": e.get("speed_mm_s", ""),
                "fill_style": e.get("fill_style", ""),
                "fill_angles_deg": "/".join(str(a) for a in e.get("fill_angles_deg", [])),
                "hatch_mm": e.get("hatch_mm", ""),
                "job_dxf": rec["job_dxf"],
            })


def _write_log(path: Path, log, warnings) -> None:
    lines = list(log)
    lines.append("")
    if warnings:
        lines.append(f"{len(warnings)} warning(s):")
        lines.extend("  - " + w for w in warnings)
    else:
        lines.append("No warnings.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
