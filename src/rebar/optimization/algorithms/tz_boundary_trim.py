"""User-selected physical shortening/splitting at the external slab outline."""
from ..services.shaped_geometry import shaped_cut_length_mm
from ..services.opening_relocation import lane_map
from ..services.tz_boundary_trim import (
    OPENINGS_POLICY, POLICY, build_trimmed_pieces, straight_outer_intersections, trimming_domain, trimming_parent,
)


def trim_straight_bars_to_outer_boundary(before, actual_host, *, lanes=None, nudge_edge_axis=False,
                                        discard_empty_intersections=False, respect_openings=False):
    """Return actual pieces and complete provenance; never an engineering pass."""
    if not isinstance(before, tuple) or not 1 <= len(before) <= 5000:
        raise ValueError("Complete bounded immutable physical inventory required")
    if (type(nudge_edge_axis) is not bool or type(discard_empty_intersections) is not bool
            or nudge_edge_axis and lanes is None):
        raise ValueError("Explicit nudge policy with complete source lanes required")
    sources = lane_map(lanes) if lanes is not None else {}
    trimming_domain(actual_host, respect_openings=respect_openings)
    after, mapping = [], []
    for bar in before:
        shaped_cut_length_mm(bar)
        parent = trimming_parent(bar, actual_host, sources, nudge_edge_axis=nudge_edge_axis,
                                 respect_openings=respect_openings)
        if parent.shape_kind == "straight":
            intervals = straight_outer_intersections(parent, actual_host, respect_openings=respect_openings)
            pieces = (build_trimmed_pieces(parent, intervals) if intervals or discard_empty_intersections
                      else (parent,))
        else:
            pieces = (bar,)
        after.extend(pieces)
        mapping.append({"direction": str(bar.direction), "source_bar_id": bar.id,
                        "piece_ids": [b.id for b in pieces],
                        "policy": OPENINGS_POLICY if respect_openings else POLICY})
        if len(after) > 10000:
            raise ValueError("Physical piece budget exceeded; no partial output")
    if len({(b.direction, b.id) for b in after}) != len(after):
        raise ValueError("Duplicate physical piece identity; input IDs need disambiguation")
    return tuple(after), tuple(mapping)
