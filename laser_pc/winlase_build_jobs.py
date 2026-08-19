"""Build WinLase Pro jobs from a built PFLM exposure set: one .wlj per array.

For each array in a set (discovered from `plan.json` + `jobs/<array_id>.dxf`), this
creates ONE WinLase job holding that array's SINGLE vector graphic -- the centered
pinfin/heater pattern -- positions it at its true field coordinates (the DXF origin is
the field center, so the array's own center lands on (0,0) and auto-centering stays
effectively OFF), sets the 0.01 mm parallel fill @ 0 deg, one pass, mark-fill on /
outline off, verifies the laser profile read-only, and saves a `.wlj`. That replaces
the per-file drag, drop, and settings the operator does by hand for every array with a
single command.

Unlike the Singulation build (which seated a Horizontal.dxf @ 0 deg and a Vertical.dxf
@ 90 deg into ONE job per jig station P1..P4), a PFLM array is a single graphic, so
this builds ONE graphic per job and ONE job per array. Work is discovered from the set
folder's `plan.json` and `jobs/<array_id>.dxf`, not from P1..P4 station folders.

It drives WinLase Professional's COM Automation server -- `CreateObject("Winlase.
Automate")`, the `IAutomate` interface documented in the *WinLase Automation Server
Reference Manual* (Lanmark Controls, Rev 8.8). The functions used, with their manual
signatures:

    AttachToMarker() / ReleaseMarker()
    GetScanCardCount() -> count
    GetLensCalFactor(card, head) -> bits/mm            (converts mm <-> field bits)
    GetObjProfile(objIndex, card) -> (MarkSpeed, ..., LaserPower%, ..., FreqKHz, ...)
    SetObjProfile(objIndex, card, *values)             (only the speed field is written)
    NewJob(0, fileName) -> jobIndex                      (leading [out] index -> placeholder 0)
    NewVectorGraphic(0, objName, fileName) -> objIndex   (imports *.dxf directly)
    GetObjRect(objIndex) -> (Left, Top, Right, Bottom) in field bits
    OffsetObj(objIndex, dxBits, dyBits)
    SetObjFill(objIndex, spacingBits, slope1Deg, slope2Deg, style)   style 0 = parallel
    SetObjMarkFillFlag(objIndex, 1) / SetObjMarkOutlineFlag(objIndex, 0)
    SetObjNumPasses(objIndex, 1)
    IsObjOutOfBounds(objIndex) -> flag
    GetObjCount() -> count
    SaveJobToFile(fileName, appVersion, date, appName, company)
    CloseJob(jobIndex)

The field is a Cartesian grid in "bits": (0,0) at the field center, +/-32768 at the
corners. GetLensCalFactor gives bits/mm for the loaded lens, so a mark at X mm lands
at round(X * bits_per_mm) bits. Placement here is defensive: after each import it
reads the object's actual rect and OFFSETS it so the object's center matches the
DXF's own (field-centered) bounding-box center -- correct whether the import
preserved coordinates or auto-centered the graphic.

RUN THIS ON THE LASER PC. It needs WinLase Pro + its dongle + a scan card + a loaded
lens cal. Close the WinLase GUI first: the GUI and the automation server cannot hold
the marker library at the same time. This script only BUILDS and SAVES jobs -- it
never downloads or marks.

This script sets the mark speed to 400 mm/s (the WinLase default profile is 1000 mm/s)
by writing ONLY the speed field of Profile 0: it reads the profile, changes the speed,
writes it back, then reads it again and verifies laser power and frequency are unchanged
(aborting the build if not). It never sets laser power or frequency itself; those, the
delays, and jump settings all come from WinLase's default profile.

Usage:
    python laser_pc/winlase_build_jobs.py output/sets/081026_PFLM_Heaters
    python laser_pc/winlase_build_jobs.py output/sets/081026_PFLM_Heaters --dry-run
    python laser_pc/winlase_build_jobs.py <set> --verify   # build first array in memory,
                                                          # report placement, do not save
    python laser_pc/winlase_build_jobs.py <setA> <setB> ... # several wafers at once
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import etch_params  # editable per-type etch table (passes + crosshatch angles), same dir

# --- Settings (from the exposure operating procedure) ----------------------------
FILL_SPACING_MM = 0.01             # default hatch / pass width (0.01 mm); per-array override from plan
FILL_STYLE_PARALLEL = 0            # SetObjFill style: 0 = parallel lines (single angle)
FILL_STYLE_CROSSHATCH = 2          # SetObjFill style: crosshatch (two-angle fill).
                                   # Confirmed correct on the machine (2026-08-18).
DEFAULT_FILL_ANGLES_DEG = (0.0, 90.0)
FILL_ANGLE_DEG = 0                 # fallback single angle if a plan array carries no etch params
NUM_PASSES = 1                     # fallback if a job carries no pass count. Normally each job's
                                   # per-array etch PASSES are set ON THE OBJECT so WinLase runs them
                                   # all in ONE mark (alternating the crosshatch angles). A single
                                   # mark only lays the first angle, so passes must NOT be an outer loop.
MARK_SPEED_MM_S = 400.0            # written onto Profile 0 (WinLase default profile is 1000 mm/s);
SPEED_TOLERANCE_MM_S = 10.0        # ONLY the speed is written -- power/frequency are verified unchanged

# The laser profile the operator confirmed in WinLase (Vector Graphic -> Properties ->
# Profile): power 100 %, frequency 30 kHz, mark speed 400 mm/s. The build never WRITES
# power or frequency; it reads them back and REFUSES to save a job whose profile does
# not match these -- so a job can never be saved with the wrong laser settings.
EXPECTED_LASER_POWER_PCT = 100.0
EXPECTED_FREQ_KHZ = 30.0
POWER_TOLERANCE_PCT = 0.5
FREQ_TOLERANCE_KHZ = 0.1

FIELD_BIT_LIMIT = 32768            # +/- field half in bits

APP_NAME = "PFLM winlase_build_jobs"
APP_VERSION = "1.0"
COMPANY = "Stanford UV Laser PFLM"


# --- DXF bounds (dependency-free; the laser PC need not have klayout) -------------
def dxf_bounds_mm(path):
    # type: (Path) -> tuple
    """Return (xmin, ymin, xmax, ymax) in mm over the ENTITIES section.

    Handles the two shapes these ezdxf files use, in mm:
      * CIRCLE (round pins): center = group 10 (x) / 20 (y), radius = group 40;
        the extent contributed is center +/- radius.
      * LWPOLYLINE (any polygonal marks): vertices are group 10 / 20 pairs.
    The header's $EXTMIN/$EXTMAX are sentinels, so extents are read off the
    entities. Only the ENTITIES section is scanned (later sections also carry
    10/20 codes and would corrupt the box).
    """
    lines = [ln.strip() for ln in path.read_text(errors="strict").splitlines()]
    pairs = list(zip(lines[0::2], lines[1::2]))  # DXF = code/value pairs on alternating lines
    xs = []
    ys = []
    in_entities = False
    etype = None
    cx = cy = None
    for code, value in pairs:
        if value == "ENTITIES":
            in_entities = True
            continue
        if in_entities and value == "ENDSEC":
            break
        if not in_entities:
            continue
        if code == "0":                      # start of a new entity
            etype = value
            cx = cy = None
            continue
        if etype == "CIRCLE":
            if code == "10":
                cx = float(value)
            elif code == "20":
                cy = float(value)
            elif code == "40" and cx is not None and cy is not None:
                r = float(value)
                xs.extend((cx - r, cx + r))
                ys.extend((cy - r, cy + r))
        else:                                # LWPOLYLINE / POLYLINE: 10/20 are vertices
            if code == "10":
                xs.append(float(value))
            elif code == "20":
                ys.append(float(value))
    if not xs or not ys:
        raise ValueError("No CIRCLE/LWPOLYLINE geometry found in %s" % path)
    return min(xs), min(ys), max(xs), max(ys)


# --- Job discovery (from plan.json + jobs/<array_id>.dxf) -------------------------
def load_plan(set_dir):
    # type: (Path) -> dict
    plan_path = set_dir / "plan.json"
    if not plan_path.is_file():
        raise SystemExit("no plan.json under %s (is this a built set folder?)" % set_dir)
    with plan_path.open(encoding="utf-8") as stream:
        return json.load(stream)


def discover_jobs(set_dir):
    # type: (Path) -> list
    """One entry per array in the set's plan.json that has a centered DXF present.

    Arrays are read from plan['rows'] (each row carries its arrays), preserving the
    plan's ordering. Each entry names the array's single centered DXF, and the output
    job name is `<set>_<array_id>` (per the ARCHITECTURE set-folder contract).
    """
    plan = load_plan(set_dir)
    jobs = []
    for row in plan.get("rows", []):
        for array in row.get("arrays", []):
            array_id = array["array_id"]
            rel = array.get("job_dxf") or ("jobs/%s.dxf" % array_id)
            dxf = (set_dir / rel)
            if not dxf.is_file():
                print("  ! %s: missing DXF %s; skipped" % (array_id, dxf))
                continue
            etch = array.get("etch") or {}
            jobs.append({
                "array_id": array_id,
                "type": array.get("type"),
                "row_index": array.get("row_index"),
                "col_index": array.get("col_index"),
                "has_geometry": bool(array.get("has_geometry", True)),
                "polygon_count": array.get("polygon_count"),
                "passes": etch.get("passes"),                 # applied at RUN time (expose_wafer)
                "fill_angles_deg": etch.get("fill_angles_deg"),
                "hatch_mm": etch.get("hatch_mm"),
                "speed_mm_s": etch.get("speed_mm_s"),
                "dxf": dxf,
                "name": "%s_%s" % (set_dir.name, array_id),
            })
    return jobs


# --- COM session ------------------------------------------------------------------
class WinLaseSession(object):
    """Thin wrapper over the IAutomate COM interface.

    Every COM call is isolated here. win32com dynamic (late) binding reads the type
    library at runtime; documented [out] parameters come back as return values. A
    TRAILING [out] param (the usual case -- GetObjRect, GetObjProfile,
    GetLensCalFactor...) auto-returns as the result or a tuple. A LEADING [out] param
    (NewJob/NewVectorGraphic put the new index FIRST, per the manual) is not the
    retval, so it is passed as a placeholder 0 and comes back as the return.
    """

    def __init__(self):
        try:
            from win32com.client import dynamic
        except ImportError as exc:
            raise SystemExit(
                "pywin32 is not installed in this venv, so WinLase COM is unavailable.\n"
                "Install it offline (wheels are in venv\\wheels), then re-run:\n"
                "    pip install --no-index --find-links venv\\wheels pywin32\n"
                "    python venv\\Scripts\\pywin32_postinstall.py -install"
            )
        self.m = dynamic.Dispatch("Winlase.Automate")
        self.m.AttachToMarker()
        self.cards = int(self.m.GetScanCardCount())
        if self.cards < 1:
            raise RuntimeError(
                "GetScanCardCount() == 0: no scan card detected. Run on the laser PC "
                "with the card + dongle installed, or use --dry-run off the machine."
            )
        self.bits_per_mm = int(self.m.GetLensCalFactor(0, 0))
        if self.bits_per_mm <= 0:
            raise RuntimeError("GetLensCalFactor returned <= 0; is a lens cal loaded?")
        self._reported_speeds = set()   # print the read-back profile once per distinct mark speed

    def mm_to_bits(self, mm):
        # type: (float) -> int
        return int(round(mm * self.bits_per_mm))

    def build_job(self, job, save):
        # type: (dict, bool) -> list
        """Create one job holding the array's single graphic; return warning strings."""
        warnings = []
        out_path = job["out_path"]
        job_index = int(self.m.NewJob(0, str(out_path)))  # leading [out] index -> pass 0 placeholder

        if not job.get("has_geometry", True):
            warnings.append(
                "%s: plan flags has_geometry=false -- this array has NO pinfin shapes; "
                "the job will look identical to a real one at the machine" % job["array_id"])

        dxf = job["dxf"]
        xmin, ymin, xmax, ymax = dxf_bounds_mm(dxf)
        want_cx = self.mm_to_bits((xmin + xmax) / 2.0)
        want_cy = self.mm_to_bits((ymin + ymax) / 2.0)

        obj = int(self.m.NewVectorGraphic(0, job["array_id"], str(dxf)))

        left, top, right, bottom = (float(v) for v in self.m.GetObjRect(obj))
        got_cx = (left + right) / 2.0
        got_cy = (top + bottom) / 2.0
        self.m.OffsetObj(obj, int(round(want_cx - got_cx)), int(round(want_cy - got_cy)))

        # Size sanity: imported width/height should equal the DXF mm size * bits/mm.
        want_w = self.mm_to_bits(xmax - xmin)
        want_h = self.mm_to_bits(ymax - ymin)
        got_w, got_h = abs(right - left), abs(top - bottom)
        tol = max(2, int(0.02 * max(want_w, want_h, 1)))
        if abs(got_w - want_w) > tol or abs(got_h - want_h) > tol:
            warnings.append(
                "%s: imported size %.0fx%.0f bits != expected %dx%d "
                "(unexpected import scaling?)" % (job["array_id"], got_w, got_h, want_w, want_h))

        hatch_mm = float(job.get("hatch_mm") or FILL_SPACING_MM)
        spacing_bits = self.mm_to_bits(hatch_mm)
        if spacing_bits < 1:
            spacing_bits = 1
            warnings.append(
                "%s: %.4f mm rounds below 1 bit at %d bits/mm; fill spacing set to 1 bit"
                % (job["array_id"], hatch_mm, self.bits_per_mm))
        # Per-array crosshatch: two fill angles from the plan (square 0/90, hex -30/+30).
        angles = job.get("fill_angles_deg") or list(DEFAULT_FILL_ANGLES_DEG)
        a1 = float(angles[0])
        a2 = float(angles[1]) if len(angles) > 1 else a1
        if abs(a2 - a1) > 1e-6:
            self.m.SetObjFill(obj, spacing_bits, a1, a2, FILL_STYLE_CROSSHATCH)
        else:
            self.m.SetObjFill(obj, spacing_bits, a1, a1, FILL_STYLE_PARALLEL)
        self.m.SetObjMarkFillFlag(obj, 1)
        self.m.SetObjMarkOutlineFlag(obj, 0)
        # Bake the per-array pass count onto the object so WinLase runs all passes in one
        # mark (alternating the crosshatch angles). expose_wafer also sets this from the
        # plan at run time, so editing plan.json passes takes effect without a rebuild.
        obj_passes = int(job.get("passes") or NUM_PASSES)
        self.m.SetObjNumPasses(obj, obj_passes)
        # Force the mark speed to 400 mm/s (the WinLase default profile is 1000).
        # Write ONLY the speed: read Profile 0, change just the speed field, write
        # it back (laser power, frequency, and delays are echoed unchanged), then
        # read again and VERIFY power (index 5) and frequency/T1 (index 9) did not
        # move. A corrupt round-trip aborts the build rather than alter the laser.
        speed_mm_s = float(job.get("speed_mm_s") or MARK_SPEED_MM_S)
        before = list(self.m.GetObjProfile(obj, 0))
        after = list(before)
        after[0] = speed_mm_s / 1000.0 * self.bits_per_mm  # mm/s -> bits/mSec
        self.m.SetObjProfile(obj, 0, *after)
        check = list(self.m.GetObjProfile(obj, 0))
        if (abs(float(check[5]) - float(before[5])) > 1e-3
                or abs(float(check[9]) - float(before[9])) > 1e-3):
            raise RuntimeError(
                "%s: profile write moved power/frequency (power %s->%s %%, "
                "freq %s->%s kHz) -- ABORTING build, no job saved"
                % (job["array_id"], before[5], check[5], before[9], check[9]))
        got_speed = float(check[0]) / self.bits_per_mm * 1000.0
        if abs(got_speed - speed_mm_s) > SPEED_TOLERANCE_MM_S:
            raise RuntimeError("%s: mark speed is %.0f mm/s after write, not %.0f"
                               % (job["array_id"], got_speed, speed_mm_s))

        # Absolute safety gate: the job's power/frequency must MATCH the confirmed
        # WinLase profile (100 %, 30 kHz), not merely be unchanged. Refuse to save
        # a job with the wrong laser settings.
        got_power = float(check[5])
        got_freq = float(check[9])
        if (abs(got_power - EXPECTED_LASER_POWER_PCT) > POWER_TOLERANCE_PCT
                or abs(got_freq - EXPECTED_FREQ_KHZ) > FREQ_TOLERANCE_KHZ):
            raise RuntimeError(
                "%s: laser profile is power %.3f %% / freq %.2f kHz, which does NOT "
                "match the required %.0f %% / %.2f kHz -- ABORTING build, no job saved"
                % (job["array_id"], got_power, got_freq,
                   EXPECTED_LASER_POWER_PCT, EXPECTED_FREQ_KHZ))
        if round(got_speed) not in self._reported_speeds:
            self._reported_speeds.add(round(got_speed))
            print("  laser profile read back from the job: power %.1f %%, freq %.2f kHz, "
                  "mark speed %.0f mm/s  [power/freq must match WinLase Properties]"
                  % (got_power, got_freq, got_speed))

        if int(self.m.IsObjOutOfBounds(obj)):
            warnings.append(
                "%s: object falls outside the markable field (needs |x|,|y| <= %d bits "
                "= %.1f mm)" % (job["array_id"], FIELD_BIT_LIMIT,
                                FIELD_BIT_LIMIT / self.bits_per_mm))

        count = int(self.m.GetObjCount())
        if count != 1:
            warnings.append("%s: job holds %d objects, expected 1" % (job["array_id"], count))

        if save:
            self.m.SaveJobToFile(str(out_path), APP_VERSION, _today(), APP_NAME, COMPANY)
        self.m.CloseJob(job_index)
        return warnings

    def close(self):
        try:
            self.m.ReleaseMarker()
        except Exception:
            pass


