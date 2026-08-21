"""Layer inspection + selector parsing (§5.1).

Adapted from the Singulation project:
  - ``slicing/run_splitter.py::inspect_layers`` / ``LayerInfo``
  - ``slicing/split_klayout.py`` selector helpers
    (``parse_layer_spec`` / ``layer_matches``)

Load GDS via ``pya.Layout().read(path)``; DXF via ``LoadLayoutOptions().dxf_unit``.
No hardware imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:  # KLayout GUI/macro embeds pya; standalone uses the klayout wheel.
    import pya  # type: ignore
except ImportError:
    import klayout.db as pya

# DXF drawing units: the references use 1 unit = 1 mm = 1000 um.
DXF_UNIT_UM = 1_000.0

# Historical default source layer (a DXF layer "0" imports as 0/0).
SOURCE_LAYER = 0
SOURCE_DATATYPE = 0
DEFAULT_LAYER_NAME = "0"


@dataclass(frozen=True)
class LayerInfo:
    """One layer in the source file, with enough detail to choose sensibly."""

    selector: str        # what to pass as a selector, e.g. "3/0"
    name: str            # "" if unnamed
    layer: int
    datatype: int
    polygons: int
    paths: int
    widths_um: tuple     # observed path widths
    area_mm2: float
    bbox_mm: tuple       # (l, b, r, t) or None

    def describe(self) -> str:
        label = self.name or f"{self.layer}/{self.datatype}"
        parts = [f"{label:<20}"]
        parts.append(f"{self.polygons:>6} poly")
        parts.append(f"{self.paths:>5} path")
        parts.append(f"{self.area_mm2:>12.4f} mm2")
        if self.bbox_mm is not None:
            l, b, r, t = self.bbox_mm
            parts.append(f"bbox mm: ({l:.2f},{b:.2f},{r:.2f},{t:.2f})")
        if self.widths_um:
            shown = ", ".join(f"{w:g}" for w in self.widths_um[:4])
            more = ", ..." if len(self.widths_um) > 4 else ""
            parts.append(f"widths um: {shown}{more}")
        return "  ".join(parts)


def read_layout(path) -> "pya.Layout":
    """Read a GDS/OAS/DXF into a fresh Layout (DXF forced to mm units)."""
    path = Path(path)
    layout = pya.Layout()
    if path.suffix.lower() == ".dxf":
        options = pya.LoadLayoutOptions()
        options.dxf_unit = DXF_UNIT_UM
        layout.read(str(path), options)
    else:
        layout.read(str(path))
    return layout


def inspect_layers(path) -> list[LayerInfo]:
    """List every layer in a DXF/GDS/OAS, merged, area-sorted descending."""
    layout = read_layout(path)
    scale = layout.dbu / 1000.0  # dbu -> mm
    found: list[LayerInfo] = []
    for index in layout.layer_indices():
        info = layout.get_info(index)
        name = str(getattr(info, "name", "") or "")
        polygons = paths = 0
        widths: set[float] = set()
        for cell in layout.each_cell():
            for shape in cell.each_shape(index):
                if shape.is_path():
                    paths += 1
                    widths.add(round(abs(shape.path_width) * layout.dbu, 4))
                else:
                    polygons += 1

        region = pya.Region()
        for top in layout.top_cells():
            region += pya.Region(top.begin_shapes_rec(index))
        region = region.merged()
        box = region.bbox() if not region.is_empty() else None
        found.append(
            LayerInfo(
                selector=name if name else f"{info.layer}/{info.datatype}",
                name=name,
                layer=info.layer,
                datatype=info.datatype,
                polygons=polygons,
                paths=paths,
                widths_um=tuple(sorted(widths)),
                area_mm2=region.area() * (layout.dbu ** 2) / 1_000_000.0,
                bbox_mm=None if box is None else (
                    box.left * scale, box.bottom * scale,
                    box.right * scale, box.top * scale,
                ),
            )
        )
    found.sort(key=lambda info: info.area_mm2, reverse=True)
    return found


def parse_layer_spec(spec) -> tuple:
    """Read a layer selector: "" | "7" | "7/2" | a layer name -> (layer, datatype, name).

    Blank -> (None, None, None) (the historical default). ``7`` -> (7, 0, None).
    ``7/2`` -> (7, 2, None). A non-numeric string is treated as a layer name.
    """
    text = str(spec).strip()
    if not text:
        return None, None, None
    if "/" in text:
        layer_part, datatype_part = text.split("/", 1)
        try:
            return int(layer_part), int(datatype_part), None
        except ValueError:
            return None, None, text
    try:
        return int(text), 0, None
    except ValueError:
        return None, None, text


def layer_matches(info, spec) -> bool:
    """Does a KLayout LayerInfo (or our LayerInfo) match a parsed selector?"""
    layer, datatype, name = spec
    info_name = str(getattr(info, "name", "") or "")
    if name is not None:
        return info_name == name
    if layer is None:
        numeric = info.layer == SOURCE_LAYER and info.datatype == SOURCE_DATATYPE
        return numeric or info_name in {str(SOURCE_LAYER), DEFAULT_LAYER_NAME}
    return (info.layer == layer and info.datatype == datatype) or info_name == str(layer)


def layer_indices_for_spec(layout, spec) -> list:
    """All layer indices in ``layout`` that match a parsed selector."""
    matching = []
    for index in layout.layer_indices():
        if layer_matches(layout.get_info(index), spec):
            matching.append(index)
    return matching


def best_pinfin_row(layers: list[LayerInfo]) -> int:
    """Heuristic index of the pinfin layer: the most polygons (many small shapes)."""
    if not layers:
        return -1
    best = 0
    for i, info in enumerate(layers):
        if info.polygons > layers[best].polygons:
            best = i
    return best


def best_bbox_row(layers: list[LayerInfo]) -> int:
    """Heuristic index of the bbox layer: a modest number of large rectangles.

    Best-effort only (the reliable path is an explicit ``--bbox`` selector). The
    bbox layer is often drawn as a single arrayed rectangle/path at cell level, so
    counts are unreliable; rank instead by a large bounding-box extent that is not
    the full-wafer outline, excluding the pinfin layer.
    """
    if not layers:
        return -1
    pinfin = best_pinfin_row(layers)
    # Full-wafer outlines: bbox spanning most of a ~100 mm wafer with 1-2 shapes.
    best = -1
    best_score = None
    for i, info in enumerate(layers):
        if i == pinfin:
            continue
        if info.bbox_mm is None:
            continue
        l, b, r, t = info.bbox_mm
        span = min(r - l, t - b)
        shapes = info.polygons + info.paths
        # Skip the near-full-wafer reference outlines (>= ~95 mm across, <= 2 shapes).
        if span >= 95.0 and shapes <= 2:
            continue
        score = (span, -shapes)
        if best_score is None or score > best_score:
            best_score = score
            best = i
    if best < 0:  # fall back: any non-pinfin layer, else the first layer
        for i, info in enumerate(layers):
            if i != pinfin:
                return i
        return 0
    return best
