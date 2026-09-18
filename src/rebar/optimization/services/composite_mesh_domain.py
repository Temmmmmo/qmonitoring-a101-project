"""Geometry occupied by source KLEENKA cells, independent of reinforcement level."""

from shapely.geometry import Polygon, box
from shapely.ops import unary_union


def composite_mesh_domain(demand):
    """Return the exact union of source cells; gaps are forbidden, including holes."""
    if not demand.cells:
        raise ValueError("для проверки белых областей нужны исходные КЭ")
    return unary_union([Polygon(cell.poly) for cell in demand.cells])


def zone_inside_mesh(domain, bbox):
    """A rectangular demand zone must lie entirely on source FE geometry."""
    return domain.covers(box(*bbox))
