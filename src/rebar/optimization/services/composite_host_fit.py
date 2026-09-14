"""Fit stock-length surplus in a host without clipping demand, bars or anchorage."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from shapely.geometry import Polygon, box

from rebar.models import Axis, Direction, Layer

from ..contracts.composite_coverage import STO_279_COVERAGE_POLICY
from ..contracts.physical import PhysicalBar
from ..contracts.placement import AxisPlacement, CompositeLayoutZone, PatternedRebarSet, PeriodicAxisPattern, RecipePlacement
from ..contracts.problem import DemandCell, DemandMap, LayoutConstraints

from .axis_patterns import pattern_coordinates
from .bar_geometry import longitudinal_interval
from .composite_coverage import _check_patterns
from .composite_detailing import evaluate_composite_zone
from .physical_host_fit import _envelope, _host_intervals, _validate_host
from .solid_host import AREA_TOLERANCE_MM2, OrthogonalSolidHost


def admissible_host_window(demand, level_index, host, constraints):
    """Conservative demand/window bounds; all periodic axes remain in the host."""
    diameter = max(spec.diameter for spec in demand.level(level_index).recipe.additions)
    along = host.side_cover_mm + diameter * constraints.anchorage_diameters
    across = host.side_cover_mm + diameter / 2
    dx, dy = (along, across) if demand.direction.axis is Axis.X else (across, along)
    x, y, right, top = host.outer_mm
    box = x + dx, y + dy, right - dx, top - dy
    if box[0] >= box[2] or box[1] >= box[3]:
        raise ValueError("Плита слишком мала для полного окна и анкеровки")
    return box


def fit_composite_zone_to_host(demand, zone, host, *, constraints):
    """Nearest feasible whole-set translation along bars; phases/counts unchanged.

    Each component may have a different stock length. Every physical axis must
    avoid every opening with cover. No bending, trimming or shifting of demand.
    """
    if not evaluate_composite_zone(demand, zone, constraints=constraints).geometry_valid:
        raise ValueError("Нельзя подгонять невалидную исходную зону")
    axis = demand.direction.axis
    along, across = ((0, 2), (1, 3)) if axis is Axis.X else ((1, 3), (0, 2))
    start, end = longitudinal_interval(axis, zone.demand_bbox)
    components = []
    for component in zone.components:
        length = component.installed_length_mm
        extension = component.rebar.diameter * constraints.anchorage_diameters
        lo = max(host.outer_mm[along[0]] + host.side_cover_mm, end + extension - length)
        hi = min(host.outer_mm[along[1]] - host.side_cover_mm - length, start - extension)
        if lo > hi + 1e-6:
            raise ValueError("Полная длина стержня и 40d с обоих концов не помещаются в host")
        lo = min(lo, hi)  # Equal endpoints can differ at the last binary float bit.
        intervals = [(lo, hi)]
        radius = component.rebar.diameter / 2
        coordinates = pattern_coordinates(component.placement, component.axis_window_mm)
        for hole in host.openings_mm:
            lower, upper = hole[across[0]] - host.side_cover_mm, hole[across[1]] + host.side_cover_mm
            if not any(c + radius > lower and c - radius < upper for c in coordinates):
                continue
            forbidden_lo = hole[along[0]] - host.side_cover_mm - length
            forbidden_hi = hole[along[1]] + host.side_cover_mm
            next_intervals = []
            for a, b in intervals:
                if a <= min(b, forbidden_lo):
                    next_intervals.append((a, min(b, forbidden_lo)))
                if max(a, forbidden_hi) <= b:
                    next_intervals.append((max(a, forbidden_hi), b))
            intervals = next_intervals
        if not intervals:
            raise ValueError("Проём не позволяет сохранить длину и анкеровку всех стержней набора")
        original = component.longitudinal_interval_mm[0]
        chosen = min((min(max(original, a), b) for a, b in intervals), key=lambda v: (abs(v - original), v))
        components.append(replace(component, longitudinal_interval_mm=(chosen, chosen + length)))
    fitted = replace(zone, components=tuple(components))
    if not evaluate_composite_zone(demand, fitted, constraints=constraints).geometry_valid:
        raise ValueError("Независимая проверка отвергла сохранность длины/анкеровки после сдвига")
    return fitted


SOLID_FIT_MAX_BARS = 10000
SOLID_FIT_MAX_INTERVALS = 4096
SOLID_FIT_TOL_MM = 1e-6


class CompositeSolidHostFitLimitError(ValueError):
    """Resource limit, not an infeasibility certificate and never silent truncation."""


@dataclass(frozen=True)
class CompositeSolidHostFitResult:
    """One common along-axis translation; a blocked result retains the original."""

    original_zone: CompositeLayoutZone
    fitted_zone: CompositeLayoutZone
    status: str
    shift_mm: float
    source_shift_window_mm: tuple[float, float]
    host_shift_windows_mm: tuple[tuple[float, float], ...]
    admissible_shift_windows_mm: tuple[tuple[float, float], ...]
    blocked_reason: str | None
    physical_bar_count: int
    containment_before: bool
    containment_after: bool
    host_blocked_bar_count_before: int
    host_blocked_bar_count_after: int
    outside_host_area_sum_before_mm2: float
    outside_host_area_sum_after_mm2: float
    checked_section_indexes: tuple[int, ...]
    placement_eligible: bool = False
    source_demand_removed: bool = False
    lengths_counts_axes_patterns_and_mass_unchanged: bool = True
    full40d_at_each_original_required_end: bool = True
    host_policy: str = "common_material_intersection_of_all_Z_sections"
    numerical_interval_query_tolerance_mm: float = 0.0
    containment_area_tolerance_mm2: float = AREA_TOLERANCE_MM2
    not_checked: tuple[str, ...] = (
        "whole_plate_original_FE_coverage", "joint_zone_body_collisions",
        "actual_Z_and_cross_direction_3D_collisions", "existing_revit_reinforcement",
        "stock_batch_witness_recomputation", "Revit_readback", "engineering_acceptance",
    )


def _solid_fit_finite(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and abs(value) <= 1e9)


def _solid_fit_interval(values, *, bbox=False):
    return (isinstance(values, tuple) and len(values) == (4 if bbox else 2)
            and all(_solid_fit_finite(v) for v in values)
            and (values[0] < values[2] and values[1] < values[3] if bbox else values[0] < values[1]))


def _solid_fit_validate(demand, zone, constraints):
    """Strict numerical checks before the shared detailing/pattern validators."""
    if (not isinstance(demand, DemandMap) or not isinstance(zone, CompositeLayoutZone)
            or not isinstance(constraints, LayoutConstraints)
            or not isinstance(demand.direction, Direction)
            or not isinstance(demand.direction.axis, Axis) or not isinstance(demand.direction.layer, Layer)
            or not isinstance(zone.direction, Direction) or not isinstance(zone.direction.axis, Axis)
            or not isinstance(zone.direction.layer, Layer)
            or zone.direction != demand.direction
            or not isinstance(zone.id, str) or not zone.id.strip()
            or type(zone.level_index) is not int or not 0 <= zone.level_index < len(demand.levels)
            or not _solid_fit_interval(zone.demand_bbox, bbox=True)
            or not _solid_fit_finite(constraints.anchorage_diameters)
            or constraints.anchorage_diameters != 40):
        raise ValueError("Typed finite composite demand/zone and explicit full40d required")
    if not isinstance(zone.placement, RecipePlacement):
        raise ValueError("Typed complete explicit recipe placement required")
    if (not isinstance(zone.components, tuple) or not 1 <= len(zone.components) <= 64
            or not isinstance(demand.cells, tuple) or not 1 <= len(demand.cells) <= 10000):
        raise CompositeSolidHostFitLimitError("Complete bounded source cells/components required")
    seen = set()
    for cell in demand.cells:
        if (not isinstance(cell, DemandCell) or type(cell.id) is not int or cell.id in seen
                or type(cell.level_index) is not int or not 0 <= cell.level_index < len(demand.levels)
                or not isinstance(cell.poly, tuple) or not 3 <= len(cell.poly) <= 128
                or any(not isinstance(p, (tuple, list)) or len(p) != 2
                       or any(not _solid_fit_finite(v) for v in p) for p in cell.poly)):
            raise ValueError("Original FE geometry must contain finite typed points")
        seen.add(cell.id)
        poly = Polygon(cell.poly)
        if not poly.is_valid or poly.area <= 0:
            raise ValueError("Original FE polygon must be valid and nondegenerate")
    for component in zone.components:
        if (not isinstance(component, PatternedRebarSet) or type(component.component_index) is not int
                or type(component.bar_count) is not int or component.bar_count < 1
                or not _solid_fit_interval(component.axis_window_mm)
                or not _solid_fit_interval(component.longitudinal_interval_mm)
                or any(not _solid_fit_finite(value) or value <= 0 for value in (
                    component.required_length_mm, component.anchored_length_mm,
                    component.installed_length_mm, component.mass_kg))):
            raise ValueError("Finite complete physical component dimensions/count required")
    placements = (zone.placement.background, *zone.placement.additions,
                  *(component.placement for component in zone.components))
    for placement in placements:
        if (not isinstance(placement, AxisPlacement) or not isinstance(placement.pattern, PeriodicAxisPattern)
                or not _solid_fit_finite(placement.origin_mm)
                or placement.axis_depth_from_face_mm is not None and (
                    not _solid_fit_finite(placement.axis_depth_from_face_mm) or placement.axis_depth_from_face_mm <= 0)
                or not _solid_fit_finite(placement.pattern.period_mm)
                or any(not _solid_fit_finite(v) for v in placement.pattern.offsets_mm)):
            raise ValueError("Explicit finite original pattern origins/offsets required")
    check = evaluate_composite_zone(demand, zone, constraints=constraints)
    if not check.geometry_valid:
        raise ValueError("Invalid source composite geometry: " + "; ".join(check.diagnostics))
    _check_patterns(zone, STO_279_COVERAGE_POLICY)
    if check.physical_bar_count > SOLID_FIT_MAX_BARS:
        raise CompositeSolidHostFitLimitError("Complete physical inventory exceeds solid-fit bar budget")
    return check.physical_bar_count


def _intersect_shift_windows(first, second):
    """Intersection of complete sorted closed interval unions, without sampling."""
    result, i, j = [], 0, 0
    while i < len(first) and j < len(second):
        lo, hi = max(first[i][0], second[j][0]), min(first[i][1], second[j][1])
        if lo <= hi:
            result.append((lo, hi))
        elif lo-hi <= SOLID_FIT_TOL_MM:
            # Last-bit disagreement at a contact still needs independent full-body
            # containment and full40d checks after selection.
            result.append(((lo+hi)/2, (lo+hi)/2))
        if first[i][1] < second[j][1]:
            i += 1
        else:
            j += 1
    if len(result) > SOLID_FIT_MAX_INTERVALS:
        raise CompositeSolidHostFitLimitError("Complete common shift-window budget exceeded")
    return tuple(result)


def _solid_host_shift_windows(bars, material, cover, *, numerical_retry=False):
    """Exact host interval query; optional ULP guard never changes installed bars.

    The retry makes only the INTERNAL query kernel smaller by two machine-scale
    epsilons, then re-centres its interval. It recovers decimal tangent windows
    lost by strict floating subtraction in the shared helper. Original full-size
    bars remain the candidates and MUST pass independent body containment.
    """
    host_windows, cache = None, {}
    empty_individual, maximum_epsilon = False, 0.0
    magnitude = max((abs(v) for v in material.bounds), default=1.0) if not material.is_empty else 1.0
    for bar in bars:
        key = (bar.diameter_mm, bar.transverse_axis_mm, bar.installed_length_mm)
        if key not in cache:
            epsilon = (min(SOLID_FIT_TOL_MM, 8*math.ulp(max(1.0, magnitude,
                       *(abs(v) for v in bar.installed_interval_mm), bar.installed_length_mm)))
                       if numerical_retry else 0.0)
            query = bar if not epsilon else replace(bar, installed_interval_mm=(
                bar.installed_interval_mm[0]+epsilon, bar.installed_interval_mm[1]-epsilon))
            intervals = _host_intervals(query, material, cover, SOLID_FIT_MAX_INTERVALS)
            cache[key] = tuple((a-epsilon, b-epsilon) for a, b in intervals)
            maximum_epsilon = max(maximum_epsilon, epsilon)
        absolute = cache[key]
        empty_individual |= not bool(absolute)
        delta = tuple((a-bar.installed_interval_mm[0], b-bar.installed_interval_mm[0]) for a, b in absolute)
        host_windows = delta if host_windows is None else _intersect_shift_windows(host_windows, delta)
    return host_windows or (), empty_individual, maximum_epsilon


def fit_composite_zone_to_solid_host(
    demand: DemandMap, zone: CompositeLayoutZone, host: OrthogonalSolidHost, *, constraints: LayoutConstraints,
) -> CompositeSolidHostFitResult:
    """Opt-in ONE common along-axis translation of all original physical bars.

    The complete real Solid (including holes and ledges) is checked through the
    common material of every Z section. No demand rectangle, transverse origin,
    STO pattern, component length, diameter, count or mass changes. Only surplus
    beyond the original demand interval AND40d at BOTH ends can be redistributed.
    ``blocked`` preserves the original zone; it is not a partial fitted result.
    """
    count = _solid_fit_validate(demand, zone, constraints)
    _validate_host(host, 20000)
    material = host.sections[0].footprint
    for section in host.sections[1:]:
        material = material.intersection(section.footprint)
    start, end = longitudinal_interval(zone.direction.axis, zone.demand_bbox)
    source_lo, source_hi = -math.inf, math.inf
    bars = []
    for component in zone.components:
        a, b = component.longitudinal_interval_mm
        extension = 40*component.rebar.diameter
        source_lo = max(source_lo, end+extension-b)
        source_hi = min(source_hi, start-extension-a)
        coordinates = pattern_coordinates(component.placement, component.axis_window_mm,
                                          maximum_bars=SOLID_FIT_MAX_BARS)
        if len(coordinates) != component.bar_count:
            raise ValueError("Independent pattern expansion changed original physical count")
        for index, coordinate in enumerate(coordinates):
            # Internal geometry carrier only. This does not invent source-owner
            # certificates or a steel grade for a Revit/export physical plan.
            bars.append(PhysicalBar(f"{zone.id}/{component.component_index}/{index}", zone.direction,
                "geometry-only-not-a-steel-grade", component.rebar.diameter, coordinate,
                component.longitudinal_interval_mm, ()))
    if len(bars) != count or source_lo > source_hi+SOLID_FIT_TOL_MM:
        raise ValueError("Original complete inventory/full40d shift window is inconsistent")
    if source_lo > source_hi:
        source_lo = source_hi = (source_lo+source_hi)/2
    source_window = (source_lo, source_hi)
    outside_before = tuple(box(*_envelope(bar, host.side_cover_mm)).difference(material).area for bar in bars)
    blocked_before = sum(value > AREA_TOLERANCE_MM2 for value in outside_before)
    host_windows, empty_individual, query_epsilon = _solid_host_shift_windows(bars, material, host.side_cover_mm)
    admissible = _intersect_shift_windows(host_windows, (source_window,))
    if not admissible:
        host_windows, empty_individual, query_epsilon = _solid_host_shift_windows(
            bars, material, host.side_cover_mm, numerical_retry=True)
        admissible = _intersect_shift_windows(host_windows, (source_window,))
    reason = None
    if not host_windows:
        reason = ("a_physical_axis_has_no_full_length_host_interval" if empty_individual
                  else "no_common_host_shift_for_all_components_and_axes")
    elif not admissible:
        reason = "host_translation_would_shorten_required40d"
    if reason:
        return CompositeSolidHostFitResult(zone, zone, "blocked", 0.0, source_window,
            host_windows, admissible, reason, count, not blocked_before, not blocked_before,
            blocked_before, blocked_before, math.fsum(outside_before), math.fsum(outside_before),
            tuple(range(len(host.sections))), numerical_interval_query_tolerance_mm=query_epsilon)
    shift = min((min(max(0.0, a), b) for a, b in admissible), key=lambda value: (abs(value), value))
    fitted = zone if shift == 0 else replace(zone, components=tuple(replace(component,
        longitudinal_interval_mm=(component.longitudinal_interval_mm[0]+shift,
                                  component.longitudinal_interval_mm[1]+shift)) for component in zone.components))
    if not evaluate_composite_zone(demand, fitted, constraints=constraints).geometry_valid:
        raise ValueError("Independent detailing rejected full40d/count/length after common translation")
    if any(replace(after, longitudinal_interval_mm=before.longitudinal_interval_mm) != before
           for before, after in zip(zone.components, fitted.components)):
        raise ValueError("Common translation changed frozen physical attributes")
    outside_after = tuple(box(*_envelope(replace(bar, installed_interval_mm=(bar.installed_interval_mm[0]+shift,
        bar.installed_interval_mm[1]+shift)), host.side_cover_mm)).difference(material).area for bar in bars)
    if any(value > AREA_TOLERANCE_MM2 for value in outside_after):
        raise ValueError("Exact interval candidate failed independent complete physical-body containment")
    return CompositeSolidHostFitResult(zone, fitted, "unchanged" if shift == 0 else "shifted", shift,
        source_window, host_windows, admissible, None, count, not blocked_before, True,
        blocked_before, 0, math.fsum(outside_before), math.fsum(outside_after), tuple(range(len(host.sections))),
        numerical_interval_query_tolerance_mm=query_epsilon)
