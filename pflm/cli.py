"""Command-line entry point (§5.6): ``python -m pflm.cli inspect|build``.

  python -m pflm.cli inspect <gds>
  python -m pflm.cli build   <gds> [--pinfin 3/0] [--bbox 4/0] [--align 5/0]
                                   [--set NAME] [--rotation auto|0|90|180|270]
                                   [--stride N] [--global-x UM] [--global-y UM]
                                   [--no-backside] [--output DIR]

No hardware imports.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .layers import inspect_layers
from .plan import build_set

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETS_DIR = REPO_ROOT / "output" / "sets"

# Wafer-flat direction on the stage -> design rotation (deg). The flat-at-bottom GDS is
# turned so its flat points at the physical flat: front=-Y, right=+X, back=+Y, left=-X.
JIG_FLAT_DEG = {"front": 0, "right": 90, "back": 180, "left": 270}


def _cmd_inspect(args) -> int:
    path = Path(args.gds)
    if not path.is_file():
        print(f"input not found: {path}")
        return 1
    layers = inspect_layers(path)
    if not layers:
        print("No layers found.")
        return 1
    print(f"Layers in {path.name} (area-sorted):\n")
    for entry in layers:
        print(f"  {entry.selector:<10} {entry.describe()}")
    return 0


def _cmd_build(args) -> int:
    path = Path(args.gds)
    if not path.is_file():
        print(f"input not found: {path}")
        return 1
    set_name = args.set or path.stem
    set_dir = Path(args.output) if args.output else (DEFAULT_SETS_DIR / set_name)

    rotation = args.rotation
    if rotation != "auto":
        rotation = int(rotation)
    # --jig-flat is a convenience that maps the wafer-flat direction to a rotation and
    # overrides --rotation (front=-Y=0, right=+X=90, back=+Y=180, left=-X=270).
    if getattr(args, "jig_flat", None):
        rotation = JIG_FLAT_DEG[args.jig_flat]

    build_set(
        path, set_dir,
        pinfin=args.pinfin, bbox=args.bbox, align=args.align,
        backside=not args.no_backside,
        rotation_deg=rotation,
        within_row_stride=args.stride,
        global_offset_um=(args.global_x, args.global_y),
        params_csv=args.params,
        pin_mode=("circle" if args.circles else "polygon"),
        ablate_dead_space=args.ablate_dead_space,
        cell=args.cell,
        dead_space_wash=not args.no_dead_space_wash,
    )
    print(f"Set folder: {set_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pflm.cli",
        description="PFLM exposure prep: inspect a wafer GDS or build a set folder.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser("inspect", help="print the layer table for a GDS")
    p_inspect.add_argument("gds", help="source GDS/OAS/DXF")
    p_inspect.set_defaults(func=_cmd_inspect)

    p_build = sub.add_parser("build", help="build a set folder from a GDS")
    p_build.add_argument("gds", help="source GDS/OAS/DXF")
    p_build.add_argument("--pinfin", default="3/0", help="pinfin/geometry layer selector")
    p_build.add_argument("--bbox", default="4/0", help="per-array bbox layer selector")
    p_build.add_argument("--align", default="5/0", help="alignment-mark layer selector")
    p_build.add_argument("--set", default=None, help="set name (default: GDS stem)")
    p_build.add_argument("--rotation", default="auto",
                         help="auto|0|90|180|270 design rotation")
    p_build.add_argument("--jig-flat", choices=("front", "right", "back", "left"), default=None,
                         help="convenience for --rotation from the wafer-flat direction on the "
                              "stage: front(-Y)=0, right(+X)=90, back(+Y)=180, left(-X)=270. "
                              "Overrides --rotation when given.")
    p_build.add_argument("--stride", type=int, default=2,
                         help="within-row masking stride (§2.2)")
    p_build.add_argument("--global-x", type=float, default=0.0,
                         help="baked-in DXF X correction, microns (0 = none; bulk placement is in the "
                              "taught reference -- this is the knob for future small corrections)")
    p_build.add_argument("--global-y", type=float, default=0.0,
                         help="baked-in DXF Y correction, microns (0 = none; see --global-x)")
    p_build.add_argument("--params", default=None,
                         help="design manifest CSV with per-array etch params (passes, fill angles)")
    p_build.add_argument("--circles", action="store_true",
                         help="export round pins as true DXF CIRCLE entities (fast/compact; for round-pin arrays)")
    p_build.add_argument("--ablate-dead-space", action="store_true",
                         help="prepend a dead-space ablation phase: etch each chip's cell "
                              "footprint minus its pin-field box (nothing in the pin box), per "
                              "chip, no masks, then one wash pause before the pinfins")
    p_build.add_argument("--cell", default="4/0",
                         help="chip-footprint layer for the dead-space phase (default: 4/0)")
    p_build.add_argument("--no-dead-space-wash", action="store_true",
                         help="skip the wash/clean pause between dead-space and pinfins")
    p_build.add_argument("--no-backside", action="store_true",
                         help="record backside=false in the plan")
    p_build.add_argument("--output", default=None,
                         help="explicit set directory (default: output/sets/<name>)")
    p_build.set_defaults(func=_cmd_build)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
