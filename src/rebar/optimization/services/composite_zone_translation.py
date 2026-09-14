"""Finite transverse window translations with frozen original FE obligations.

The global STO axes do not move. Translating a fixed-width selection window may
exchange an outside axis for an inside axis, but may not change the number,
diameter, installed length or phase of a component. Every positive fragment of
original sufficient demand served by this zone remains served by each proposal.

This service does not check the host, other zones, stock cutting, Z or Revit.
Candidates are proposals for a subsequent joint search and fresh full checks.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from shapely.geometry import Polygon, box

from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe

from ..contracts.composite_coverage import MONOTONE_SINGLE_STO_COVERAGE_POLICY
from ..contracts.placement import CompositeLayoutZone, PatternedRebarSet, RecipePlacement
from ..contracts.problem import DemandMap, LayoutConstraints
from .axis_patterns import pattern_coordinates
from .composite_coverage import (
    check_composite_zone_coverage_geometry, monotone_single_recipe_covers,
    validate_composite_demand,
)
from .composite_detailing import build_composite_zone, prepare_composite_detailing
from .cutting import PLATE_11700_BATCH_PROFILE
from .geometry import GEOMETRY_TOLERANCE_MM

MAX_EVENT_REPRESENTATIVES = 20000


@dataclass(frozen=True)
class TransverseZoneTranslationCandidates:
    candidates: tuple[CompositeLayoutZone, ...]
    evaluated_event_count: int
    accepted_before_limit: int
    truncated: bool
    placement_eligible: bool = False


def _finite(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
        and math.isfinite(value) and abs(value) <= 1e9)


def _bounds(value, count):
    return isinstance(value, tuple) and len(value) == count and all(_finite(v) for v in value)


def _validate(demand, zone, constraints, maximum_shift_mm, maximum_candidates):
    if (not isinstance(demand, DemandMap) or not isinstance(demand.direction, Direction)
            or not isinstance(demand.direction.axis, Axis) or not isinstance(demand.direction.layer, Layer)
            or not isinstance(constraints, LayoutConstraints)
            or not _finite(constraints.anchorage_diameters) or constraints.anchorage_diameters != 40
            or type(constraints.min_width_cells) is not int or constraints.min_width_cells < 1):
        raise ValueError("Typed original demand and full40d constraints required")
    if not _finite(maximum_shift_mm) or not 0 <= maximum_shift_mm <= 1200:
        raise ValueError("maximum_shift_mm must be finite in 0..1200")
    if type(maximum_candidates) is not int or not 1 <= maximum_candidates <= 128:
        raise ValueError("maximum_candidates must be an integer in 1..128")
    if (not isinstance(zone, CompositeLayoutZone) or not _bounds(zone.demand_bbox, 4)
            or type(zone.level_index) is not int or not isinstance(zone.placement, RecipePlacement)
            or not isinstance(zone.components, tuple) or len(zone.components) != 1):
        raise ValueError("One typed original STO component and finite zone geometry required")
    validate_composite_demand(demand)
    if (any(type(cell.id) is not int or type(cell.level_index) is not int for cell in demand.cells)
            or any(type(level.index) is not int for level in demand.levels)):
        raise ValueError("Original integer FE identifiers and level indices required")
    for level in demand.levels:
        recipe = level.recipe
        if (not isinstance(recipe, ReinforcementRecipe) or len(recipe.additions) > 1
                or recipe.background.step != 300):
            raise ValueError("Explicit single-addition scale on a common @300 background required")
        for spec in (recipe.background, *recipe.additions):
            if (not isinstance(spec, Rebar) or type(spec.diameter) is not int
                    or not 6 <= spec.diameter <= 40 or type(spec.step) is not int
                    or spec.step not in (100, 150, 300)):
                raise ValueError("Explicit supported integer diameter/spacing required")
    for component in zone.components:
        if (not isinstance(component, PatternedRebarSet)
                or not isinstance(component.rebar, Rebar)
                or type(component.rebar.diameter) is not int or type(component.rebar.step) is not int
                or not _bounds(component.axis_window_mm, 2)
                or not _bounds(component.longitudinal_interval_mm, 2)
                or any(not _finite(v) for v in (component.required_length_mm,
                    component.anchored_length_mm, component.installed_length_mm, component.mass_kg))
                or type(component.component_index) is not int
                or type(component.bar_count) is not int or component.bar_count < 1):
            raise ValueError("Finite original component geometry/count required")
    check = check_composite_zone_coverage_geometry(demand, zone, constraints=constraints,
        policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY)
    if not check.geometry_and_pattern_valid:
        raise ValueError("Original zone failed independent geometry/STO/full40d validation")
    return check


def _frozen_fragments(demand, zone, service_box):
    """No area cutoff: even a tiny positive fragment is an original obligation."""
    offered = box(*service_box)
    parts = []
    for cell in demand.cells:
        required = demand.level(cell.level_index).recipe
        if not monotone_single_recipe_covers(required, zone.recipe):
            continue
        fragment = Polygon(cell.poly).intersection(offered)
        if not fragment.is_empty and fragment.area > 0:
            parts.append(fragment)
    return tuple(parts)


def _representatives(zone, lower, upper):
    """All finite axis-selection events and adjacent open-stratum witnesses.

    pattern_coordinates includes its shared closed-window tolerance, so both the
    nominal contacts and tolerance-adjusted selection events are retained. Frozen
    FE window limits are supplied as lower/upper and cannot be stepped over.
    """
    if lower > upper:
        raise ValueError("Original window no longer contains its frozen service fragments")
    events = {lower, upper, 0.0}
    tolerance = GEOMETRY_TOLERANCE_MM
    for component in zone.components:
        placement = component.placement
        for boundary, entering in zip(component.axis_window_mm, (False, True)):
            coordinates = pattern_coordinates(placement,
                (boundary+lower-2*tolerance, boundary+upper+2*tolerance))
            for coordinate in coordinates:
                nominal = coordinate-boundary
                exact = nominal-tolerance if entering else nominal+tolerance
                for value in (nominal, exact):
                    if lower <= value <= upper:
                        events.add(value)
        period = placement.pattern.period_mm
        for index in range(math.ceil(lower/period), math.floor(upper/period)+1):
            events.add(index*period)
    ordered = sorted(events)
    result = set(ordered)
    for left, right in zip(ordered, ordered[1:]):
        if left >= right:
            continue
        result.add((left+right)/2)
        # Include nearest usable representatives on either side of a contact;
        # do not let a fixed epsilon jump over a narrower event interval.
        epsilon = min(4*tolerance, (right-left)/4)
        result.update((left+epsilon, right-epsilon,
            math.nextafter(left, right), math.nextafter(right, left)))
    if len(result) > MAX_EVENT_REPRESENTATIVES:
        raise ValueError("Transverse event budget exceeded; no events were silently dropped")
    return tuple(sorted(result, key=lambda shift: (abs(shift), shift)))


def propose_transverse_zone_translations(
    demand: DemandMap, zone: CompositeLayoutZone, *, constraints: LayoutConstraints,
    maximum_shift_mm: float = 600, maximum_candidates: int = 32,
) -> TransverseZoneTranslationCandidates:
    """Propose fixed-phase, fixed-inventory windows preserving original service.

    The original is always first. Remaining proposals are ranked by |translation|
    and then signed translation. Physically identical axis selections are merged;
    among valid sampled windows the nearest is retained. ``accepted_before_limit``
    counts these distinct physical arrangements, including the original.

    ``truncated`` describes only the explicit returned-candidate cap. Even without
    truncation this is a finite proposal set, not proof of global infeasibility:
    phases, recipes, widths, lengths and each zone's served FE fragments are fixed.
    A zone with no positive served demand is retained with its complete inventory.
    """
    source_check = _validate(demand, zone, constraints, maximum_shift_mm, maximum_candidates)
    fragments = _frozen_fragments(demand, zone, source_check.component_service_bboxes_mm[0])
    across = 1 if demand.direction.axis is Axis.X else 0
    original_lo, original_hi = zone.demand_bbox[across], zone.demand_bbox[across+2]
    lower, upper = -maximum_shift_mm, maximum_shift_mm
    if fragments:
        lower = max(lower, max(part.bounds[across+2] for part in fragments)-original_hi)
        upper = min(upper, min(part.bounds[across] for part in fragments)-original_lo)
    shifts = _representatives(zone, lower, upper)
    context = prepare_composite_detailing(demand)
    original_signature = tuple(pattern_coordinates(c.placement, c.axis_window_mm) for c in zone.components)
    signatures, candidates = {original_signature}, [zone]
    evaluated = 0
    for shift in shifts:
        evaluated += 1
        if shift == 0:
            continue
        bounds = list(zone.demand_bbox)
        bounds[across], bounds[across+2] = original_lo+shift, original_hi+shift
        window = bounds[across], bounds[across+2]
        signature = tuple(pattern_coordinates(component.placement, window) for component in zone.components)
        if signature in signatures or any(len(axes) != component.bar_count
                for axes, component in zip(signature, zone.components, strict=True)):
            continue
        kwargs = ({"installed_lengths_mm": tuple(c.installed_length_mm for c in zone.components)}
                  if constraints.cutting_profile == PLATE_11700_BATCH_PROFILE else {})
        proposed = build_composite_zone(demand, tuple(bounds), zone.level_index, zone.id, zone.placement,
            constraints=constraints, context=context, **kwargs)
        proposed = replace(proposed, components=tuple(replace(new,
            longitudinal_interval_mm=original.longitudinal_interval_mm)
            for new, original in zip(proposed.components, zone.components, strict=True)))
        if any(new.bar_count != original.bar_count or new.rebar != original.rebar
                or new.installed_length_mm != original.installed_length_mm
                or new.mass_kg != original.mass_kg
                for new, original in zip(proposed.components, zone.components, strict=True)):
            raise ValueError("Transverse proposal changed the original installed inventory")
        check = check_composite_zone_coverage_geometry(demand, proposed, constraints=constraints,
            context=context, policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY)
        if not check.geometry_and_pattern_valid:
            continue
        offered = box(*check.component_service_bboxes_mm[0])
        if not all(offered.covers(fragment) for fragment in fragments):
            continue
        signatures.add(signature)
        candidates.append(proposed)
    return TransverseZoneTranslationCandidates(tuple(candidates[:maximum_candidates]), evaluated,
        len(candidates), len(candidates) > maximum_candidates)
