"""Clip pinfin geometry to one array bbox, rotate, and center on the origin (§5.3).

Adapted from Singulation's ``split_klayout.py`` centering/clip math. The "field
center" that Singulation subtracted per window is here each array's own bbox
center, and a ``design_rotation_deg`` (§2.1) is applied so the centered DXF
carries the rotated features that the rotated ``exposed_center_um`` stage target
assumes.

Keep the source Layout alive while the returned Region is used (KLayout iterator
lifetime, §8). No hardware imports.
"""

from __future__ import annotations

try:
    import pya  # type: ignore
except ImportError:
    import klayout.db as pya

from .layers import layer_indices_for_spec, parse_layer_spec

USABLE_FIELD_HALF_UM = 30_000.0


def _um_to_dbu(layout, value_um: float) -> int:
    return int(round(value_um / layout.dbu))


def _dbu_to_um(layout, value_dbu: int) -> float:
    return value_dbu * layout.dbu


def pinfin_region(layout, pinfin_spec) -> "pya.Region":
    """Union of every pinfin-layer shape, recursive from the top cells."""
    spec = parse_layer_spec(pinfin_spec)
    indices = layer_indices_for_spec(layout, spec)
    region = pya.Region()
    for top in layout.top_cells():
        for index in indices:
            region += pya.Region(top.begin_shapes_rec(index))
    return region


def clip_and_center(layout, pinfin_spec, bbox_um, *, design_rotation_deg=0,
                    global_offset_um=(0.0, 0.0), center_override=None) -> "pya.Region":
    """Pinfin shapes clipped to ``bbox_um``, rotated by ``design_rotation_deg``
    about the center, then translated so the center is at (0, 0) (plus a
    calibration ``global_offset_um``).

    ``center_override`` (exposed-frame um): translate about this point instead of
    the bbox center. Used for alignment marks that can't be field-centered — the
    stage centers on ``center_override`` (a reachable point) and the mark lands
    off-center in the field by (mark_center - center_override), i.e. at its true
    wafer location. For pinfin arrays it is None (translate about the bbox center).

    Net map for a point p: ``p -> R(p - center) + global_offset``.
    """
    left, bottom, right, top = bbox_um
    if center_override is not None:
        cx, cy = center_override
    else:
        cx = (left + right) / 2.0
        cy = (bottom + top) / 2.0

    region = pinfin_region(layout, pinfin_spec)
    clip_box = pya.Box(
        _um_to_dbu(layout, left), _um_to_dbu(layout, bottom),
        _um_to_dbu(layout, right), _um_to_dbu(layout, top),
    )
    region = region & pya.Region(clip_box)

    gx, gy = global_offset_um
    rot_k = int(round(design_rotation_deg / 90.0)) % 4
    # Applied right-to-left: first move the array center to the origin, then rotate
    # about the origin, then apply the calibration offset.
    to_origin = pya.Trans(pya.Trans.R0, _um_to_dbu(layout, -cx), _um_to_dbu(layout, -cy))
    rotate = pya.Trans(rot_k)  # k*90 CCW about the origin
    offset = pya.Trans(pya.Trans.R0, _um_to_dbu(layout, gx), _um_to_dbu(layout, gy))
    region.transform(offset * rotate * to_origin)
    return region


def array_circles(layout, pinfin_spec, bbox_um, *, design_rotation_deg=0,
                  global_offset_um=(0.0, 0.0)):
    """Round pins in one array's bbox as centered circles (efficient, no big Region).

    Iterates ONLY the pins touching ``bbox_um`` via ``begin_shapes_rec_touching``
    (O(pins in this array), not O(all pins on the layer)), treats each pin as a
    circle = (bbox center, bbox half-width), applies the same transform as
    ``clip_and_center`` (move array center to origin, rotate k*90, add
    calibration ``global_offset_um``), and returns ``(circles_um, bbox_um)`` where
    circles_um is a list of ``(cx, cy, r)`` in microns. Fast + exact vs polygonizing.
    """
    spec = parse_layer_spec(pinfin_spec)
    indices = layer_indices_for_spec(layout, spec)
    left, bottom, right, top = bbox_um
    cx = (left + right) / 2.0
    cy = (bottom + top) / 2.0
    gx, gy = global_offset_um
    k = int(round(design_rotation_deg / 90.0)) % 4
    clip = pya.Box(_um_to_dbu(layout, left), _um_to_dbu(layout, bottom),
                   _um_to_dbu(layout, right), _um_to_dbu(layout, top))
    circles = []
    minx = miny = 1e18
    maxx = maxy = -1e18
    for topcell in layout.top_cells():
        for index in indices:
            it = topcell.begin_shapes_rec_touching(index, clip)
            while not it.at_end():
                pb = it.shape().bbox().transformed(it.trans())
                pcx = _dbu_to_um(layout, (pb.left + pb.right) / 2.0)
                pcy = _dbu_to_um(layout, (pb.bottom + pb.top) / 2.0)
                if not (left <= pcx <= right and bottom <= pcy <= top):
                    it.next(); continue        # only pins whose center is inside this box
                r = _dbu_to_um(layout, pb.width() / 2.0)
                x = pcx - cx
                y = pcy - cy
                if k == 1:   x, y = -y, x
                elif k == 2: x, y = -x, -y
                elif k == 3: x, y = y, -x
                x += gx; y += gy
                circles.append((x, y, r))
                minx = min(minx, x - r); maxx = max(maxx, x + r)
                miny = min(miny, y - r); maxy = max(maxy, y + r)
                it.next()
    bbox = (minx, miny, maxx, maxy) if circles else None
    return circles, bbox


def region_bbox_um(layout, region):
    """(l, b, r, t) of a region in microns, or None if empty."""
    if region.is_empty():
        return None
    box = region.bbox()
    return tuple(_dbu_to_um(layout, v) for v in (box.left, box.bottom, box.right, box.top))


def fits_field(region, usable_half_um=USABLE_FIELD_HALF_UM, *, dbu=None) -> bool:
    """True if a (centered) region stays within +/- ``usable_half_um`` of origin.

    ``region.bbox()`` is in database units; pass the layout ``dbu`` to convert to
    microns (default assumes the region is already in microns). An empty region
    trivially fits.
    """
    if region.is_empty():
        return True
    scale = 1.0 if dbu is None else dbu
    box = region.bbox()
    half = usable_half_um
    return (abs(box.left * scale) <= half and abs(box.right * scale) <= half
            and abs(box.bottom * scale) <= half and abs(box.top * scale) <= half)
