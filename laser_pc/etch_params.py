"""Editable per-array-type etch table for the laser PC (passes + crosshatch angles).

Defaults to the design table (P = passes, S = speed mm/s, crosshatch at the listed
angles, 0.01 mm hatch). The run launcher (expose_ui.py) lets the operator adjust it on
the laser PC and saves it to ``etch_params.json`` next to this file; winlase_build_jobs.py
and expose_wafer.py load it (``--etch-params``) and apply it BY ARRAY TYPE, overriding the
values baked into plan.json at prep time. Power/frequency are NOT here -- they stay fixed
by the WinLase profile (the read-only safety gate is unchanged).

Python 3.8, standard library only (json). Type keys: "D<dia>_sq" / "D<dia>_hex".
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent / "etch_params.json"

# Display grid order (matches the design spreadsheet): rows Square/Hex, cols D300/D100/D50.
LATTICES = (("Square", "sq"), ("Hex", "hex"))
DIAMETERS = (300, 100, 50)

# --- Defaults = the etch table -------------------------------------------------
DEFAULT = {
    "fill_style": "crosshatch",
    "hatch_mm": 0.01,
    "types": {
        "D50_sq":  {"diameter_um": 50,  "pitch_um": 100, "lattice": "square", "passes": 44, "speed_mm_s": 400, "fill_angles_deg": [0, 90]},
        "D100_sq": {"diameter_um": 100, "pitch_um": 150, "lattice": "square", "passes": 37, "speed_mm_s": 400, "fill_angles_deg": [0, 90]},
        "D300_sq": {"diameter_um": 300, "pitch_um": 350, "lattice": "square", "passes": 20, "speed_mm_s": 400, "fill_angles_deg": [0, 90]},
        "D50_hex":  {"diameter_um": 50,  "pitch_um": 100, "lattice": "hex", "passes": 42, "speed_mm_s": 400, "fill_angles_deg": [-30, 30]},
        "D100_hex": {"diameter_um": 100, "pitch_um": 150, "lattice": "hex", "passes": 30, "speed_mm_s": 400, "fill_angles_deg": [-30, 30]},
        "D300_hex": {"diameter_um": 300, "pitch_um": 350, "lattice": "hex", "passes": 18, "speed_mm_s": 400, "fill_angles_deg": [-30, 30]},
    },
}


def type_key(dia_um, lattice_short) -> str:
    return "D%d_%s" % (int(dia_um), lattice_short)


def defaults() -> dict:
    return copy.deepcopy(DEFAULT)


def load(path=DEFAULT_PATH) -> dict:
    """Load the table, or DEFAULT if the file is absent/unreadable. Missing type entries
    are backfilled from DEFAULT so a partial/hand-edited file can't drop a type."""
    data = None
    try:
        p = Path(path)
        if p.is_file():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("types"), dict):
                data = raw
    except (OSError, ValueError, TypeError):
        data = None
    out = defaults()
    if data:
        out["fill_style"] = data.get("fill_style", out["fill_style"])
        out["hatch_mm"] = data.get("hatch_mm", out["hatch_mm"])
        for k, v in data["types"].items():
            if k in out["types"] and isinstance(v, dict):
                out["types"][k].update({kk: v[kk] for kk in
                                        ("passes", "speed_mm_s", "fill_angles_deg",
                                         "diameter_um", "pitch_um", "lattice") if kk in v})
            elif isinstance(v, dict):
                out["types"][k] = v
    return out


def save(data, path=DEFAULT_PATH) -> None:
    Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def params_for_type(data, array_type):
    """Return the etch dict for a type key ("D50_sq"), or None if not present/None."""
    if not array_type or not isinstance(data, dict):
        return None
    return (data.get("types") or {}).get(array_type)


# --- UI grid <-> table ---------------------------------------------------------
def to_grid(data):
    """(passes[(short,dia)], angles_str[short], hatch_mm) for the editor grid."""
    passes = {}
    angles = {}
    for _name, short in LATTICES:
        for dia in DIAMETERS:
            t = data["types"].get(type_key(dia, short), {})
            passes[(short, dia)] = t.get("passes", "")
        # angles are per lattice (shared across diameters); read the D50 entry
        t = data["types"].get(type_key(50, short), {})
        angles[short] = "/".join(str(a) for a in t.get("fill_angles_deg", []))
    return passes, angles, data.get("hatch_mm", 0.01)


def apply_grid(data, passes, angles_str, hatch_mm=None):
    """Update `data` in place from editor values. `passes` keyed (short,dia)->int;
    `angles_str` keyed short->"a/b"; hatch optional. speed_mm_s left as-is (400)."""
    for _name, short in LATTICES:
        try:
            ang = [float(a) for a in str(angles_str.get(short, "")).replace(",", "/").split("/") if a.strip() != ""]
        except ValueError:
            ang = None
        for dia in DIAMETERS:
            t = data["types"].setdefault(type_key(dia, short),
                                         copy.deepcopy(DEFAULT["types"][type_key(dia, short)]))
            try:
                t["passes"] = int(float(passes[(short, dia)]))
            except (ValueError, KeyError, TypeError):
                pass
            if ang is not None:
                t["fill_angles_deg"] = ang
    if hatch_mm is not None:
        try:
            data["hatch_mm"] = float(hatch_mm)
        except (ValueError, TypeError):
            pass
    return data
