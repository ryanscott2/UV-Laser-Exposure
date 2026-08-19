"""Row-by-row backside wafer exposure: OptiScan III stage + WinLase Pro, sequenced.

This is the run half of the PFLM exposure tool (ARCHITECTURE.md sec 7.3). It executes
the ``schedule`` baked into a built set's ``plan.json`` (sec 4.1): a flat list of
``expose`` and ``mask`` steps that already encodes the top-to-bottom monotonic sweep and
the stride-2 within-row masking checkerboard (sec 2.2). For each ``expose`` step it moves
the stage so the array's ``exposed_center_um`` (design frame, already rotated by
``design_rotation_deg``) lands on the fixed galvo-field center, then marks that array's
pre-built WinLase job. For each ``mask`` step it makes a CONTROLLED pause so the operator
can mask the just-exposed group before the next group is exposed.

The jig stays put; the stage indexes the wafer under the fixed field. Auto-centering is
OFF at the laser (the DXF origin IS the field center) -- never enable fit-to-field.

Pipeline it ties together:
  1. pflm.cli build ...        -> output/sets/<set>/  (plan.json + jobs/*.dxf; calibration
                                  OFFSET baked into the DXF geometry, not applied on the stage)
  2. winlase_build_jobs.py     -> <set>/WinLaseJobs/<set>_<array_id>.wlj
  3. (no teaching)             -> alignment is MECHANICAL (jig + flats); the wafer->stage
                                  mapping is a FIXED machine constant (transform.default_
                                  calibration, or a one-time exposure_calibration.json config)
  4. THIS script               -> pre-flight -> move -> (focus) -> mark, per schedule

SAFETY -- this can fire the laser, so it is gated (identical philosophy to Singulation's
dice_wafer.py, which the safety code is adapted from):
  * Default is SIMULATE: real stage moves, marking is faked (a short dwell). Run it this
    way first to prove motion and sequencing with NO laser.
  * Pass --arm to actually load and mark jobs through WinLase, after typing "EXPOSE"
    (unless --yes, e.g. the UI confirms instead).
  * PRE-FLIGHT (before any motion or arming): every array's stage target is computed via
    transform.wafer_to_stage and checked with transform.check_reachable -- refuse to run
    if ANY target is outside travel or over the +STAGE_Y_MAX_UM stage-Y ceiling (the
    P3/P4 ceiling, sec 2.1), or if plan.stage.feasible is false. The offending array_ids
    are printed. Then, when armed, every job's laser profile is read back and verified
    (power 100 %, freq 30 kHz, mark speed within 100-1000 mm/s); any mismatch aborts -- no motion, no
    firing. Each job is re-checked once more at mark time. This script never WRITES laser
    power or frequency; it only reads and verifies them.
  * A countdown precedes the run; pressing a key during the countdown, between arrays,
    during a mask pause, or DURING a mark does a controlled stop (stage `I` + WinLase
    TerminateMark -- the abort poll halts the in-progress multi-pass mark). Keep a hand
    on the hardware e-stop anyway. This live-laser path could not be tested off the machine.

STOP is a CONTROLLED stop (between arrays / at a mask pause), never an e-stop. A --stop-flag
file (written by the UI's STOP button) is polled the same way as a keypress.

WinLase note: the WinLase GUI and the COM server cannot both hold the marker library, so
CLOSE the WinLase GUI before an armed run. Python 3.8, no network; serial via
pyserial-or-pywin32 (see optiscan.py), WinLase via pywin32 (win32com).

    python laser_pc/expose_wafer.py output/sets/081026_PFLM_Heaters              # SIMULATE
    python laser_pc/expose_wafer.py output/sets/081026_PFLM_Heaters --list       # plan + targets
    python laser_pc/expose_wafer.py output/sets/081026_PFLM_Heaters --arm        # LIVE laser
    python laser_pc/expose_wafer.py <set> --arm --start-step 6                    # resume mid-wafer
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

from optiscan import OptiScan       # same laser_pc/ dir
import transform                    # wafer->stage math + reachability (sec 6)
import winlase_build_jobs           # job naming / bounds helpers (sec 7.2)
import etch_params                  # editable per-type etch table (passes), same dir

DEFAULT_PASSES = 1                   # fallback pass count per array unless plan/etch/CSV says otherwise
DEFAULT_PASSES_FILE = Path(__file__).resolve().parent / "expose_passes.csv"
DEFAULT_CALIBRATION = Path(__file__).resolve().parent / "exposure_calibration.json"
DEFAULT_COUNTDOWN_S = 10
STAGE_Y_MAX_UM_FALLBACK = 6950       # the P3/P4 stage-Y ceiling (sec 2.1); plan/cal override this
DEFAULT_TRAVEL_UM = (126000, 76000)  # ES111 travel envelope (sec 2)

# Required laser profile -- power and frequency MUST match the WinLase "Vector Graphic
# -> Properties -> Profile" the operator confirmed (power 100 %, frequency 30 kHz).
# Mark speed is per-array (400 mm/s pinfins/marks, 1000 mm/s dead-space), so it is not
# pinned to one value -- the gate only requires it to be a sane speed in the inclusive
# [100, 1000] mm/s range (whatever the job is set to). Before ANY stage motion or firing,
# every job is read back and checked against these; a mismatch aborts the whole run.
# GetObjProfile index map (same as winlase_build_jobs):
# [0] = mark speed (bits/mSec), [5] = laser power %, [9] = T1 frequency (kHz).
EXPECTED_LASER_POWER_PCT = 100.0
EXPECTED_FREQ_KHZ = 30.0
SPEED_MIN_MM_S = 100.0             # mark speed may be any value in this inclusive range
SPEED_MAX_MM_S = 1000.0           #   (400 = pinfins/marks, 1000 = dead-space ablation)
POWER_TOLERANCE_PCT = 0.5
FREQ_TOLERANCE_KHZ = 0.1
SPEED_TOLERANCE_MM_S = 10.0        # slop on the 100/1000 bounds (quantization at readback)
PROFILE_SPEED_IDX, PROFILE_POWER_IDX, PROFILE_FREQ_IDX = 0, 5, 9

# WinLase marking is ASYNCHRONOUS: MarkAllObj starts a pass and returns; GetBusyStatus
# polls for completion. So we wait for idle before starting a job (drains any prior job's
# tail) and after every pass (so a job fully finishes before it is closed and the next one
# loads -- otherwise closing/loading mid-mark wedges WinLase "busy").
MARK_POLL_S = 0.02           # GetBusyStatus poll interval
MARK_SETTLE_S = 0.1          # let a just-issued mark register as busy before we poll for done
MARK_WAIT_TIMEOUT_S = 120.0  # max idle wait PER PASS (a whole n-pass mark waits this x n)

# Time-estimate (ETA): empirical, measured live. Emitted as "[eta] ..." lines that the
# launcher UI mirrors into its Est. time field. A small per-set cache warm-starts it.
# Cosmetic only -- never affects motion or firing. Mask pauses are operator-driven and are
# NOT included in the estimate.
ETA_LOG_INTERVAL_S = 15.0
DEFAULT_MOVE_S = 8.0
DEFAULT_ETA_CACHE = Path(__file__).resolve().parent / ".expose_eta.json"

# Controlled-pause poll interval for a mask step (waiting on Resume / Stop).
RESUME_POLL_S = 0.1


def _aborted() -> bool:
    """True if a key was pressed (non-blocking), on Windows. Consumes the key."""
    try:
        import msvcrt
    except ImportError:
        return False
    if msvcrt.kbhit():
        msvcrt.getch()
        return True
    return False


# ------------------------------------------------------------------- WinLase COM
class WinLaseMarker:
    """Loads and marks pre-built .wlj jobs through the WinLase automation server.

    Adapted verbatim from Singulation dice_wafer.py -- the async MarkAllObj/GetBusyStatus
    two-idle poll, the read-only laser-profile gate, and TerminateMark on timeout/abort
    are the tested safety core and are intentionally unchanged.
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
            ) from exc
        # Late/dynamic binding on purpose -- see the note in winlase_build_jobs.py:
        # WinLase's non-retval [out] params break early binding; dynamic auto-returns them.
        self.m = dynamic.Dispatch("Winlase.Automate")
        self.m.AttachToMarker()
        if int(self.m.GetScanCardCount()) < 1:
            raise SystemExit("WinLase reports no scan card; can't mark. Run on the laser PC.")
        self.bits_per_mm = int(self.m.GetLensCalFactor(0, 0))
        if self.bits_per_mm <= 0:
            raise SystemExit("GetLensCalFactor returned <= 0; is a lens cal loaded?")

    def ready(self) -> bool:
        return int(self.m.GetBusyStatus(0)) == 0

    def _wait_not_busy(self, abort, timeout_s: float) -> bool:
        """Poll until WinLase is idle (two consecutive not-busy reads, so a transient 0
        can't be mistaken for done). Returns True when idle, False if aborted mid-wait.
        Raises TimeoutError (after TerminateMark) if it never goes idle in time."""
        t0 = time.time()
        idle_hits = 0
        while True:
            if abort():
                return False
            if self.ready():
                idle_hits += 1
                if idle_hits >= 2:
                    return True
            else:
                idle_hits = 0
            if time.time() - t0 > timeout_s:
                try:
                    self.m.TerminateMark()
                except Exception:
                    pass
                raise TimeoutError("WinLase stayed busy > %.0f s" % timeout_s)
            time.sleep(MARK_POLL_S)

    def _check_active_params(self, label: str):
        """Read every object's profile in the ACTIVE job and compare to the required
        laser settings. Returns a list of human-readable problem strings (empty = OK)."""
        problems = []
        count = int(self.m.GetObjCount())
        if count < 1:
            return ["%s: job holds no objects to verify" % label]
        for obj in range(count):
            prof = list(self.m.GetObjProfile(obj, 0))
            power = float(prof[PROFILE_POWER_IDX])
            freq = float(prof[PROFILE_FREQ_IDX])
            speed = float(prof[PROFILE_SPEED_IDX]) / self.bits_per_mm * 1000.0
            if abs(power - EXPECTED_LASER_POWER_PCT) > POWER_TOLERANCE_PCT:
                problems.append("%s obj %d: laser power %.3f %% (need %.0f %%)"
                                % (label, obj, power, EXPECTED_LASER_POWER_PCT))
            if abs(freq - EXPECTED_FREQ_KHZ) > FREQ_TOLERANCE_KHZ:
                problems.append("%s obj %d: frequency %.2f kHz (need %.2f kHz)"
                                % (label, obj, freq, EXPECTED_FREQ_KHZ))
            if not (SPEED_MIN_MM_S - SPEED_TOLERANCE_MM_S <= speed
                    <= SPEED_MAX_MM_S + SPEED_TOLERANCE_MM_S):
                problems.append("%s obj %d: mark speed %.0f mm/s (must be %.0f-%.0f mm/s)"
                                % (label, obj, speed, SPEED_MIN_MM_S, SPEED_MAX_MM_S))
        return problems

    def verify_job_params(self, wlj: Path):
        """Load a job read-only, check its laser profile, close it. Returns problems."""
        idx = int(self.m.LoadJobFromFile(str(wlj.resolve())))
        try:
            self.m.SetActiveJob(idx)
            return self._check_active_params(wlj.name)
        finally:
            try:
                self.m.CloseJob(idx)
            except Exception:
                pass

    def mark_job(self, wlj: Path, passes: int, abort, on_pass=None) -> bool:
        """Load a job, set its per-array pass count on every object, and mark it ONCE.

        WinLase runs all `passes` passes inside that single mark, alternating the two
        crosshatch fill angles -- a single external mark only lays the FIRST angle, so the
        pass count has to live on the object, not in an outer loop. Setting it here (from
        the plan) also means editing plan.json passes takes effect without rebuilding jobs.
        Returns False if aborted; `on_pass(0, dt)`, if given, reports the mark's wall-clock
        seconds for the live estimate (best-effort -- an exception in it is swallowed)."""
        n = max(1, int(passes))
        job_index = int(self.m.LoadJobFromFile(str(wlj.resolve())))
        self.m.SetActiveJob(job_index)
        # Last-instant re-check right before firing (defence in depth on top of the
        # pre-flight gate): never mark a job whose laser profile is not exactly right.
        problems = self._check_active_params(wlj.name)
        if problems:
            try:
                self.m.CloseJob(job_index)
            except Exception:
                pass
            raise RuntimeError("laser profile check failed at mark time:\n  "
                               + "\n  ".join(problems))
        try:
            # Set the real pass count on every object so WinLase cross-hatches internally
            # (all passes in one mark), rather than an outer loop that only lays one angle.
            for obj in range(int(self.m.GetObjCount())):
                self.m.SetObjNumPasses(obj, n)
            # Make sure the marker is idle before we start -- this also drains the tail of
            # the previous array's job so loading/closing never collides with a mark.
            if not self._wait_not_busy(abort, MARK_WAIT_TIMEOUT_S):
                self.m.TerminateMark()
                return False
            if abort():
                self.m.TerminateMark()
                return False
            t_pass = time.time()
            self.m.MarkAllObj(0)          # async: WinLase runs ALL n passes, returns immediately
            time.sleep(MARK_SETTLE_S)     # let the mark register as busy before polling
            # The whole n-pass mark must fit the timeout, so scale it by the pass count.
            if not self._wait_not_busy(abort, MARK_WAIT_TIMEOUT_S * n):
                self.m.TerminateMark()
                return False
            if on_pass is not None:
                try:
                    on_pass(0, time.time() - t_pass)
                except Exception:
                    pass                  # ETA is cosmetic; never let it break a mark
        finally:
            try:
                self.m.CloseJob(job_index)    # safe now: the job has fully finished marking
            except Exception:
                pass
        return True

    def stop(self) -> None:
        try:
            self.m.TerminateMark()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.m.ReleaseMarker()
        except Exception:
            pass