def _today():
    # type: () -> str
    from datetime import date
    return date.today().isoformat()


# --- Planning / dry run -----------------------------------------------------------
def print_plan(set_dir, jobs, out_dir):
    # type: (Path, list, Path) -> None
    print("\n%s: %d job(s) -> %s" % (set_dir.name, len(jobs), out_dir))
    for job in jobs:
        flag = "  (EMPTY: no geometry)" if not job.get("has_geometry", True) else ""
        typ = (" [%s]" % job["type"]) if job.get("type") else ""
        print("  %s.wlj%s%s" % (job["name"], typ, flag))
        xmin, ymin, xmax, ymax = dxf_bounds_mm(job["dxf"])
        cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
        angles = job.get("fill_angles_deg") or list(DEFAULT_FILL_ANGLES_DEG)
        ang = "/".join("%g" % a for a in angles)
        passes = job.get("passes")
        print("     crosshatch %s deg @ %s mm | %s passes @ %s mm/s | bbox "
              "[%.3f,%.3f]..[%.3f,%.3f] mm, center (%+.3f,%+.3f) mm"
              % (ang, job.get("hatch_mm") or FILL_SPACING_MM,
                 passes if passes is not None else "?",
                 job.get("speed_mm_s") or MARK_SPEED_MM_S,
                 xmin, ymin, xmax, ymax, cx, cy))


