"""Barebones PFLM exposure launcher (Tkinter, Windows, Python 3.8, standard library only).

A thin wrapper so you never type paths: pick a built set folder and click a button. Each
button runs the existing CLI script (optiscan.py, winlase_build_jobs.py, expose_wafer.py)
as a subprocess with the paths filled in and streams its output into the log pane -- so all
the verified safety logic (pre-flight reachability + laser-profile gate, arm gate, controlled
stop, row-by-row masking) runs unchanged. This UI adds NO laser logic of its own; it only
shells out and mirrors stdout, exactly like the Singulation dice_ui it is adapted from.

What is different from dicing (see docs/ARCHITECTURE.md):
  * A PFLM set folder is identified by its ``plan.json`` (not P1..P4 station subfolders).
  * There is NO teaching: alignment is mechanical (jig + flats), the wafer->stage mapping is a
    fixed machine constant, and the fine calibration OFFSET lives in the DXF geometry (baked at
    prep time), never applied on the stage.
  * expose_wafer.py runs the ``schedule`` from plan.json one array at a time, pausing to let
    the operator MASK a completed row/phase before the next exposure (ARCHITECTURE 2.2).
    This UI watches stdout for those pauses and pops a modal; clicking Resume writes the
    ``--resume-flag`` file the run loop is waiting on, mirroring the ``--stop-flag`` pattern.
    The modal is NOT closed on the click -- it stays up (button -> "Resuming...") and closes
    only once the run confirms the resume by actually starting the next mark (WinLase
    ``[mark] started`` armed, or ``SIMULATE mark`` in a dry run). So an open window always
    means "not marking yet."

STOP is a CONTROLLED stop -- it writes the stop flag and the run loop stops cleanly between
arrays / at the next mask pause. It is NOT an emergency stop; use the hardware e-stop for
that.

Progress / mask parsing contract (what this UI keys off in expose_wafer.py stdout):
  * ``[eta] ...``   -> split into two readouts: the ``next <wash|mask> ~M:SS`` token feeds
                       the "Time to next pause" field, and ``remaining ~.. | total ~..``
                       feed the "Est. remaining" field (raw text if a line has neither).
  * ``[<n> mask] LABEL`` -> the specific pause reason; stashed and shown in the modal.
  * ``[mask] paused ...`` -> opens the mask-pause modal (run loop is now blocked).
  * ``[mask] resumed.``   -> run consumed the flag; the modal is NOT reopened (it waits for
                            the mark-started line before closing).
  * ``[mark] started ...`` / ``SIMULATE mark`` -> the next mark has begun; closes the modal.
  * any line containing ``step N/M`` (e.g. ``[expose] step 3/9 row 1 phase B r01c01 ...``)
    updates the Progress readout; ``row N``, ``phase A|B``, and an ``r##c##`` array id are
    picked up too if present. Parsing is forgiving: whatever tokens appear are shown.

Launch:  double-click run_ui.bat   (or:  pythonw expose_ui.py)
"""

import csv
import json
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import etch_params  # editable per-type etch table (passes + crosshatch angles), same dir

HERE = Path(__file__).resolve().parent
PYCON = str(Path(sys.executable).with_name("python.exe"))  # console python for children
OPTISCAN = HERE / "optiscan.py"
BUILD = HERE / "winlase_build_jobs.py"
EXPOSE = HERE / "expose_wafer.py"
PASSES_CSV = HERE / "expose_passes.csv"
STOP_FLAG = HERE / ".expose_stop"        # UI writes this on STOP; expose_wafer polls it
RESUME_FLAG = HERE / ".expose_resume"    # UI writes this on Resume; the mask pause waits on it
ROOT_MEMO = HERE / ".expose_ui_root"     # remembers the last sets root
ETCH_PARAMS = HERE / "etch_params.json"  # editable per-type etch table (passes + angles)
DEFAULT_ROOT = HERE.parent / "output" / "sets"
DEFAULT_PASSES = 1
DEFAULT_PORT = "COM5"

