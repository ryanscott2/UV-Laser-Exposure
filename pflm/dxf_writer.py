"""Write a centered array Region as the DXF the laser tools expect (§5.4).

Adapted verbatim in intent from Singulation's
``split_klayout.py::write_dxf_r2010``: AutoCAD 2010 (R2010), millimeter units
($INSUNITS = 4), closed LWPOLYLINEs on layer '0', emitted through ezdxf because
KLayout's own DXF writer cannot set the version or the units header.
"""

from __future__ import annotations

from pathlib import Path

OUTPUT_LAYER_NAME = "0"


def write_dxf_r2010(path, region, dbu: float) -> None:
    """Write ``region`` (in database units) to ``path`` as R2010 / mm DXF.

    Coordinates are emitted in millimeters (1 DXF unit = 1 mm). Each polygon hull
    and every hole becomes its own closed LWPOLYLINE on layer '0'.
    """
    import ezdxf  # lazy: only DXF output needs it

    path = Path(path)
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4  # 4 = millimeters
    msp = doc.modelspace()
    scale = dbu / 1000.0  # database units -> mm
    for poly in region.each():
        rings = [poly.each_point_hull()]
        for hole in range(poly.holes()):
            rings.append(poly.each_point_hole(hole))
        for ring in rings:
            points = [(p.x * scale, p.y * scale) for p in ring]
            if points:
                msp.add_lwpolyline(
                    points, close=True, dxfattribs={"layer": OUTPUT_LAYER_NAME}
                )
    doc.saveas(str(path))


def write_rects_r2010(path, rects_um) -> None:
    """Write axis-aligned rectangles as closed LWPOLYLINEs (R2010 / mm, layer '0').

    ``rects_um`` is a list of ``(l, b, r, t)`` in microns (already centered). Each
    rectangle is its own simple closed polyline -- used for the dead-space ablation
    regions, which are decomposed into hole-free rectangles so the fill never covers
    the pin-field box."""
    import ezdxf  # lazy

    path = Path(path)
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4  # mm
    msp = doc.modelspace()
    for (l, b, r, t) in rects_um:
        pts = [(l / 1000.0, b / 1000.0), (r / 1000.0, b / 1000.0),
               (r / 1000.0, t / 1000.0), (l / 1000.0, t / 1000.0)]
        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": OUTPUT_LAYER_NAME})
    doc.saveas(str(path))


def write_circles_r2010(path, circles_um, dbu: float = None) -> None:
    """Write round pins as true DXF CIRCLE entities (R2010 / mm, layer '0').

    ``circles_um`` is a list of ``(cx_um, cy_um, r_um)`` (already centered). One
    CIRCLE per pin — far smaller and exact vs a many-gon polygon, and what
    WinLase imports natively (the source pin DXFs were CIRCLE entities). ``dbu``
    is unused (coordinates are already in microns) and kept for call symmetry.
    """
    import ezdxf  # lazy

    path = Path(path)
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4  # mm
    msp = doc.modelspace()
    for cx, cy, r in circles_um:
        msp.add_circle((cx / 1000.0, cy / 1000.0), r / 1000.0,
                       dxfattribs={"layer": OUTPUT_LAYER_NAME})
    doc.saveas(str(path))
