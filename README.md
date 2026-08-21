# UV-Laser-Exposure

Row-by-row UV-laser exposure of pinfin/heater arrays on the **backside** of a 100 mm
wafer. Takes a GDS, lets you pick the layer that holds the pinfins (and, optionally,
the layer that holds each array's bounding box), and exposes the arrays **one row at a
time** — always holding the array currently being exposed at the **center of the laser
field** — so completed rows can be masked off to keep them clean.

Modeled on, and reusing a lot of code from, the sibling **UV Laser Singulation** project.
Same core contract: **the DXF/job origin (0,0) is the laser field center and the laser
runs with auto-centering OFF**; the laser is fixed and a Prior OptiScan III stage moves
the wafer so each target sits under the beam.

## Two halves (mirrors Singulation)

| Half | Where | Python | Deps | Does |
|---|---|---|---|---|
| **Prep** (`pflm/`, `prep_app/`) | design PC (online) | 3.11+ | klayout, ezdxf, PySide6 | Load GDS → pick pinfin/bbox/align layers → detect the arrays (10 in the v2 layout), group into rows, order top→bottom → write a **set folder** (`plan.json` + one centered DXF per array + manifest). PySide6/QML preview app. |
| **Run** (`laser_pc/`) | offline laser PC | 3.8 | pyserial, pywin32 (local wheels) | Teach one stage reference, build one WinLase `.wlj` per array, then step the stage row-by-row (top→bottom) marking each centered array, pausing between rows so you can mask. Tkinter launcher. SIMULATE by default; `--arm` fires. |

The two halves meet at a **set folder** under `output/sets/<name>/`. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the coordinate frames, the `plan.json`
schema, the wafer→stage transform, and the full module contract.

## Quick start (prep half)

```bash
pip install -r requirements-prep.txt
# inspect layers in a GDS
python -m pflm.cli inspect "../081026_PFLM_Heaters.gds"
# build a set (defaults: pinfin=3/0, bbox=4/0, align=5/0, top-to-bottom)
python -m pflm.cli build "../081026_PFLM_Heaters.gds" --pinfin 3/0 --bbox 4/0 --set 081026_PFLM_Heaters
# or launch the prep app
python prep_app/prep_app.py
```

## Status

- **Prep half** — verifiable offline against a real GDS; this is the primary deliverable.
- **Laser-PC half** — adapted from Singulation's vetted `optiscan.py` / `winlase_build_jobs.py` /
  `dice_wafer.py`. The serial + WinLase-COM paths **cannot be tested off the machine** and
  require on-hardware bring-up. The stage calibration is now **ported from the dialed-in
  Singulation setup and confirmed definitive** — refine it with a tiny DXF `global_offset`, not
  by re-teaching. The genuine remaining caveat: the **WinLase-COM and serial paths are still
  un-verified off the machine.**

> ⚠️ Laser safety: exposure runs default to SIMULATE. Arming requires `--arm` plus a typed
> confirmation and a countdown. The UI STOP is a *controlled* stop between rows/passes — the
> only emergency stop is the hardware e-stop.