# stdout parsing (see the module docstring's contract).
RE_STEP = re.compile(r"step\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)
RE_ROW = re.compile(r"row\s+(\d+)", re.IGNORECASE)
RE_PHASE = re.compile(r"phase\s+([AB]|\d+)", re.IGNORECASE)
RE_ARRAY = re.compile(r"\br(\d{2})c(\d{2})\b")
RE_MASK_STEP = re.compile(r"^\[\d+\s+mask\]\s*(.+)$", re.IGNORECASE)  # the specific pause reason
RE_RESUME = re.compile(r"\[resume-step\]\s+(\d+)")   # expose_wafer prints this on a controlled stop
# [eta] line -> two operator readouts (see the module docstring's contract).
RE_ETA_PAUSE = re.compile(r"next\s+(wash|mask)\s+~([0-9:]+)", re.IGNORECASE)
RE_ETA_NOPAUSE = re.compile(r"no more pauses", re.IGNORECASE)
RE_ETA_REMAIN = re.compile(r"remaining\s+~([0-9:]+)", re.IGNORECASE)
RE_ETA_TOTAL = re.compile(r"\btotal\s+~([0-9:]+)", re.IGNORECASE)


def is_set_dir(p):
    """A built PFLM set: a directory that contains plan.json (ARCHITECTURE 4)."""
    return p.is_dir() and (p / "plan.json").is_file()


def read_plan(set_dir):
    """Load plan.json defensively. Returns the dict, or None if missing/unreadable."""
    try:
        data = json.loads((set_dir / "plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def plan_summary(data):
    """(summary_text, n_schedule_steps, feasible) from a plan dict, all forgiving."""
    if not isinstance(data, dict):
        return "no plan.json", 0, None
    sched = data.get("schedule")
    if not isinstance(sched, list):
        sched = []
    n_steps = len(sched)
    n_expose = sum(1 for s in sched if isinstance(s, dict) and s.get("action") == "expose")
    n_mask = sum(1 for s in sched if isinstance(s, dict) and s.get("action") == "mask")
    rot = data.get("design_rotation_deg")
    stage = data.get("stage") if isinstance(data.get("stage"), dict) else {}
    feasible = stage.get("feasible")
    rot_txt = ("%s deg" % rot) if rot is not None else "rotation ?"
    feas_txt = {True: "feasible", False: "INFEASIBLE (pre-flight will refuse)"}.get(
        feasible, "feasible: ?")
    text = "%s  |  %d steps (%d expose, %d mask)  |  %s" % (
        rot_txt, n_steps, n_expose, n_mask, feas_txt)
    return text, n_steps, feasible


def passes_for(set_name):
    """Passes for a set from expose_passes.csv (exact, else 'default', else DEFAULT_PASSES)."""
    table = {}
    try:
        with PASSES_CSV.open(newline="", encoding="utf-8") as stream:
            for row in csv.reader(stream):
                if len(row) < 2 or not row[0].strip() or row[0].lstrip().startswith("#"):
                    continue
                if row[0].strip().lower() in ("set", "name"):
                    continue
                try:
                    table[row[0].strip()] = int(row[1])
                except ValueError:
                    pass
    except OSError:
        pass
    return table.get(set_name, table.get("default", DEFAULT_PASSES))


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self.mask_win = None
        self.mask_lbl = None
        self.mask_resume_btn = None      # so Resume can flip to "Resuming..." and stay open
        self.mask_status_lbl = None      # "waiting for WinLase to start the mark..."
        self._pending_mask_label = None  # specific pause reason from the "[<n> mask]" line
        self.plan_total = 0
        self._reset_progress()
        root.title("UV Laser Exposure")
        root.geometry("900x700")

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Sets root:").grid(row=0, column=0, sticky="w")
        self.root_var = tk.StringVar(value=self._load_root())
        ttk.Entry(top, textvariable=self.root_var).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(top, text="Browse", command=self.browse_root).grid(row=0, column=2)

        ttk.Label(top, text="Set:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.set_var = tk.StringVar()
        self.set_combo = ttk.Combobox(top, textvariable=self.set_var, state="readonly")
        self.set_combo.grid(row=1, column=1, sticky="we", padx=4, pady=(6, 0))
        self.set_combo.bind("<<ComboboxSelected>>", lambda e: self._show_set_info())
        ttk.Button(top, text="Refresh", command=self.refresh_sets).grid(row=1, column=2, pady=(6, 0))

        params = ttk.Frame(top)
        params.grid(row=2, column=0, columnspan=3, sticky="we", pady=(6, 0))
        ttk.Label(params, text="Start step:").grid(row=0, column=0, sticky="w")
        self.start_var = tk.StringVar(value="0")
        self.start_spin = ttk.Spinbox(params, from_=0, to=100000, width=8,
                                      textvariable=self.start_var)
        self.start_spin.grid(row=0, column=1, sticky="w", padx=(4, 12))
        ttk.Label(params, text="Port:").grid(row=0, column=2, sticky="w")
        self.port_var = tk.StringVar(value=DEFAULT_PORT)
        self.port_entry = ttk.Entry(params, textvariable=self.port_var, width=8)
        self.port_entry.grid(row=0, column=3, sticky="w", padx=(4, 12))
        self.focus_var = tk.BooleanVar(value=False)
        self.focus_chk = ttk.Checkbutton(params, text="Focus (set Z)", variable=self.focus_var)
        self.focus_chk.grid(row=0, column=4, sticky="w")
        self.resume_hint = tk.StringVar(value="")
        ttk.Label(params, textvariable=self.resume_hint, foreground="#c0392b",
                  font=("Segoe UI", 9, "bold")).grid(row=0, column=5, sticky="w", padx=(10, 0))

        ttk.Label(params, text="Re-datum (RIS):").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.redatum_var = tk.StringVar(value="move")   # default ON (per-move) for consistent alignment
        self.redatum_combo = ttk.Combobox(params, textvariable=self.redatum_var, state="readonly",
                                          width=7, values=["off", "row", "move"])
        self.redatum_combo.grid(row=1, column=1, sticky="w", padx=(4, 12), pady=(6, 0))
        ttk.Label(params, text="RIS before every move / new row to hold alignment on the open-loop "
                               "stage. Keep the travel path clear; qualify switch repeatability first.",
                  foreground="#666").grid(row=1, column=2, columnspan=4, sticky="w", pady=(6, 0))

        ttk.Label(top, text="(passes + crosshatch come from the Etch table below, applied per array "
                            "type. Start step resumes mid-wafer.)",
                  foreground="#666").grid(row=3, column=0, columnspan=3, sticky="w", padx=4)

        ttk.Label(top, text="Plan:").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.plan_var = tk.StringVar(value="--")
        self.plan_lbl = ttk.Label(top, textvariable=self.plan_var, foreground="#2d7d46",
                                  font=("Segoe UI", 9, "bold"))
        self.plan_lbl.grid(row=4, column=1, columnspan=2, sticky="w", padx=4, pady=(6, 0))

        ttk.Label(top, text="Next pause:").grid(row=5, column=0, sticky="w", pady=(4, 0))
        self.next_pause_var = tk.StringVar(value="--")
        ttk.Label(top, textvariable=self.next_pause_var, foreground="#d9822b",
                  font=("Segoe UI", 10, "bold")).grid(row=5, column=1, columnspan=2, sticky="w",
                                                       padx=4, pady=(4, 0))

        ttk.Label(top, text="Est. remaining:").grid(row=6, column=0, sticky="w", pady=(4, 0))
        self.eta_var = tk.StringVar(value="--")
        ttk.Label(top, textvariable=self.eta_var, foreground="#2d7d46",
                  font=("Segoe UI", 9, "bold")).grid(row=6, column=1, columnspan=2, sticky="w",
                                                      padx=4, pady=(4, 0))

        ttk.Label(top, text="Progress:").grid(row=7, column=0, sticky="w", pady=(4, 0))
        self.progress_var = tk.StringVar(value="--")
        ttk.Label(top, textvariable=self.progress_var, foreground="#1f6feb",
                  font=("Segoe UI", 10, "bold")).grid(row=7, column=1, columnspan=2, sticky="w",
                                                       padx=4, pady=(4, 0))
        top.columnconfigure(1, weight=1)

        # -- editable etch table (passes per type; laser-PC adjustable) ---------
        self.etch = etch_params.load(ETCH_PARAMS)   # defaults to the design table if no file yet
        etchf = ttk.LabelFrame(
            root, padding=8,
            text="Etch table — passes per type (adjust here on the laser PC; applied by "
                 "Build jobs / Dry run / EXPOSE)")
        etchf.pack(fill="x", padx=8, pady=(4, 0))
        ncol = len(etch_params.DIAMETERS)
        for c, dia in enumerate(etch_params.DIAMETERS):
            ttk.Label(etchf, text="D%d" % dia, font=("Segoe UI", 9, "bold")).grid(
                row=0, column=1 + c, padx=6)
        ttk.Label(etchf, text="Pass angles", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=1 + ncol, padx=6)
        self.etch_pass = {}   # (short, dia) -> StringVar
        self.etch_ang = {}    # short -> StringVar
        pg, ag, hatch = etch_params.to_grid(self.etch)
        for r, (name, short) in enumerate(etch_params.LATTICES, start=1):
            ttk.Label(etchf, text=name, font=("Segoe UI", 9, "bold")).grid(
                row=r, column=0, sticky="w")
            for c, dia in enumerate(etch_params.DIAMETERS):
                v = tk.StringVar(value=str(pg.get((short, dia), "")))
                self.etch_pass[(short, dia)] = v
                ttk.Spinbox(etchf, from_=1, to=100000, width=7, textvariable=v).grid(
                    row=r, column=1 + c, padx=6, pady=2)
            av = tk.StringVar(value=ag.get(short, ""))
            self.etch_ang[short] = av
            ttk.Entry(etchf, width=10, textvariable=av).grid(row=r, column=1 + ncol, padx=6)
        ttk.Label(etchf, text="Hatch (mm):").grid(row=3, column=0, sticky="e", pady=(6, 0))
        self.hatch_var = tk.StringVar(value=str(hatch))
        ttk.Entry(etchf, width=7, textvariable=self.hatch_var).grid(
            row=3, column=1, sticky="w", pady=(6, 0))
        ttk.Label(etchf, text="speed 400 mm/s + crosshatch (fixed); power/freq stay on the "
                              "WinLase profile", foreground="#666").grid(
            row=3, column=2, columnspan=ncol - 1, sticky="w", pady=(6, 0))
        self.etch_save_btn = ttk.Button(etchf, text="Save etch table", command=self._save_etch)
        self.etch_save_btn.grid(row=3, column=1 + ncol, sticky="we", pady=(6, 0))
        self.etch_reset_btn = ttk.Button(etchf, text="Reset to defaults", command=self._reset_etch)
        self.etch_reset_btn.grid(row=4, column=1 + ncol, sticky="we")

        # -- buttons (two rows) -------------------------------------------------
        self.buttons = {}
        row_a = ttk.Frame(root, padding=(8, 4))
        row_a.pack(fill="x")
        for i, (name, fn) in enumerate([
                ("Pick set folder", self.pick_set_folder),
                ("Info", self.info),
                ("Build jobs", self.build),
                ("List / pre-flight", self.list_preflight)]):
            b = ttk.Button(row_a, text=name, command=fn)
            b.grid(row=0, column=i, padx=3, sticky="we")
            row_a.columnconfigure(i, weight=1)
            self.buttons[name] = b

        row_b = ttk.Frame(root, padding=(8, 0))
        row_b.pack(fill="x")
        for i, (name, fn) in enumerate([
                ("Dry run", self.dry_run),
                ("EXPOSE (arm)", self.expose),
                ("Home", self.home),
                ("Extract", self.extract)]):
            b = ttk.Button(row_b, text=name, command=fn)
            b.grid(row=0, column=i, padx=3, sticky="we")
            row_b.columnconfigure(i, weight=1)
            self.buttons[name] = b

        self.stop_btn = tk.Button(root, text="STOP  (controlled stop -- not an e-stop)",
                                  command=self.stop, bg="#c0392b", fg="white",
                                  font=("Segoe UI", 11, "bold"))
        self.stop_btn.pack(fill="x", padx=8, pady=4)

        self.log_txt = scrolledtext.ScrolledText(root, height=20, wrap="none", bg="black",
                                                  fg="#d0d0d0", font=("Consolas", 9))
        self.log_txt.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log_txt.configure(state="disabled")

        self.refresh_sets()
        self.log("Ready. Pick a set, then: Build jobs -> List/pre-flight -> Dry run -> EXPOSE.")
        self.log("STOP is a CONTROLLED stop (between arrays / at a mask pause), not an e-stop.")
        self._pump()

    # -- sets root memory -------------------------------------------------------
    def _load_root(self):
        try:
            saved = ROOT_MEMO.read_text(encoding="utf-8").strip()
            if saved:
                return saved
        except OSError:
            pass
        return str(DEFAULT_ROOT if DEFAULT_ROOT.is_dir() else HERE)

    def _save_root(self, d):
        try:
            ROOT_MEMO.write_text(d, encoding="utf-8")
        except OSError:
            pass

    def browse_root(self):
        d = filedialog.askdirectory(initialdir=self.root_var.get() or str(HERE))
        if d:
            self.root_var.set(d)
            self._save_root(d)
            self.refresh_sets()

    def pick_set_folder(self):
        """Browse straight to a built set folder (one that holds plan.json)."""
        d = filedialog.askdirectory(title="Pick a set folder (contains plan.json)",
                                    initialdir=self.root_var.get() or str(DEFAULT_ROOT))
        if not d:
            return
        p = Path(d)
        if not is_set_dir(p):
            messagebox.showwarning("UV Laser Exposure",
                                   "That folder has no plan.json.\n"
                                   "Pick a built set folder (output/sets/<name>).")
            return
        self.root_var.set(str(p.parent))
        self._save_root(str(p.parent))
        self.refresh_sets()
        if p.name in self.set_combo["values"]:
            self.set_var.set(p.name)
        self._show_set_info()

    def refresh_sets(self):
        root = Path(self.root_var.get())
        sets = sorted(p.name for p in root.iterdir() if is_set_dir(p)) if root.is_dir() else []
        self.set_combo["values"] = sets
        if sets and self.set_var.get() not in sets:
            self.set_var.set(sets[0])
        elif not sets:
            self.set_var.set("")
        self._show_set_info()

    def selected_set(self):
        name = self.set_var.get()
        if not name:
            messagebox.showwarning("UV Laser Exposure",
                                   "Pick a set first (a folder with plan.json).")
            return None
        return Path(self.root_var.get()) / name

    def _show_set_info(self):
        """Refresh passes + plan summary when the selected set changes."""
        name = self.set_var.get()
        if not name:
            self.plan_var.set("--")
            self.plan_lbl.config(foreground="#2d7d46")
            self.plan_total = 0
            return
        data = read_plan(Path(self.root_var.get()) / name)
        text, n_steps, feasible = plan_summary(data)
        self.plan_total = n_steps
        self.plan_var.set(text)
        if data is None:
            self.plan_lbl.config(foreground="#b58900")  # amber: no/unreadable plan
        elif feasible is False:
            self.plan_lbl.config(foreground="#c0392b")  # red: infeasible
        else:
            self.plan_lbl.config(foreground="#2d7d46")  # green

    # -- etch table (passes + crosshatch angles per type) -----------------------
    def _read_etch_from_ui(self):
        passes = {k: v.get() for k, v in self.etch_pass.items()}
        angles = {k: v.get() for k, v in self.etch_ang.items()}
        return etch_params.apply_grid(self.etch, passes, angles, self.hatch_var.get())

    def _persist_etch(self):
        """Write the current table to etch_params.json so the child scripts pick it up."""
        try:
            etch_params.save(self._read_etch_from_ui(), ETCH_PARAMS)
            return True
        except OSError as exc:
            messagebox.showwarning("UV Laser Exposure", "could not write etch table: %s" % exc)
            return False

    def _save_etch(self):
        if self._persist_etch():
            self.log("[etch] saved -> %s (applies to Build jobs / Dry run / EXPOSE)." % ETCH_PARAMS.name)

    def _reset_etch(self):
        self.etch = etch_params.defaults()
        pg, ag, hatch = etch_params.to_grid(self.etch)
        for (short, dia), v in self.etch_pass.items():
            v.set(str(pg.get((short, dia), "")))
        for short, v in self.etch_ang.items():
            v.set(ag.get(short, ""))
        self.hatch_var.set(str(hatch))
        self.log("[etch] reset to the default table (click Save etch table to persist).")

    def _start_step(self):
        try:
            n = int(self.start_var.get())
        except (ValueError, tk.TclError):
            n = 0
        return max(0, n)

    def _flash_start(self):
        """Highlight the pre-filled resume step so a one-click EXPOSE is obvious."""
        try:
            self.resume_hint.set("<- resume here; click EXPOSE")
        except tk.TclError:
            pass

    def _clear_resume_hint(self):
        try:
            self.resume_hint.set("")
        except tk.TclError:
            pass

    def _port(self):
        return (self.port_var.get() or DEFAULT_PORT).strip() or DEFAULT_PORT

    # -- subprocess plumbing ----------------------------------------------------
    def _reset_progress(self):
        self.p_step = None
        self.p_row = None
        self.p_phase = None
        self.p_array = None
        self.p_state = None

    def _run(self, argv):
        if self.busy:
            return
        for flag in (STOP_FLAG, RESUME_FLAG):
            try:
                flag.unlink()
            except OSError:
                pass
        self._set_busy(True)
        self.eta_var.set("--")
        self.next_pause_var.set("--")
        self._pending_mask_label = None
        self._clear_resume_hint()
        self._reset_progress()
        self._update_progress()
        cmd = [PYCON, "-u"] + [str(a) for a in argv]
        self.log("\n$ " + " ".join(cmd[2:]))

        def worker():
            try:
                p = subprocess.Popen(cmd, cwd=str(HERE), stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1)
                for line in iter(p.stdout.readline, ""):
                    self.q.put(line.rstrip("\n"))
                p.stdout.close()
                self.q.put(("__done__", p.wait()))
            except Exception as exc:
                self.q.put("ERROR launching: %s" % exc)
                self.q.put(("__done__", -1))

        threading.Thread(target=worker, daemon=True).start()

    def _pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "__done__":
                    self.log("[exit %s]" % item[1])
                    self._close_mask_modal()
                    self.p_state = "done"
                    self._update_progress()
                    self._set_busy(False)
                else:
                    self._parse_line(item)
                    self.log(item)
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    def _parse_line(self, line):
        if not isinstance(line, str):
            return
        s = line.strip()
        low = s.lower()
        if s.startswith("[eta] "):
            self._parse_eta(s[6:].strip())
        # The "[<n> mask] <label>" step line carries the SPECIFIC pause reason; stash it so
        # the modal (opened by the generic "[mask] paused" line that follows) can name it.
        mstep = RE_MASK_STEP.match(s)
        if mstep:
            self._pending_mask_label = mstep.group(1).strip()
        if low.startswith("[mask]"):
            rest = s[len("[mask]"):].strip()
            if rest.lower().startswith("resumed"):
                # The run loop consumed the Resume flag. Do NOT reopen the modal -- it stays
                # up (showing "Resuming...") until the next mark actually starts.
                self.p_state = "RESUMING -- waiting for WinLase to start the mark"
                self._update_progress()
                return
            self.p_state = "MASK PAUSE -- mask, then Resume"
            self._update_progress()
            self._open_mask_modal(self._pending_mask_label or rest
                                  or "Mask the completed arrays, then Resume.")
            return
        # Resume confirmed THROUGH the marking API: WinLase has started the next mark (or the
        # simulate stand-in has). Only now do we close the mask-pause modal. A mark line can
        # only appear when the run is NOT paused, so an open modal here means we just resumed.
        if self.mask_win is not None and (low.startswith("[mark] started")
                                          or "simulate mark" in low):
            self._close_mask_modal()
            self.p_state = "EXPOSING"
            self._update_progress()
        if low.startswith("[expose]") or " exposing" in low or low.startswith("exposing"):
            self.p_state = "EXPOSING"
        if "all steps complete" in low or "[resume-step] done" in low:
            self.start_var.set("0")          # run finished; next run starts fresh
            self._clear_resume_hint()
        m = RE_RESUME.search(s)
        if m:
            n = int(m.group(1))
            self.start_var.set(str(n))       # pre-fill so EXPOSE resumes in one click
            self.p_state = "STOPPED -- ready to resume at step %d" % n
            self._flash_start()
            self.log("[resume] Start step set to %d. Wash + mask now; if the wafer left the "
                     "stage, re-teach the reference, then click EXPOSE to resume (one click)." % n)
            self._update_progress()
            return
        m = RE_STEP.search(s)
        if m:
            self.p_step = (int(m.group(1)), int(m.group(2)))
        m = RE_ROW.search(s)
        if m:
            self.p_row = int(m.group(1))
        m = RE_PHASE.search(s)
        if m:
            self.p_phase = m.group(1).upper()
        m = RE_ARRAY.search(s)
        if m:
            self.p_array = "r%sc%s" % (m.group(1), m.group(2))
        self._update_progress()

    def _parse_eta(self, body):
        """Split an [eta] line into the two operator-facing readouts: time until the next
        wash/mask pause (the actionable one), and the remaining/total run estimate. Lines
        that carry neither (e.g. 'estimating...', 'done | total elapsed ..') fall back to
        showing their text in the Est. remaining field and leave Next pause untouched."""
        mr = RE_ETA_REMAIN.search(body)
        mt = RE_ETA_TOTAL.search(body)
        if mr or mt:
            bits = []
            if mr:
                bits.append("~%s remaining" % mr.group(1))
            if mt:
                bits.append("~%s total" % mt.group(1))
            self.eta_var.set("   ·   ".join(bits))
        else:
            self.eta_var.set("estimating..." if "estimating" in body.lower() else body)
        mp = RE_ETA_PAUSE.search(body)
        if mp:
            self.next_pause_var.set("~%s   (%s)" % (mp.group(2), mp.group(1).lower()))
        elif RE_ETA_NOPAUSE.search(body):
            self.next_pause_var.set("none remaining")

    def _update_progress(self):
        parts = []
        if self.p_step is not None:
            parts.append("step %d/%d" % self.p_step)
        elif self.plan_total:
            parts.append("step ?/%d" % self.plan_total)
        if self.p_row is not None:
            parts.append("row %d" % self.p_row)
        if self.p_phase:
            parts.append("phase %s" % self.p_phase)
        if self.p_array:
            parts.append(self.p_array)
        if self.p_state:
            parts.append(self.p_state)
        self.progress_var.set("  |  ".join(parts) if parts else "--")

    def _set_busy(self, busy):
        self.busy = busy
        for b in self.buttons.values():
            b.config(state="disabled" if busy else "normal")
        for w in (self.start_spin, self.port_entry, self.focus_chk,
                  self.etch_save_btn, self.etch_reset_btn):
            w.config(state="disabled" if busy else "normal")
        # readonly combobox re-enables to "readonly", not "normal" (else it turns editable)
        self.redatum_combo.config(state="disabled" if busy else "readonly")

    def log(self, text):
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", text + "\n")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    # -- mask pause modal -------------------------------------------------------
    def _open_mask_modal(self, label):
        """Pop the mask-pause prompt. Resume writes the resume flag the run loop waits on;
        the window then stays up (button -> "Resuming...") until the run confirms by
        starting the next mark -- see _parse_line and _resume."""
        # Drop any stale resume flag so THIS pause needs a fresh, deliberate Resume.
        try:
            RESUME_FLAG.unlink()
        except OSError:
            pass
        if self.mask_win is not None and self.mask_win.winfo_exists():
            self.mask_lbl.config(text=label)
            self._reset_modal_controls()   # a fresh pause: undo any "Resuming..." state
            self.mask_win.lift()
            return
        win = tk.Toplevel(self.root)
        win.title("Mask pause -- Resume when done")
        win.configure(bg="#161616")
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", lambda: None)  # force an explicit Resume or STOP
        tk.Label(win, text="MASK PAUSE (controlled)", bg="#161616", fg="#f0c000",
                 font=("Segoe UI", 13, "bold")).pack(padx=16, pady=(14, 6))
        self.mask_lbl = tk.Label(win, text=label, bg="#161616", fg="#e0e0e0",
                                 font=("Segoe UI", 11), wraplength=460, justify="center")
        self.mask_lbl.pack(padx=16, pady=(0, 8))
        tk.Label(win, text="Mask the completed arrays, then click Resume to continue.\n"
                           "The run loop is paused and waiting for the resume flag.",
                 bg="#161616", fg="#9aa0a6", font=("Segoe UI", 9), justify="center").pack(
            padx=16, pady=(0, 6))
        # Confirmation line: after Resume, shows we are waiting for WinLase to start marking;
        # the window only closes when that mark actually begins.
        self.mask_status_lbl = tk.Label(win, text="", bg="#161616", fg="#2d7d46",
                                        font=("Segoe UI", 10, "bold"), wraplength=460,
                                        justify="center")
        self.mask_status_lbl.pack(padx=16, pady=(0, 8))
        bar = tk.Frame(win, bg="#161616")
        bar.pack(padx=16, pady=(0, 16), fill="x")
        self.mask_resume_btn = tk.Button(bar, text="Resume exposure", command=self._resume,
                                         bg="#2d7d46", fg="white",
                                         font=("Segoe UI", 11, "bold"), width=18)
        self.mask_resume_btn.pack(side="left", expand=True, padx=4)
        tk.Button(bar, text="STOP (controlled)", command=self._stop_from_modal, bg="#c0392b",
                  fg="white", font=("Segoe UI", 11, "bold"), width=18).pack(side="left",
                                                                            expand=True, padx=4)
        win.update_idletasks()
        try:
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            ww, wh = win.winfo_width(), win.winfo_height()
            win.geometry("+%d+%d" % (rx + max(0, (rw - ww) // 2), ry + max(0, (rh - wh) // 3)))
        except Exception:
            pass
        win.lift()
        win.attributes("-topmost", True)
        self.mask_win = win

    def _reset_modal_controls(self):
        """Return the modal to its fresh 'awaiting Resume' state (used when the same window
        is reused for the next pause after a prior Resume left it in 'Resuming...')."""
        if self.mask_resume_btn is not None:
            try:
                self.mask_resume_btn.config(state="normal", text="Resume exposure")
            except tk.TclError:
                pass
        if self.mask_status_lbl is not None:
            try:
                self.mask_status_lbl.config(text="")
            except tk.TclError:
                pass

    def _close_mask_modal(self):
        if self.mask_win is not None:
            try:
                self.mask_win.destroy()
            except Exception:
                pass
        self.mask_win = None
        self.mask_lbl = None
        self.mask_resume_btn = None
        self.mask_status_lbl = None

    def _resume(self):
        try:
            RESUME_FLAG.write_text("resume", encoding="utf-8")
        except OSError as exc:
            messagebox.showwarning("UV Laser Exposure", "could not write resume flag: %s" % exc)
            return
        self.log("[resume] operator resumed -- wrote %s; waiting for WinLase to start the "
                 "next mark before this window closes." % RESUME_FLAG.name)
        # Do NOT close on the click. The window stays up until the run confirms the resume by
        # actually starting the next mark ("[mark] started" armed / "SIMULATE mark" dry-run),
        # closed in _parse_line. An open window therefore always means "not marking yet".
        if self.mask_resume_btn is not None:
            try:
                self.mask_resume_btn.config(state="disabled", text="Resuming...")
            except tk.TclError:
                pass
        if self.mask_status_lbl is not None:
            try:
                self.mask_status_lbl.config(
                    text="Resuming -- waiting for WinLase to start the mark...")
            except tk.TclError:
                pass
        self.p_state = "RESUMING -- waiting for WinLase to start the mark"
        self._update_progress()

    def _stop_from_modal(self):
        self.stop()
        self._close_mask_modal()

    # -- actions ----------------------------------------------------------------
    def info(self):
        self._run([OPTISCAN, "--port", self._port(), "info"])

    def build(self):
        s = self.selected_set()
        if s:
            self._persist_etch()
            self._run([BUILD, s, "--etch-params", ETCH_PARAMS])

    def list_preflight(self):
        s = self.selected_set()
        if s:
            self._run([EXPOSE, s, "--list", "--port", self._port()])

    def _expose_argv(self, s, armed):
        argv = [EXPOSE, s, "--yes", "--port", self._port(),
                "--etch-params", ETCH_PARAMS,
                "--start-step", self._start_step(),
                "--redatum", (self.redatum_var.get() or "off"),
                "--stop-flag", STOP_FLAG, "--resume-flag", RESUME_FLAG]
        if armed:
            argv.insert(2, "--arm")
        if self.focus_var.get():
            argv.append("--focus")
        return argv

    def _passes_summary(self):
        """min..max passes across the current etch table, for the confirm dialog."""
        try:
            vals = [int(t["passes"]) for t in self._read_etch_from_ui()["types"].values()]
            return "%d..%d passes/type" % (min(vals), max(vals)) if vals else "per-type passes"
        except (ValueError, KeyError, TypeError):
            return "per-type passes"

    def dry_run(self):
        s = self.selected_set()
        if not s:
            return
        self._persist_etch()
        self._run(self._expose_argv(s, armed=False))

    def expose(self):
        s = self.selected_set()
        if not s:
            return
        if not self._persist_etch():
            return
        data = read_plan(s)
        _text, _steps, feasible = plan_summary(data)
        warn = ""
        if feasible is False:
            warn = ("\n\nWARNING: plan.stage.feasible is FALSE -- the pre-flight will refuse "
                    "to move. Rebuild with a rotation that fits before arming.")
        start = self._start_step()
        resume_txt = ""
        if start > 0:
            resume_txt = ("\n    RESUMING at schedule step %d (earlier steps are skipped -- "
                          "re-teach the reference if the wafer was removed)" % start)
        if not messagebox.askyesno(
                "ARM THE LASER",
                "Fire the UV laser and EXPOSE this set:\n\n"
                "    %s\n    %s (from the etch table)%s\n\n"
                "Close the WinLase GUI first, and keep a hand on the e-stop.\n"
                "STOP here is a CONTROLLED stop (between arrays / at a mask pause), "
                "NOT an emergency stop.%s\n\nProceed?"
                % (s.name, self._passes_summary(), resume_txt, warn),
                icon="warning", default="no"):
            self.log("expose cancelled.")
            return
        self._run(self._expose_argv(s, armed=True))

    def home(self):
        self._run([OPTISCAN, "--port", self._port(), "home", "--yes"])

    def extract(self):
        self._run([OPTISCAN, "--port", self._port(), "extract", "--yes"])

    def stop(self):
        try:
            STOP_FLAG.write_text("stop", encoding="utf-8")
            self.log("STOP requested -- expose/dry-run will controlled-stop at the next array "
                     "or mask pause (use the hardware e-stop for a true emergency).")
        except OSError as exc:
            messagebox.showwarning("UV Laser Exposure", "could not write stop flag: %s" % exc)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
