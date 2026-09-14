"""Exact bar-pair intersections in an explicitly assumed coplanar pattern model.

Only additional bars represented by CompositeLayoutZone are checked. Different
directions, background reinforcement, host geometry and actual 3D depths are not
inferred. Straight bars have flat ends: separated/touching end planes do not
constitute a positive-volume body intersection.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from rebar.models import Axis, Direction, Layer

from ..contracts.placement import CompositeLayoutZone, PatternedRebarSet
from .axis_patterns import MAX_PATTERN_BARS, pattern_coordinates
from .geometry import GEOMETRY_TOLERANCE_MM

SAME_PLANE_ASSUMPTION = "all-additional-bars-coplanar-within-each-layer-and-axis/research-v1"


class PatternedConflictLimitError(ValueError):
    """The complete check exceeds its resource budget; no partial pass is returned."""


@dataclass(frozen=True)
class PatternedBarLocator:
    """Zero-based component and bar indexes; bar index follows sorted pattern axes."""

    direction: Direction
    zone_id: str
    component_index: int
    bar_index: int
    transverse_coordinate_mm: float
    longitudinal_interval_mm: tuple[float, float]
    diameter_mm: int
    declared_axis_depth_from_face_mm: float | None


@dataclass(frozen=True)
class PatternedBarConflict:
    first: PatternedBarLocator
    second: PatternedBarLocator
    kind: str
    longitudinal_overlap_interval_mm: tuple[float, float]
    transverse_axis_distance_mm: float
    # Negative = bodies intersect; zero = tangent; positive = separated surfaces.
    transverse_surface_gap_mm: float


@dataclass(frozen=True)
class PatternedZonePair:
    direction: Direction
    first_zone_id: str
    second_zone_id: str


@dataclass(frozen=True)
class PatternedConflictReport:
    bars_checked: int
    components_checked: int
    zones_checked: int
    body_intersection_count: int
    clearance_only_count: int
    body_intersection_zone_pair_count: int
    affected_bar_count: int
    affected_zone_count: int
    body_intersection_pairs: tuple[PatternedBarConflict, ...]
    clearance_only_pairs: tuple[PatternedBarConflict, ...]
    body_intersection_zone_pairs: tuple[PatternedZonePair, ...]
    pair_checks: int
    minimum_clear_spacing_mm: float
    geometry_tolerance_mm: float = GEOMETRY_TOLERANCE_MM
    assumption: str = SAME_PLANE_ASSUMPTION
    actual_3d_checked: bool = False
    background_checked: bool = False
    cross_direction_checked: bool = False
    longitudinal_end_clearance_checked: bool = False
    placement_eligible: bool = False


def _locator_key(bar: PatternedBarLocator) -> tuple:
    return str(bar.direction), bar.zone_id, bar.component_index, bar.bar_index


def _positive_integer(name: str, value: int, maximum: int | None = None) -> None:
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        raise ValueError(f"{name} must be a positive integer" + (f" <= {maximum}" if maximum else ""))


def _decode_bars(
    zones: tuple[CompositeLayoutZone, ...], maximum_bars: int,
) -> tuple[tuple[PatternedBarLocator, ...], int]:
    bars: list[PatternedBarLocator] = []
    zone_keys: set[tuple[Direction, str]] = set()
    known_depths: dict[Direction, set[float]] = {}
    component_count = 0
    for zone in zones:
        if (not isinstance(zone, CompositeLayoutZone) or not isinstance(zone.direction, Direction)
                or not isinstance(zone.direction.layer, Layer) or not isinstance(zone.direction.axis, Axis)
                or not isinstance(zone.id, str) or not zone.id.strip()):
            raise ValueError("expected typed CompositeLayoutZone with a nonempty ID and Direction")
        zone_key = (zone.direction, zone.id)
        if zone_key in zone_keys:
            raise ValueError("duplicate zone ID inside one direction")
        zone_keys.add(zone_key)
        if (not isinstance(zone.components, tuple) or not zone.components
                or len(zone.components) != len(zone.recipe.additions)
                or len(zone.components) != len(zone.placement.additions)):
            raise ValueError("zone must preserve all recipe components")
        for index, component in enumerate(zone.components):
            component_count += 1
            if (not isinstance(component, PatternedRebarSet)
                    or type(component.component_index) is not int or component.component_index != index
                    or component.rebar != zone.recipe.additions[index]
                    or component.placement != zone.placement.additions[index]):
                raise ValueError("component index, reinforcement or placement differs from recipe")
            _positive_integer("component bar_count", component.bar_count)
            _positive_integer("component diameter", component.rebar.diameter)
            remaining = maximum_bars - len(bars)
            if component.bar_count > remaining:
                raise PatternedConflictLimitError("bar budget exceeded; no check result was truncated")
            interval = component.longitudinal_interval_mm
            if (not isinstance(interval, tuple) or len(interval) != 2
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(value) for value in interval)
                    or interval[0] >= interval[1]
                    or isinstance(component.installed_length_mm, bool)
                    or not isinstance(component.installed_length_mm, (int, float))
                    or not math.isfinite(component.installed_length_mm)
                    or not math.isclose(interval[1] - interval[0], component.installed_length_mm,
                                        rel_tol=0, abs_tol=1e-6)):
                raise ValueError("component longitudinal interval must reproduce positive installed length")
            # Pass the bound before expanding actual axes. A lying stored bar_count
            # cannot cause unbounded materialization or hide extra pattern axes.
            try:
                coordinates = pattern_coordinates(component.placement, component.axis_window_mm,
                                                  maximum_bars=remaining)
            except ValueError as error:
                if "превышает лимит" in str(error):
                    raise PatternedConflictLimitError("actual pattern exceeds bar budget; not truncated") from error
                raise
            if len(coordinates) != component.bar_count:
                raise ValueError("component bar_count differs from actual pattern coordinates")
            if any(not math.isfinite(coordinate) for coordinate in coordinates):
                raise ValueError("pattern coordinates must be finite")
            depth = component.placement.axis_depth_from_face_mm
            if depth is not None:
                known_depths.setdefault(zone.direction, set()).add(depth)
            bars.extend(PatternedBarLocator(zone.direction, zone.id, index, bar_index,
                        coordinate, interval, component.rebar.diameter, depth)
                        for bar_index, coordinate in enumerate(coordinates))
    # Do not call bars already declared at different depths coplanar. The caller
    # must explicitly construct a hypothetical same-plane model instead.
    if any(len(depths) > 1 for depths in known_depths.values()):
        raise ValueError("declared unequal depths contradict the same-plane assumption")
    return tuple(bars), component_count


def check_patterned_same_plane_conflicts(
    zones: tuple[CompositeLayoutZone, ...] | list[CompositeLayoutZone],
    *,
    assume_same_depth_per_direction: bool,
    minimum_clear_spacing_mm: float = 0.0,
    maximum_bars: int = 5000,
    maximum_pairs: int = 200_000,
    maximum_pair_checks: int = 2_000_000,
) -> PatternedConflictReport:
    """Enumerate every intersection of actual additional bar bodies under an assumption.

    Body intersection requires positive longitudinal overlap and transverse axis
    distance less than the sum of radii, beyond the explicit 1e-6 mm numerical
    tolerance returned in the report (not an engineering clearance). There is no
    capsule/end extension and no zone-envelope substitution. Different diameters
    at the same axis and components inside the same zone are NOT deduplicated.

    ``minimum_clear_spacing_mm`` optionally reports transverse clearance-only
    violations for longitudinally overlapping bars; it does not change the body
    intersection count. End-to-end clearances are outside this checker.

    An explicit True is required even if all depths are missing. Contradictory
    known depths are rejected, not ignored. Missing Z never becomes a 3D pass.
    Resource exhaustion raises PatternedConflictLimitError, never truncated pass.
    """
    if assume_same_depth_per_direction is not True:
        raise ValueError("explicit assume_same_depth_per_direction=True is required")
    if not isinstance(zones, (tuple, list)):
        raise ValueError("zones must be a bounded tuple/list of CompositeLayoutZone")
    _positive_integer("maximum_bars", maximum_bars, MAX_PATTERN_BARS)
    _positive_integer("maximum_pairs", maximum_pairs)
    _positive_integer("maximum_pair_checks", maximum_pair_checks)
    if len(zones) > maximum_bars:
        raise PatternedConflictLimitError("zone count already exceeds bar budget; not truncated")
    if (isinstance(minimum_clear_spacing_mm, bool)
            or not isinstance(minimum_clear_spacing_mm, (int, float))
            or not math.isfinite(minimum_clear_spacing_mm) or minimum_clear_spacing_mm < 0):
        raise ValueError("minimum_clear_spacing_mm must be finite and nonnegative")
    bars, component_count = _decode_bars(tuple(zones), maximum_bars)
    grouped: dict[Direction, list[PatternedBarLocator]] = {}
    for bar in bars:
        grouped.setdefault(bar.direction, []).append(bar)
    body_pairs, clearance_pairs = [], []
    pair_checks = 0
    for direction in sorted(grouped, key=str):
        ordered = sorted(grouped[direction], key=lambda bar: (bar.transverse_coordinate_mm, _locator_key(bar)))
        maximum_radius = max(bar.diameter_mm for bar in ordered) / 2
        for index, first in enumerate(ordered):
            for other_index in range(index + 1, len(ordered)):
                second = ordered[other_index]
                distance = second.transverse_coordinate_mm - first.transverse_coordinate_mm
                if distance + GEOMETRY_TOLERANCE_MM >= first.diameter_mm / 2 + maximum_radius + minimum_clear_spacing_mm:
                    break
                pair_checks += 1
                if pair_checks > maximum_pair_checks:
                    raise PatternedConflictLimitError("pair-check budget exceeded; result is not complete")
                overlap = (max(first.longitudinal_interval_mm[0], second.longitudinal_interval_mm[0]),
                           min(first.longitudinal_interval_mm[1], second.longitudinal_interval_mm[1]))
                if overlap[1] - overlap[0] <= GEOMETRY_TOLERANCE_MM:
                    continue
                radius_sum = (first.diameter_mm + second.diameter_mm) / 2
                if distance + GEOMETRY_TOLERANCE_MM >= radius_sum + minimum_clear_spacing_mm:
                    continue
                if len(body_pairs) + len(clearance_pairs) >= maximum_pairs:
                    raise PatternedConflictLimitError("intersection-output budget exceeded; not truncated")
                ordered_pair = tuple(sorted((first, second), key=_locator_key))
                is_body = distance + GEOMETRY_TOLERANCE_MM < radius_sum
                item = PatternedBarConflict(*ordered_pair,
                    "body_intersection" if is_body else "transverse_clearance_only",
                    overlap, distance, distance - radius_sum)
                (body_pairs if is_body else clearance_pairs).append(item)
    sort_key = lambda pair: (_locator_key(pair.first), _locator_key(pair.second))  # noqa: E731
    body_pairs.sort(key=sort_key)
    clearance_pairs.sort(key=sort_key)
    zone_pairs = {(pair.first.direction, *sorted((pair.first.zone_id, pair.second.zone_id))) for pair in body_pairs}
    affected_bars = {_locator_key(bar) for pair in body_pairs for bar in (pair.first, pair.second)}
    affected_zones = {(bar.direction, bar.zone_id) for pair in body_pairs for bar in (pair.first, pair.second)}
    return PatternedConflictReport(
        len(bars), component_count, len(zones), len(body_pairs), len(clearance_pairs), len(zone_pairs),
        len(affected_bars), len(affected_zones), tuple(body_pairs), tuple(clearance_pairs),
        tuple(PatternedZonePair(*pair) for pair in sorted(zone_pairs, key=lambda item: (str(item[0]), *item[1:]))),
        pair_checks, float(minimum_clear_spacing_mm),
    )
