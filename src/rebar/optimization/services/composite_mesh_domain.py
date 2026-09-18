"""Geometry occupied by source KLEENKA cells, independent of reinforcement level."""

from shapely.geometry import Polygon, box, mapping
from shapely.ops import unary_union


def composite_mesh_domain(demand):
    """Return the exact union of source cells; gaps are forbidden, including holes."""
    if not demand.cells:
        raise ValueError("для проверки белых областей нужны исходные КЭ")
    return unary_union([Polygon(cell.poly) for cell in demand.cells])


def zone_inside_mesh(domain, bbox):
    """A rectangular demand zone must lie entirely on source FE geometry."""
    return domain.covers(box(*bbox))


def clipped_zone_footprint(domain, bbox):
    """The effective zone is its search rectangle intersected with source FE cells."""
    rectangle = box(*bbox)
    shape = rectangle.intersection(domain)
    if shape.is_empty or shape.area <= 0:
        raise ValueError("зона не пересекает исходную сетку КЭ")
    return {"search_bbox_mm": list(bbox), "geometry_mm": mapping(shape),
            "area_mm2": shape.area, "clipped_white_area_mm2": max(0.0, rectangle.area - shape.area)}