def main():
    # type: () -> int
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sets", nargs="+", type=Path,
                        help="one or more built set directories (each = one wafer)")
    parser.add_argument("--out-subdir", default="WinLaseJobs",
                        help="subfolder written inside each set (default: WinLaseJobs)")
    parser.add_argument("--etch-params", type=Path, default=None,
                        help="etch table JSON (passes + crosshatch angles per type); overrides "
                             "the plan's baked values by array type. Default: etch_params.json "
                             "next to this script if it exists, else the plan's values.")
    parser.add_argument("--dry-run", action="store_true",
                        help="parse and print the plan without loading WinLase (no COM)")
    parser.add_argument("--verify", action="store_true",
                        help="build the first array's job in memory and report placement, do not save")
    args = parser.parse_args()

    # Editable per-type etch table: --etch-params, else etch_params.json next to this
    # script if present. Overrides the plan's baked angles/speed/hatch/passes BY TYPE.
    etch_path = args.etch_params or etch_params.DEFAULT_PATH
    etch_table = etch_params.load(etch_path) if Path(etch_path).is_file() else None
    if etch_table is not None:
        print("etch table: %s (per-type passes + crosshatch override the plan)" % etch_path)
    else:
        print("etch table: none found (%s); using the plan's baked per-array values." % etch_path)

    plans = []
    for set_dir in args.sets:
        if not set_dir.is_dir():
            print("! not a directory: %s" % set_dir)
            continue
        jobs = discover_jobs(set_dir)
        if not jobs:
            print("! no arrays with DXFs found under %s (check plan.json + jobs/)" % set_dir)
            continue
        if etch_table is not None:
            for job in jobs:
                p = etch_params.params_for_type(etch_table, job.get("type"))
                if p:
                    job["fill_angles_deg"] = p.get("fill_angles_deg", job.get("fill_angles_deg"))
                    job["speed_mm_s"] = p.get("speed_mm_s", job.get("speed_mm_s"))
                    job["hatch_mm"] = etch_table.get("hatch_mm", job.get("hatch_mm"))
                    job["passes"] = p.get("passes", job.get("passes"))
        out_dir = set_dir / args.out_subdir
        for job in jobs:
            job["out_path"] = (out_dir / ("%s.wlj" % job["name"])).resolve()
        plans.append((set_dir, jobs, out_dir))
        print_plan(set_dir, jobs, out_dir)

    if not plans:
        return 1
    if args.dry_run:
        print("\nDry run: no jobs written. Re-run on the laser PC without --dry-run.")
        return 0

    session = WinLaseSession()
    print("\nWinLase attached: %d scan card(s), lens %d bits/mm (field +/-%.1f mm)."
          % (session.cards, session.bits_per_mm, FIELD_BIT_LIMIT / session.bits_per_mm))
    print("  required laser profile: power %.0f %%, freq %.2f kHz (fixed); mark speed is "
          "per-array, verified against each job's own setting (read back once per speed)."
          % (EXPECTED_LASER_POWER_PCT, EXPECTED_FREQ_KHZ))
    try:
        if args.verify:
            set_dir, jobs, out_dir = plans[0]
            print("\nVERIFY: building %s in memory (not saved)..." % jobs[0]["name"])
            warnings = session.build_job(jobs[0], save=False)
            for w in warnings:
                print("  WARN %s" % w)
            print("  placement math ran; " + ("see warnings above." if warnings
                  else "no warnings -- safe to run the full build."))
            return 0

        total = 0
        for set_dir, jobs, out_dir in plans:
            out_dir.mkdir(parents=True, exist_ok=True)
            print("\nBuilding %s -> %s" % (set_dir.name, out_dir))
            for job in jobs:
                warnings = session.build_job(job, save=True)
                flag = "  <-- CHECK" if warnings else ""
                print("  wrote %s%s" % (job["out_path"].name, flag))
                for w in warnings:
                    print("     WARN %s" % w)
                total += 1
        print("\nDone: %d job(s) written. The exposure run loop (expose_wafer.py) drives "
              "the row-by-row schedule from plan.json; close the WinLase GUI before arming."
              % total)
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