# ------------------------------------------------------------------ time estimate
def _fmt_dur(seconds: float) -> str:
    seconds = int(round(max(0.0, seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def load_eta_cache(path: Path, set_name: str) -> dict:
    """Warm-start per-pass/move seconds for a set from a prior armed run. Returns {} for a
    missing, unreadable, or malformed cache -- every shape is validated, so a bad or
    hand-edited cache can never raise into (and abort) an exposure run."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        entry = data.get(set_name)
        if not isinstance(entry, dict):
            return {}
        warm = {}
        per_pass = entry.get("per_pass")
        if isinstance(per_pass, dict):
            for k, v in per_pass.items():
                if isinstance(v, (int, float)):
                    warm[str(k)] = float(v)
        if isinstance(entry.get("move"), (int, float)):
            warm["move"] = float(entry["move"])
        return warm
    except (OSError, ValueError, TypeError):
        return {}


def save_eta_cache(path: Path, set_name: str, per_pass: dict, move_s) -> None:
    """Persist this run's measured pace so the next run of the same set estimates from
    t=0. Best-effort: any failure is swallowed (an ETA cache is never worth an error)."""
    try:
        data = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                data = {}
        if not isinstance(data, dict):
            data = {}
        entry = {"per_pass": {k: round(v, 3) for k, v in per_pass.items() if v is not None}}
        if move_s is not None:
            entry["move"] = round(move_s, 3)
        entry["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        data[set_name] = entry
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except (OSError, ValueError, TypeError):
        pass


class EtaTracker:
    """Live, empirical time estimate for an exposure run.

    Every mark pass of an array reloads the same job, so its wall-clock time is
    near-constant; we measure it (and each stage move) and extrapolate over the remaining
    passes and arrays. No geometry, no machine constants -- just measured pace, refined as
    the run proceeds. A per-set cache warm-starts it. Estimates are marked '~'. Mask pauses
    are operator-driven and deliberately excluded. Labels are the exposed array_ids in
    schedule order.
    """

    def __init__(self, labels, passes, warm=None, final_move=False, emit=print):
        self.labels = list(labels)
        # `passes` may be an int (uniform) or a dict label->passes (per-array).
        self.passes = passes
        self.warm = dict(warm or {})
        self.final_move = bool(final_move)
        self.emit = emit
        self.per_pass = {label: [] for label in self.labels}
        self.moves = []
        self.run_t0 = None
        self.cur_idx = 0
        self.cur_pass = 0
        self._last_emit = 0.0

    def _np(self, label) -> int:
        """Passes for one array (per-array dict, or the uniform int)."""
        if isinstance(self.passes, dict):
            return int(self.passes.get(label, 1))
        return int(self.passes)

    def _total_passes(self) -> int:
        return sum(self._np(l) for l in self.labels)

    # -- measurement --------------------------------------------------------
    def start(self):
        self.run_t0 = time.time()

    def record_move(self, seconds):
        self.moves.append(float(seconds))

    def on_station_start(self, idx, label):
        self.cur_idx = idx
        self.cur_pass = 0
        self._maybe_emit(force=True)

    def on_pass(self, idx, label, pass_i, dt):
        self.cur_idx = idx
        self.cur_pass = pass_i + 1
        self.per_pass[label].append(float(dt))
        self._maybe_emit(force=(len(self.per_pass[label]) == 1))

    # -- estimation ---------------------------------------------------------
    def _move_est(self):
        if self.moves:
            return sum(self.moves) / len(self.moves)
        return self.warm.get("move", DEFAULT_MOVE_S)

    def _per_pass_est(self, label):
        vals = self.per_pass.get(label, [])
        if len(vals) >= 2:
            return sum(vals[1:]) / len(vals[1:])   # drop the cold-start first pass
        if len(vals) == 1:
            return vals[0]
        if label in self.warm:
            return self.warm[label]
        measured = [v for lst in self.per_pass.values() for v in lst]
        if measured:
            return sum(measured) / len(measured)
        warm_vals = [self.warm[l] for l in self.labels if l in self.warm]
        if warm_vals:
            return sum(warm_vals) / len(warm_vals)
        return None

    def _remaining(self):
        if not self.labels:
            return 0.0, True
        known = True
        total = 0.0
        cur = self._per_pass_est(self.labels[self.cur_idx])
        if cur is None:
            known, cur = False, 0.0
        total += max(0, self._np(self.labels[self.cur_idx]) - self.cur_pass) * cur
        for idx in range(self.cur_idx + 1, len(self.labels)):
            est = self._per_pass_est(self.labels[idx])
            if est is None:
                known, est = False, 0.0
            total += self._np(self.labels[idx]) * est
        remaining_moves = (len(self.labels) - 1 - self.cur_idx) + (1 if self.final_move else 0)
        total += max(0, remaining_moves) * self._move_est()
        return total, known

    def per_pass_means(self):
        out = {}
        for label in self.labels:
            vals = self.per_pass[label]
            if len(vals) >= 2:
                out[label] = sum(vals[1:]) / len(vals[1:])
            elif vals:
                out[label] = vals[0]
        return out

    def move_mean(self):
        return (sum(self.moves) / len(self.moves)) if self.moves else None

    # -- output -------------------------------------------------------------
    def preview(self):
        if not self.labels:
            return
        total = 0.0
        for label in self.labels:
            pp = self.warm.get(label)
            if pp is None:
                return
            total += self._np(label) * pp
        moves = len(self.labels) + (1 if self.final_move else 0)
        total += moves * self._move_est()
        self.emit("[eta] estimated total ~%s (%d total passes, from this set's last armed run)"
                  % (_fmt_dur(total), self._total_passes()))

    def _maybe_emit(self, force):
        if not self.labels:
            return
        now = time.time()
        if not force and (now - self._last_emit) < ETA_LOG_INTERVAL_S:
            return
        self._last_emit = now
        elapsed = now - (self.run_t0 or now)
        remaining, known = self._remaining()
        done = sum(len(v) for v in self.per_pass.values())
        total_passes = self._total_passes()
        label = self.labels[self.cur_idx]
        if known:
            tail = "remaining ~%s | total ~%s" % (_fmt_dur(remaining), _fmt_dur(elapsed + remaining))
        else:
            tail = "remaining estimating..."
        self.emit("[eta] elapsed %s | %s (%d/%d) pass %d/%d | %d/%d passes | %s"
                  % (_fmt_dur(elapsed), label, self.cur_idx + 1, len(self.labels),
                     self.cur_pass, self._np(label), done, total_passes, tail))

    def finish(self):
        elapsed = time.time() - (self.run_t0 or time.time())
        self.emit("[eta] done | total elapsed %s" % _fmt_dur(elapsed))


# --------------------------------------------------------------------- planning
def load_plan(set_dir: Path) -> dict:
    """Read <set_dir>/plan.json (the contract between the two halves, sec 4)."""
    plan_path = set_dir / "plan.json"
    if not plan_path.is_file():
        raise SystemExit("No plan.json in %s -- build the set first (python -m pflm.cli build)."
                         % set_dir)
    try:
        return json.loads(plan_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit("plan.json in %s is not valid JSON: %s" % (set_dir, exc))


def index_arrays(plan: dict) -> dict:
    """array_id -> array dict, flattened over plan['rows']."""
    arrays = {}
    for row in plan.get("rows", []):
        for a in row.get("arrays", []):
            arrays[a["array_id"]] = a
    return arrays


def load_calibration(path: Path):
    """Fixed mechanical wafer->stage mapping (jig geometry), NOT a taught reference.

    Alignment is purely mechanical (the jig + wafer flats fix the wafer at a known,
    repeatable position), so there is NO teaching step: if no config file is present the
    built-in default mapping (transform.default_calibration) is used. A site can override
    the fixed constants by dropping an exposure_calibration.json next to this script, but
    it is a one-time machine config, not a per-wafer teach. The fine calibration OFFSET is
    baked into the DXF geometry at prep time (global_offset), never applied on the stage."""
    if not Path(path).is_file():
        return transform.default_calibration()
    return transform.load_calibration(str(path))


def load_passes(csv_path: Path, set_name: str, default: int):
    """Mark passes per set, from a CSV of `set,passes` rows.

    Exact set-folder match wins; else a 'default' row; else `default`. Blank lines,
    '#' comments, and a 'set,passes' header are ignored. Returns (passes, source)."""
    table = {}
    if csv_path.is_file():
        with csv_path.open(newline="", encoding="utf-8") as stream:
            for row in csv.reader(stream):
                if len(row) < 2 or not row[0].strip() or row[0].lstrip().startswith("#"):
                    continue
                key, value = row[0].strip(), row[1].strip()
                if key.lower() in ("set", "name"):
                    continue
                try:
                    table[key] = int(value)
                except ValueError:
                    pass
    if set_name in table:
        return table[set_name], "%s in %s" % (set_name, csv_path.name)
    if "default" in table:
        return table["default"], "default in %s" % csv_path.name
    return default, "built-in default (%d)" % default


def passes_for(step, arrays, override, fallback):
    """Mark passes for ONE array (each array is marked to completion before the next).

    --passes `override` forces a uniform count (test runs); otherwise the plan's
    per-array etch passes (from the schedule step, or the array record's etch), and
    finally the CSV/built-in `fallback`. Passes vary per type (e.g. D50 sq = 44)."""
    if override is not None:
        return int(override)
    p = step.get("passes")
    if p is None:
        p = ((arrays.get(step.get("array_id"), {}) or {}).get("etch") or {}).get("passes")
    return int(p) if p is not None else int(fallback)


def stage_bounds(plan: dict, cal_raw: dict):
    """(travel_x, travel_y, stage_y_max) from plan.stage, falling back to cal then const."""
    stage = plan.get("stage", {}) or {}
    travel = stage.get("travel_um") or cal_raw.get("travel_um") or list(DEFAULT_TRAVEL_UM)
    travel_x = int(travel[0])
    travel_y = int(travel[1])
    ceil = stage.get("stage_y_max_um")
    if ceil is None:
        ceil = cal_raw.get("stage_y_max_um", STAGE_Y_MAX_UM_FALLBACK)
    return travel_x, travel_y, int(ceil)


def job_path(set_dir: Path, set_name: str, array_id: str) -> Path:
    """<set_dir>/WinLaseJobs/<set>_<array_id>.wlj (sec 4). Uses a naming helper from
    winlase_build_jobs if it provides one, so the two halves can't drift on the filename."""
    fn = getattr(winlase_build_jobs, "job_filename", None)
    if callable(fn):
        try:
            return set_dir / "WinLaseJobs" / fn(set_name, array_id)
        except Exception:
            pass
    return set_dir / "WinLaseJobs" / ("%s_%s.wlj" % (set_name, array_id))


def expose_targets(plan: dict, arrays: dict, cal):
    """List of (step_dict, array_id, (stage_x, stage_y)) for every 'expose' step, in
    schedule order. Pure math via transform.wafer_to_stage -- no hardware."""
    out = []
    for step in plan.get("schedule", []):
        if step.get("action") == "expose":
            aid = step.get("array_id")
            a = arrays.get(aid)
            if a is None:
                raise SystemExit("schedule references unknown array_id %r (not in plan.rows)." % aid)
            x, y = transform.wafer_to_stage(a["exposed_center_um"], cal)
            out.append((step, aid, (int(round(x)), int(round(y)))))
    return out


def preflight(plan: dict, cal, travel_x: int, travel_y: int, ceil: int):
    """Authoritative reachability verdict (sec 2.1 / 7.3). Combines plan.stage.feasible
    with transform.check_reachable. Returns (ok, feasible, failures)."""
    feasible = bool(plan.get("stage", {}).get("feasible", True))
    try:
        ok_reach, failures = transform.check_reachable(plan, cal)
    except Exception as exc:
        # Never let a transform quirk pretend the wafer is reachable.
        return False, feasible, ["check_reachable raised: %s" % exc]
    failures = list(failures or [])
    return (feasible and bool(ok_reach)), feasible, failures


def _local_reach(x: int, y: int, travel_x: int, travel_y: int, ceil: int) -> bool:
    """Per-target display check mirroring check_reachable: inside travel AND under the
    stage-Y ceiling. The authoritative gate is transform.check_reachable; this is for the
    printed --list table only."""
    return abs(x) <= travel_x and -travel_y <= y <= ceil


def print_schedule(plan, set_dir, set_name, arrays, targets, passes_by_label, focus, armed,
                   travel_x, travel_y, ceil):
    tmap = {id(step): xy for step, _aid, xy in targets}
    rot = plan.get("design_rotation_deg", "?")
    strat = plan.get("mask_strategy", {}) or {}
    n_expose = len(targets)
    n_mask = sum(1 for s in plan.get("schedule", []) if s.get("action") == "mask")
    total_passes = sum(passes_by_label.values())
    print("\nExposure schedule for %s   [%s]" % (
        set_name, "ARMED - LASER LIVE" if armed else "SIMULATE - no laser"))
    print("  rotation %s deg | %s | stride %s | %d exposures, %d mask pauses | %d total passes "
          "(per-array)%s"
          % (rot, plan.get("exposure_order", "?"),
             strat.get("within_row_stride", "?"), n_expose, n_mask, total_passes,
             " | focus on" if focus else ""))
    print("  stage limits: |X| <= %d um, %d <= Y <= %d um (ceiling)"
          % (travel_x, -travel_y, ceil))
    for step in plan.get("schedule", []):
        s = int(step.get("step", -1))
        if step.get("action") == "expose":
            aid = step.get("array_id")
            xy = tmap.get(id(step))
            reach = "OK " if (xy and _local_reach(xy[0], xy[1], travel_x, travel_y, ceil)) else "OUT"
            a = arrays.get(aid, {}) or {}
            typ = a.get("type") or "?"
            print("  [%3d] expose %-8s %-9s row %s ph %s  ->  X=%-8d Y=%-8d  %sx%s pass  %s"
                  % (s, aid, typ, step.get("row_index", "?"), step.get("phase", "?"),
                     xy[0] if xy else 0, xy[1] if xy else 0,
                     "" , passes_by_label.get(aid, "?"), reach))
        elif step.get("action") == "mask":
            print("  [%3d] mask   %s" % (s, step.get("label", "mask pause")))
        else:
            print("  [%3d] ?? unknown action %r" % (s, step.get("action")))


# ------------------------------------------------------------------------- run
def countdown(seconds: int, should_abort) -> bool:
    """Abortable countdown. Returns True to proceed, False if aborted."""
    print("\nStarting in %d s -- press any key (or the UI Stop) to ABORT." % seconds)
    for n in range(seconds, 0, -1):
        sys.stdout.write("\r  %2d ... " % n)
        sys.stdout.flush()
        end = time.time() + 1.0
        while time.time() < end:
            if should_abort():
                print("\naborted at countdown.")
                return False
            time.sleep(0.05)
    print("\r  go.      ")
    return True


def wait_for_resume(stop_flag, resume_flag) -> bool:
    """Controlled mask pause. Block until the operator resumes; honor the STOP flag.

    Returns True to RESUME (a keypress in the CLI, or the --resume-flag file appears --
    how the UI's Resume button signals). Returns False if the --stop-flag appears (a
    controlled stop -- end the run cleanly). This is NEVER an e-stop; it only pauses
    BETWEEN groups, with the laser idle."""
    # Consume a stale resume flag so a leftover from a prior pause can't skip this one.
    if resume_flag is not None and resume_flag.exists():
        try:
            resume_flag.unlink()
        except OSError:
            pass
    print("  [mask] paused -- mask the completed group, then press any key "
          "(UI: Resume). Stop flag aborts.")
    while True:
        if stop_flag is not None and stop_flag.exists():
            return False
        if resume_flag is not None and resume_flag.exists():
            try:
                resume_flag.unlink()
            except OSError:
                pass
            return True
        if _aborted():
            return True
        time.sleep(RESUME_POLL_S)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("set_dir", type=Path, help="built set directory (has plan.json + WinLaseJobs/)")
    p.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION,
                   help="exposure_calibration.json (default: next to this script)")
    p.add_argument("--port", default="COM5")
    p.add_argument("--passes", type=int, default=None,
                   help="mark passes per array; overrides expose_passes.csv for this run")
    p.add_argument("--passes-file", type=Path, default=DEFAULT_PASSES_FILE,
                   help="CSV of 'set,passes' rows (default: expose_passes.csv next to this script)")
    p.add_argument("--etch-params", type=Path, default=None,
                   help="etch table JSON; per-type passes override the plan (unless --passes given). "
                        "Default: etch_params.json next to this script if it exists.")
    p.add_argument("--arm", action="store_true", help="ACTUALLY fire the laser (default: simulate)")
    p.add_argument("--focus", action="store_true",
                   help="set Z from the calibration reference before each mark (needs motor)")
    p.add_argument("--countdown", type=int, default=DEFAULT_COUNTDOWN_S)
    p.add_argument("--start-step", type=int, default=0,
                   help="resume mid-wafer: begin at this schedule step index")
    p.add_argument("--stop-flag", type=Path, default=Path(".expose_stop"),
                   help="controlled stop if this file appears (relative to the set dir)")
    p.add_argument("--resume-flag", type=Path, default=Path(".expose_resume"),
                   help="resume a mask pause when this file appears (relative to the set dir)")
    p.add_argument("--list", action="store_true",
                   help="print the schedule + computed stage targets + pre-flight verdict, then exit")
    p.add_argument("--yes", action="store_true",
                   help="skip the 'type EXPOSE' arm prompt (the UI confirms instead)")
    args = p.parse_args()

    set_dir = args.set_dir
    plan = load_plan(set_dir)
    arrays = index_arrays(plan)
    set_name = plan.get("set_name") or set_dir.name
    schedule = plan.get("schedule", [])
    if not schedule:
        raise SystemExit("plan.json has an empty 'schedule' -- nothing to expose.")

    # Flags are resolved relative to the set dir (the UI writes them there).
    stop_flag = args.stop_flag if args.stop_flag.is_absolute() else set_dir / args.stop_flag
    resume_flag = args.resume_flag if args.resume_flag.is_absolute() else set_dir / args.resume_flag

    # Calibration + reachability are needed for --list AND the run; both are pure math.
    try:
        cal_raw = json.loads(args.calibration.read_text(encoding="utf-8"))
        if not isinstance(cal_raw, dict):
            cal_raw = {}
    except (OSError, ValueError):
        cal_raw = {}
    focus_z = int(round(float(
        (cal_raw.get("reference", {}) or {}).get("stage", {}).get("z", 0)))) if cal_raw else 0

    cal = load_calibration(args.calibration)
    if args.calibration.is_file():
        print("stage map: %s (fixed mechanical mapping; calibration OFFSET is in the DXF)."
              % args.calibration.name)
    else:
        print("stage map: built-in mechanical default (no teaching; calibration OFFSET is in "
              "the DXF). Verify the fixed wafer->stage constants match this jig.")
    travel_x, travel_y, ceil = stage_bounds(plan, cal_raw)
    targets = expose_targets(plan, arrays, cal)

    fb_csv, passes_src = load_passes(args.passes_file, set_name, DEFAULT_PASSES)
    fallback = args.passes if args.passes is not None else fb_csv
    # Load the editable per-type etch table once (unless --passes forces uniform). It
    # overrides the plan's baked passes/angles BY TYPE; used for passes here and for the
    # effective-etch summary below.
    etch_path = args.etch_params or etch_params.DEFAULT_PATH
    etch_table = etch_params.load(etch_path) if (args.passes is None and Path(etch_path).is_file()) else None
    passes_by_label = {}
    for step in schedule:
        if step.get("action") != "expose":
            continue
        aid = step["array_id"]
        passes_by_label[aid] = passes_for(step, arrays, args.passes, fallback)
        if etch_table is not None:
            pt = etch_params.params_for_type(etch_table, (arrays.get(aid, {}) or {}).get("type"))
            if pt and pt.get("passes") is not None:
                passes_by_label[aid] = int(pt["passes"])
    if args.passes is not None:
        passes_src = "--passes uniform %d (overrides plan + etch table)" % args.passes
    elif etch_table is not None:
        passes_src = "per-type from %s (overrides plan)" % Path(etch_path).name
    elif any(("etch" in (arrays.get(s.get("array_id"), {}) or {})) or ("passes" in s)
             for s in schedule if s.get("action") == "expose"):
        passes_src = "per-array from plan.json"

    def effective_etch(aid):
        """Resolved passes + crosshatch angles + speed + hatch for one array (etch table
        override if loaded, else the plan's baked etch)."""
        a = arrays.get(aid, {}) or {}
        pe = a.get("etch") or {}
        tp = etch_params.params_for_type(etch_table, a.get("type")) if etch_table else None
        return {
            "type": a.get("type"),
            "passes": passes_by_label.get(aid),
            "angles": (tp or {}).get("fill_angles_deg") or pe.get("fill_angles_deg"),
            "speed": (tp or {}).get("speed_mm_s") or pe.get("speed_mm_s"),
            "hatch": (etch_table.get("hatch_mm") if tp else None) or pe.get("hatch_mm"),
        }

    print_schedule(plan, set_dir, set_name, arrays, targets, passes_by_label, args.focus, args.arm,
                   travel_x, travel_y, ceil)

    ok, feasible, failures = preflight(plan, cal, travel_x, travel_y, ceil)
    print("\npre-flight: plan.stage.feasible=%s | reachable=%s (|X|<=%d, Y<=%d ceiling)"
          % (str(feasible).lower(), "yes" if ok else "NO", travel_x, ceil))
    if not feasible:
        print("  plan.stage marks this set INFEASIBLE (rotated spans do not fit travel/ceiling).")
    if failures:
        print("  targets over the ceiling / outside travel: %s" % ", ".join(str(f) for f in failures))
    _uniq = sorted(set(passes_by_label.values()))
    print("passes per array: %s  [%s]"
          % (str(_uniq[0]) if len(_uniq) == 1 else "%d..%d (varies by type)" % (_uniq[0], _uniq[-1]),
             passes_src))

    # Effective etch summary (per type, after any override) -- what will actually run.
    by_type = {}
    for _step, aid, _xy in targets:
        e = effective_etch(aid)
        key = e["type"] or "(untyped)"
        slot = by_type.setdefault(key, [e, 0])
        slot[1] += 1
    print("\netch summary (effective; source: %s):" % passes_src)
    grand = 0
    for typ, (e, cnt) in sorted(by_type.items()):
        ang = "/".join(str(a) for a in (e["angles"] or [])) or "?"
        passes = e["passes"] if e["passes"] is not None else "?"
        grand += (e["passes"] or 0) * cnt
        print("  %-10s x%-2d : %s passes/array, crosshatch %s deg, %s mm/s, hatch %s mm"
              % (typ, cnt, passes, ang, e["speed"] if e["speed"] is not None else "?",
                 e["hatch"] if e["hatch"] is not None else "?"))
    print("  total: %d array marks, %d laser passes overall" % (len(targets), grand))

    if args.list:
        return 0

    if not ok:
        print("\n*** REFUSING TO RUN: the wafer is not reachable as planned "
              "(see pre-flight above). Fix rotation/calibration and rebuild; no motion. ***")
        return 1

    marker = None
    if args.arm:
        print("\n*** ARMED: the laser will fire. Close the WinLase GUI. Hand on e-stop. ***")
        if not args.yes and input('type "EXPOSE" to arm: ').strip() != "EXPOSE":
            print("not armed; exiting.")
            return 1
        marker = WinLaseMarker()
        if not marker.ready():
            marker.close()
            raise SystemExit("WinLase busy at start; aborting.")
        # Pre-flight laser gate: read every expose job back and confirm the profile matches
        # the confirmed WinLase settings BEFORE any stage motion or firing. Abort otherwise.
        print("\nverifying laser profile in every job (need power %.0f %%, freq %.2f kHz, "
              "speed %.0f-%.0f mm/s) ..." % (EXPECTED_LASER_POWER_PCT, EXPECTED_FREQ_KHZ,
                                             SPEED_MIN_MM_S, SPEED_MAX_MM_S))
        problems = []
        for _step, aid, _xy in targets:
            wlj = job_path(set_dir, set_name, aid)
            if not wlj.is_file():
                problems.append("%s: job file missing (%s) -- rebuild jobs" % (aid, wlj))
                continue
            problems.extend(marker.verify_job_params(wlj))
        if problems:
            marker.close()
            print("*** ABORTING: laser parameters / jobs do not match the confirmed profile ***")
            for pr in problems:
                print("  " + pr)
            print("Fix the profile in WinLase and rebuild the jobs; no motion, no firing.")
            return 1
        print("  OK -- all jobs match. Safe to arm.")

    stage = OptiScan(args.port)
    print("stage connected on %s: %s" % (stage.port, stage.identity))

    def abort() -> bool:
        return _aborted() or stop_flag.exists()

    # Slice the schedule for --start-step (resume mid-wafer).
    start = max(0, int(args.start_step))
    if start >= len(schedule):
        raise SystemExit("--start-step %d is past the end of the %d-step schedule."
                         % (start, len(schedule)))
    run_steps = schedule[start:]
    # ETA labels: the exposures that will actually run, in order.
    labels = [step.get("array_id") for step in run_steps if step.get("action") == "expose"]

    rc = 0
    completed = False
    eta = None
    try:
        warm = load_eta_cache(DEFAULT_ETA_CACHE, set_name)
        # Each array is now ONE mark op (WinLase runs all its passes internally), so the
        # estimator tracks one measured mark per array rather than per external pass.
        eta = EtaTracker(labels, {l: 1 for l in labels},
                         warm=warm, final_move=False)
        if warm:
            eta.preview()
        elif not args.arm:
            print("[eta] no timing history for %s yet -- run --arm once to record it." % set_name)
        if start > 0:
            print("resuming at schedule step %d (%d step(s) remain)." % (start, len(run_steps)))
        if not countdown(args.countdown, abort):
            return 1
        eta.start()
        exp_idx = -1
        stopped = False
        # Highest schedule index fully completed; resume = done_through + 1. An expose that
        # is aborted mid-mark is NOT counted (so resume re-does it); a mask that the operator
        # stops AT (having masked) IS counted (so resume continues past it, e.g. after a wash).
        done_through = start - 1
        for step in run_steps:
            action = step.get("action")
            s = int(step.get("step", -1))
            if action == "expose":
                exp_idx += 1
                aid = step.get("array_id")
                a = arrays[aid]
                tx, ty = transform.wafer_to_stage(a["exposed_center_um"], cal)
                tx, ty = int(round(tx)), int(round(ty))
                print("\n[%d %s] move -> X=%d Y=%d" % (s, aid, tx, ty))
                move_t0 = time.time()
                stage.goto(tx, ty)
                if args.focus:
                    stage.goto_z(focus_z)
                eta.record_move(time.time() - move_t0)
                if abort():
                    print("aborted before marking %s." % aid)
                    stopped = True
                    break
                eta.on_station_start(exp_idx, aid)
                wlj = job_path(set_dir, set_name, aid)
                np_ = passes_by_label.get(aid, fallback)   # this array's passes, run to completion
                if args.arm:
                    print("[%s] marking %s x%d (complete before advancing) ..."
                          % (aid, wlj.name, np_))
                    if not marker.mark_job(
                            wlj, np_, abort,
                            on_pass=lambda i, dt, _i=exp_idx, _l=aid: eta.on_pass(_i, _l, i, dt)):
                        print("aborted during %s." % aid)
                        stopped = True
                        break
                else:
                    print("[%s] SIMULATE mark %s x%d (no laser)" % (aid, wlj.name, np_))
                    time.sleep(0.5)
                print("[%s] done." % aid)
                done_through = s                      # array fully marked
            elif action == "mask":
                label = step.get("label", "mask pause")
                print("\n[%d mask] %s" % (s, label))
                if not wait_for_resume(stop_flag, resume_flag):
                    done_through = s                  # operator masked, then stopped (e.g. to wash)
                    print("stop requested during mask pause -- controlled stop.")
                    stopped = True
                    break
                print("  [mask] resumed.")
                done_through = s
            else:
                print("[%d] skipping unknown action %r" % (s, action))
        if not stopped:
            print("\nAll steps complete.")
            completed = True
            eta.finish()
        else:
            resume_n = done_through + 1
            if resume_n < len(schedule):
                # Machine-parseable token the launcher UI reads to pre-fill "Start step".
                print("\n[resume-step] %d" % resume_n)
                print("[resume] stopped cleanly. To continue after a wash + mask (re-teach the "
                      "reference first if the wafer left the stage):")
                print('    python expose_wafer.py "%s" --arm --start-step %d' % (set_dir, resume_n))
            else:
                print("\n[resume-step] done   (that was the last step; nothing remains)")
    except KeyboardInterrupt:
        print("\ninterrupted -- stopping stage and mark.")
        rc = 1
    except (RuntimeError, TimeoutError, ValueError) as exc:
        # A safety/hardware check tripped mid-run (laser-profile mismatch, stage move
        # timeout, WinLase stuck busy, out-of-envelope target). Abort cleanly, no traceback.
        print("\n*** ABORTED: %s ***" % exc)
        rc = 1
    finally:
        try:
            stage.stop()
        except Exception:
            pass
        try:
            stage.close()
        except Exception:
            pass
        if marker is not None:
            marker.stop()
            marker.close()

    # Record this run's measured pace so the next run of this set estimates from t=0.
    if completed and args.arm and eta is not None:
        save_eta_cache(DEFAULT_ETA_CACHE, set_name, eta.per_pass_means(), eta.move_mean())
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
