"""Explicit research-only placement of a source envelope at a plate edge."""
from dataclasses import dataclass
import math

from shapely.affinity import translate
from shapely.geometry import LineString, Polygon, box

from rebar.models import Axis


TOLERANCE_MM = .01


@dataclass(frozen=True)
class EdgeEnvelopePlacement:
    status: str
    bounds_mm: tuple[float, float, float, float] | None
    shift_mm: float | None
    reason: str | None


def place_edge_envelope(exterior, bounds_mm, axis, core_interval_mm, installed_length_mm, total_extension_mm):
    """Place unchanged-L/width envelope; total extension is not per-end anchorage."""
    if (not isinstance(exterior, Polygon) or exterior.is_empty or not exterior.is_valid
            or exterior.interiors):
        return EdgeEnvelopePlacement("not_checked", None, None, "unsupported_exterior")
    if axis not in (Axis.X, Axis.Y) or len(bounds_mm) != 4 or len(core_interval_mm) != 2:
        raise ValueError("finite axis-aligned source envelope required")
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) for v in (*bounds_mm, *core_interval_mm, installed_length_mm, total_extension_mm)):
        raise ValueError("finite edge placement geometry required")
    lo, hi = (bounds_mm[0], bounds_mm[2]) if axis is Axis.X else (bounds_mm[1], bounds_mm[3])
    core_lo, core_hi = core_interval_mm
    if bounds_mm[0] > bounds_mm[2] or bounds_mm[1] > bounds_mm[3] or lo > hi or core_lo > core_hi or installed_length_mm <= 0 or total_extension_mm < 0 or abs((hi-lo)-installed_length_mm) > 1e-6:
        raise ValueError("inconsistent source envelope length")
    if installed_length_mm + 1e-6 < core_hi-core_lo+total_extension_mm:
        return EdgeEnvelopePlacement("fail", None, None, "total_extension_insufficient")
    lower, upper = core_hi-hi, core_lo-lo
    if lower > upper + 1e-6:
        return EdgeEnvelopePlacement("fail", None, None, "core_not_contained")
    region = exterior.buffer(TOLERANCE_MM, join_style=2)
    across = (bounds_mm[1], bounds_mm[3]) if axis is Axis.X else (bounds_mm[0], bounds_mm[2])
    events = {across[0], across[1]}
    events.update(point[1 if axis is Axis.X else 0] for point in region.exterior.coords if across[0] <= point[1 if axis is Axis.X else 0] <= across[1])
    ordered = sorted(events)
    events.update((left+right)/2 for left, right in zip(ordered, ordered[1:]))
    core_box = (box(core_lo, across[0], core_hi, across[1]) if axis is Axis.X
                else box(across[0], core_lo, across[1], core_hi))
    if not region.covers(core_box):
        return EdgeEnvelopePlacement("fail", None, None, "logical_core_envelope_outside")
    feasible = [(lower, upper)]
    minimum, maximum = (region.bounds[0], region.bounds[2]) if axis is Axis.X else (region.bounds[1], region.bounds[3])
    for value in events:
        line = LineString([(minimum-1, value), (maximum+1, value)] if axis is Axis.X else [(value, minimum-1), (value, maximum+1)])
        cross = region.intersection(line)
        segments = [cross] if cross.geom_type == "LineString" else list(getattr(cross, "geoms", ()))
        allowed = []
        for segment in segments:
            coords = list(segment.coords)
            values = sorted(point[0 if axis is Axis.X else 1] for point in coords)
            if len(values) >= 2 and values[-1]-values[0] >= installed_length_mm-1e-6:
                allowed.append((values[0]-lo, values[-1]-installed_length_mm-lo))
        feasible = [(max(a, c), min(b, d)) for a, b in feasible for c, d in allowed if max(a, c) <= min(b, d)+1e-6]
        if not feasible:
            return EdgeEnvelopePlacement("fail", None, None, "no_common_longitudinal_corridor")
    original = box(*bounds_mm)
    starts = {min(max(0., a), b) for a, b in feasible}
    starts.update(value for pair in feasible for value in pair)
    for shift in sorted(starts, key=lambda value: (abs(value), value)):
        candidate = translate(original, xoff=shift if axis is Axis.X else 0, yoff=shift if axis is Axis.Y else 0)
        new_lo, new_hi = lo + shift, hi + shift
        core_contained = new_lo <= core_lo + 1e-6 and core_hi <= new_hi + 1e-6
        total_valid = new_hi - new_lo + 1e-6 >= core_hi - core_lo + total_extension_mm
        if region.covers(candidate) and core_contained and total_valid:
            return EdgeEnvelopePlacement("pass", tuple(candidate.bounds), shift, None)
    return EdgeEnvelopePlacement("fail", None, None, "corridor_final_cover_failed")
