"""Exact longitudinal host fitting of one complete physical straight bar.

The input bars, source intervals and host must already share a coordinate frame.
No binding, depth, clipping, transverse phase, length or missing source is inferred.
The interval construction is geometric, not endpoint/grid sampling: project every
positive-area component of ``transverse_strip - material`` onto the bar axis, then
erode the remaining closed material intervals by the full length and side cover.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math

from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box

from rebar.models import Axis, Direction, Layer

from ..contracts.physical import PhysicalBar, PhysicalSourceBar
from .geometry import GEOMETRY_TOLERANCE_MM
from .solid_host import (
    AREA_TOLERANCE_MM2, OrthogonalSolidHost, SolidHostSection, outside_box_volume_mm3,
)

TOL = GEOMETRY_TOLERANCE_MM
VOLUME_TOLERANCE_MM3 = 0.1


class PhysicalHostFitLimitError(ValueError):
    """A complete exact fit would exceed explicit bounds; nothing is truncated."""


@dataclass(frozen=True)
class PhysicalHostFitResult:
    original_bar: PhysicalBar
    bar: PhysicalBar
    status: str
    mode: str
    axis_z_mm: float | None
    checked_section_indexes: tuple[int, ...]
    source_start_window_mm: tuple[float, float]
    host_start_intervals_mm: tuple[tuple[float, float], ...]
    admissible_start_intervals_mm: tuple[tuple[float, float], ...]
    shift_mm: float
    blocked_reason: str | None
    containment_before: bool
    containment_after: bool
    outside_xy_area_before_mm2: float
    outside_xy_area_after_mm2: float
    outside_solid_volume_before_mm3: float | None
    outside_solid_volume_after_mm3: float | None
    source_reference_count: int
    source_background_touch_reference_count: int
    full_new_diameter_40d_preserved: bool = True
    dimensions_mass_and_source_axes_unchanged: bool = True
    global_collisions_checked: bool = False
    placement_eligible: bool = False
    actual_3d_placement_approved: bool = False
    area_tolerance_mm2: float = AREA_TOLERANCE_MM2
    volume_tolerance_mm3: float = VOLUME_TOLERANCE_MM3


def _finite(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and abs(value) <= 1e9)


def _interval(value):
    return (isinstance(value, tuple) and len(value) == 2 and all(_finite(v) for v in value)
            and value[0] < value[1])


def _polygons(geometry):
    if isinstance(geometry, Polygon):
        if not geometry.is_empty:
            yield geometry
    elif isinstance(geometry, (MultiPolygon, GeometryCollection)):
        for part in geometry.geoms:
            yield from _polygons(part)


def _validate_host(host, maximum_vertices):
    if (not isinstance(host, OrthogonalSolidHost) or not isinstance(host.sections, tuple)
            or not 1 <= len(host.sections) <= 127
            or any(not _finite(value) or value < 0 for value in
                   (host.top_cover_mm, host.bottom_cover_mm, host.side_cover_mm))):
        raise ValueError("typed complete orthogonal host with nonnegative covers is required")
    if (isinstance(host.volume_mm3, bool) or not isinstance(host.volume_mm3, (int, float))
            or not math.isfinite(host.volume_mm3) or not 0 < host.volume_mm3 <= 1e24
            or type(host.face_count) is not int or not 6 <= host.face_count <= 4096):
        raise ValueError("host volume and face count must describe a complete bounded solid")
    vertices = 0
    previous = None
    for section in host.sections:
        if (not isinstance(section, SolidHostSection)
                or not _interval((section.bottom_z_mm, section.top_z_mm))
                or previous is not None and section.bottom_z_mm != previous
                or not isinstance(section.footprint, (Polygon, MultiPolygon))
                or section.footprint.is_empty or not section.footprint.is_valid):
            raise ValueError("host sections must be valid, contiguous and ordered; no bbox fallback")
        previous = section.top_z_mm
        for polygon in _polygons(section.footprint):
            for ring in (polygon.exterior, *polygon.interiors):
                points = list(ring.coords)
                vertices += len(points) - 1
                if vertices > maximum_vertices:
                    raise PhysicalHostFitLimitError("host vertex budget exceeded; no geometry was truncated")
                for first, second in zip(points, points[1:]):
                    if (len(first) != 2 or not all(_finite(v) for v in (*first, *second))
                            or sum(a != b for a, b in zip(first, second)) != 1):
                        raise ValueError("host footprint edges must be nondegenerate and axis aligned")
    volume = math.fsum(section.footprint.area * (section.top_z_mm - section.bottom_z_mm)
                       for section in host.sections)
    if not math.isclose(host.volume_mm3, volume, rel_tol=1e-8, abs_tol=1):
        raise ValueError("host volume differs from the complete section geometry")
    if host.top_cover_mm + host.bottom_cover_mm >= host.sections[-1].top_z_mm - host.sections[0].bottom_z_mm:
        raise ValueError("host top and bottom covers leave no usable height")


def _source_window(bar, source_bars):
    if (not isinstance(bar, PhysicalBar) or not isinstance(bar.direction, Direction)
            or not isinstance(bar.direction.axis, Axis) or not isinstance(bar.direction.layer, Layer)
            or not isinstance(bar.id, str) or not bar.id.strip()
            or not isinstance(bar.steel_class, str) or not bar.steel_class.strip()
            or type(bar.diameter_mm) is not int or bar.diameter_mm <= 0
            or not _finite(bar.transverse_axis_mm) or not _interval(bar.installed_interval_mm)
            or not isinstance(bar.source_bar_ids, tuple) or not bar.source_bar_ids
            or any(not isinstance(i, str) or not i for i in bar.source_bar_ids)
            or len(set(bar.source_bar_ids)) != len(bar.source_bar_ids)):
        raise ValueError("invalid typed physical bar or source-reference ownership")
    if not isinstance(source_bars, tuple) or not 1 <= len(source_bars) <= 5000:
        raise ValueError("a complete bounded tuple of source records is required")
    source = {}
    for item in source_bars:
        if (not isinstance(item, PhysicalSourceBar) or not isinstance(item.direction, Direction)
                or not isinstance(item.direction.axis, Axis) or not isinstance(item.direction.layer, Layer)
                or not isinstance(item.id, str) or not item.id.strip()):
            raise ValueError("source records must be typed PhysicalSourceBar")
        key = (item.direction, item.id)
        if key in source:
            raise ValueError("duplicate source reference within a direction")
        source[key] = item
    required, touches = [], 0
    for identifier in bar.source_bar_ids:
        key = (bar.direction, identifier)
        if key not in source:
            raise ValueError("missing original source bar; no source is discarded")
        parent = source[key]
        if (type(parent.diameter_mm) is not int or not 0 < parent.diameter_mm <= bar.diameter_mm
                or parent.steel_class != bar.steel_class or not _finite(parent.transverse_axis_mm)
                or parent.transverse_axis_mm != bar.transverse_axis_mm
                or not _interval(parent.required_interval_mm) or not _interval(parent.installed_interval_mm)
                or type(parent.background_diameter_mm) is not int or parent.background_diameter_mm <= 0
                or not _finite(parent.background_origin_mm) or not _finite(parent.background_step_mm)
                or parent.background_step_mm <= 0):
            raise ValueError("original material, diameter, axis, intervals or background are invalid")
        if (parent.installed_interval_mm[0] > parent.required_interval_mm[0] - 40 * parent.diameter_mm + TOL
                or parent.installed_interval_mm[1] < parent.required_interval_mm[1] + 40 * parent.diameter_mm - TOL):
            raise ValueError("original source bar lacks full original-diameter 40d")
        nearest = parent.background_origin_mm + round((parent.transverse_axis_mm - parent.background_origin_mm)
                  / parent.background_step_mm) * parent.background_step_mm
        old_gap = abs(parent.transverse_axis_mm - nearest) - (parent.diameter_mm + parent.background_diameter_mm) / 2
        new_gap = abs(bar.transverse_axis_mm - nearest) - (bar.diameter_mm + parent.background_diameter_mm) / 2
        if old_gap < -TOL or new_gap < -TOL or abs(old_gap) <= TOL < abs(new_gap):
            raise ValueError("source background penetration/contact cannot be repaired by longitudinal host fitting")
        touches += abs(old_gap) <= TOL
        required.append(parent.required_interval_mm)
    lower = max(pair[1] + 40 * bar.diameter_mm for pair in required) - bar.installed_length_mm
    upper = min(pair[0] - 40 * bar.diameter_mm for pair in required)
    if (lower > upper + TOL or bar.installed_interval_mm[0] < lower - TOL
            or bar.installed_interval_mm[0] > upper + TOL):
        raise ValueError("input physical bar does not preserve every source interval plus full NEW-diameter 40d")
    return ((lower, upper) if lower <= upper else ((lower + upper) / 2,) * 2), touches


def _selected_material(host, axis_z, conservative):
    if conservative:
        selected = tuple(range(len(host.sections)))
        vertical = None
    else:
        # Radius is applied by the public function before calling this helper.
        vertical = axis_z
        selected = tuple(i for i, section in enumerate(host.sections)
                         if min(vertical[1], section.top_z_mm) > max(vertical[0], section.bottom_z_mm))
    if not selected:
        return GeometryCollection(), selected
    material = host.sections[selected[0]].footprint
    for index in selected[1:]:
        material = material.intersection(host.sections[index].footprint)
    return material, selected


def _host_intervals(bar, material, cover, maximum_intervals):
    if material.is_empty or material.area == 0:
        return ()
    axis = 0 if bar.direction.axis is Axis.X else 1
    lower, upper = material.bounds[axis], material.bounds[axis + 2]
    radius = bar.diameter_mm / 2 + cover
    transverse = (bar.transverse_axis_mm - radius, bar.transverse_axis_mm + radius)
    strip = (box(lower, transverse[0], upper, transverse[1]) if axis == 0
             else box(transverse[0], lower, transverse[1], upper))
    excluded = strip.difference(material)
    projections = []
    for polygon in _polygons(excluded):
        # Ignore ONLY zero-area components, never small positive holes/recesses.
        if polygon.area > 0:
            projections.append((polygon.bounds[axis], polygon.bounds[axis + 2]))
            if len(projections) > maximum_intervals:
                raise PhysicalHostFitLimitError("exclusion interval budget exceeded; no holes were truncated")
    projections.sort()
    forbidden = []
    for a, b in projections:
        if forbidden and a <= forbidden[-1][1]:
            forbidden[-1] = (forbidden[-1][0], max(b, forbidden[-1][1]))
        else:
            forbidden.append((a, b))
    material_runs = []
    cursor = lower
    for a, b in forbidden:
        if a >= cursor:
            material_runs.append((cursor, a))
        cursor = max(cursor, b)
    if cursor <= upper:
        material_runs.append((cursor, upper))
    result = []
    for a, b in material_runs:
        left, right = a + cover, b - bar.installed_length_mm - cover
        if left <= right:
            result.append((left, right))
    return tuple(result)


def _envelope(bar, side_cover):
    along = (bar.installed_interval_mm[0] - side_cover, bar.installed_interval_mm[1] + side_cover)
    across = (bar.transverse_axis_mm - bar.diameter_mm / 2 - side_cover,
              bar.transverse_axis_mm + bar.diameter_mm / 2 + side_cover)
    return ((along[0], across[0], along[1], across[1]) if bar.direction.axis is Axis.X
            else (across[0], along[0], across[1], along[1]))


def fit_physical_bar_to_solid_host(
    bar: PhysicalBar,
    source_bars: tuple[PhysicalSourceBar, ...],
    host: OrthogonalSolidHost,
    *,
    axis_z_mm: float | None = None,
    conservative_whole_height: bool = False,
    maximum_vertices: int = 20000,
    maximum_intervals: int = 4096,
) -> PhysicalHostFitResult:
    """Nearest exact feasible whole-bar start, or unchanged input plus diagnostics.

    Exactly one explicit mode is required: a real/assumed axis Z (envelope includes
    radius and top/bottom cover), or the conservative XY intersection of EVERY
    height section. The latter is not a chosen Z or a 3D approval. The rectangular
    envelope matches the existing conservative native host check, including side
    cover at flat longitudinal ends and radius+side cover transversely.

    Admissible intervals are exact polygon projections; numerical containment
    rechecks use the documented existing 0.001 mm² / 0.1 mm³ host tolerances.
    New global bar collisions must be independently checked by the caller.
    """
    if type(conservative_whole_height) is not bool or (axis_z_mm is not None) == conservative_whole_height:
        raise ValueError("choose exactly one explicit axis_z_mm or conservative_whole_height=True mode")
    if axis_z_mm is not None and not _finite(axis_z_mm):
        raise ValueError("axis_z_mm must be an explicit finite coordinate")
    for name, value in (("maximum_vertices", maximum_vertices), ("maximum_intervals", maximum_intervals)):
        if type(value) is not int or not 1 <= value <= 100000:
            raise ValueError(f"{name} must be an integer in 1..100000")
    _validate_host(host, maximum_vertices)
    source_window, touches = _source_window(bar, source_bars)
    mode = "conservative_whole_height_intersection" if conservative_whole_height else "explicit_axis_z_envelope"
    vertical = (axis_z_mm - bar.diameter_mm / 2 - host.bottom_cover_mm,
                axis_z_mm + bar.diameter_mm / 2 + host.top_cover_mm) if axis_z_mm is not None else None
    outside_height = vertical is not None and (vertical[0] < host.sections[0].bottom_z_mm
                                               or vertical[1] > host.sections[-1].top_z_mm)
    material, sections = _selected_material(host, vertical, conservative_whole_height)
    host_intervals = () if outside_height else _host_intervals(bar, material, host.side_cover_mm, maximum_intervals)
    admissible = tuple((max(a, source_window[0]), min(b, source_window[1])) for a, b in host_intervals
                       if max(a, source_window[0]) <= min(b, source_window[1]))

    def contained(value):
        xy = _envelope(value, host.side_cover_mm)
        outside_area = box(*xy).difference(material).area
        volume = (outside_box_volume_mm3(host, (xy[0], xy[1], vertical[0]), (xy[2], xy[3], vertical[1]))
                  if vertical is not None else None)
        passed = outside_area <= AREA_TOLERANCE_MM2 and not outside_height and (volume is None or volume <= VOLUME_TOLERANCE_MM3)
        return passed, outside_area, volume

    before, area_before, volume_before = contained(bar)
    reason, fitted = None, bar
    if admissible:
        original_start = bar.installed_interval_mm[0]
        start = min((min(b, max(a, original_start)) for a, b in admissible), key=lambda v: (abs(v - original_start), v))
        fitted = replace(bar, installed_interval_mm=(start, start + bar.installed_length_mm))
    else:
        reason = ("vertical-envelope-outside-host" if outside_height else
                  "no-material-in-selected-sections" if material.is_empty or material.area == 0 else
                  "no-host-position-preserving-length-and-full-new40d")
    after, area_after, volume_after = contained(fitted)
    if admissible and not after:
        raise ValueError("independent material containment rejected the computed start interval")
    _source_window(fitted, source_bars)
    if (abs(fitted.installed_length_mm - bar.installed_length_mm) > TOL
            or replace(fitted, installed_interval_mm=bar.installed_interval_mm) != bar):
        raise ValueError("host fitting changed length, diameter, phase or source ownership")
    shift = fitted.installed_interval_mm[0] - bar.installed_interval_mm[0]
    return PhysicalHostFitResult(bar, fitted, "blocked" if reason else "fitted" if shift != 0 else "unchanged",
        mode, axis_z_mm, sections, source_window, host_intervals, admissible, shift, reason,
        before, after, area_before, area_after, volume_before, volume_after, len(bar.source_bar_ids), touches)
