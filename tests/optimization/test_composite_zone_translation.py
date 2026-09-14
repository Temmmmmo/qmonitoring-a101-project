"""Transverse window motion exchanges axes, never phases, bars or original FE."""
from copy import deepcopy
from dataclasses import replace

import pytest
from shapely.affinity import affine_transform
from shapely.geometry import Polygon, box

from rebar.application.analyze_composite_plate import CompositeDirectionSettings, _placements
from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.contracts.composite_coverage import MONOTONE_SINGLE_STO_COVERAGE_POLICY
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutConstraints
from rebar.optimization.services.axis_patterns import pattern_coordinates
from rebar.optimization.services.composite_coverage import (
    check_composite_zone_coverage_geometry, evaluate_composite_coverage,
)
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.composite_zone_translation import propose_transverse_zone_translations
from rebar.optimization.services.cutting import PLATE_11700_BATCH_PROFILE, PLATE_11700_CUT_LENGTHS_MM


def case(axis=Axis.X, layer=Layer.TOP, step=150, batch=False):
    direction = Direction(layer, axis)
    polygon = box(1000, 0, 2000, 300)
    bounds = (1000, -150, 2000, 450)
    if axis is Axis.Y:
        polygon = affine_transform(polygon, (0, 1, 1, 0, 0, 0))
        bounds = (bounds[1], bounds[0], bounds[3], bounds[2])
    recipe = ReinforcementRecipe(Rebar(300, 10), (Rebar(step, 10),))
    levels = (DemandLevel(0, 1, 0, 1, "background", None, False, replace(recipe, additions=())),
        DemandLevel(1, 2, 1, 2, "addition", recipe.additions[0], True, recipe))
    cell = DemandCell(42, tuple(polygon.exterior.coords)[:-1],
        (polygon.centroid.x, polygon.centroid.y), 2, 1)
    demand = DemandMap(direction, levels, (cell,), polygon.bounds)
    constraints = (LayoutConstraints(cutting_profile=PLATE_11700_BATCH_PROFILE,
        allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM) if batch else LayoutConstraints())
    setting = CompositeDirectionSettings(direction, 0, 100, 150, "A500", "synthetic explicit profile")
    placement = dict(_placements(demand, setting))[1]
    zone = build_composite_zone(demand, bounds, 1, "zone", placement, constraints=constraints,
        **({"installed_lengths_mm": (3900,)} if batch else {}))
    return demand, zone, constraints


def check(demand, zone, constraints):
    return check_composite_zone_coverage_geometry(demand, zone, constraints=constraints,
        policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY)


def coordinates(zone):
    return pattern_coordinates(zone.components[0].placement, zone.components[0].axis_window_mm)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
@pytest.mark.parametrize("layer", (Layer.TOP, Layer.BOTTOM))
def test_nearest_inward_axis_exchange_preserves_phase_count_mass_full_FE_and_40d(axis, layer):
    demand, zone, constraints = case(axis, layer)
    original = deepcopy((demand, zone, constraints))
    result = propose_transverse_zone_translations(demand, zone, constraints=constraints)
    assert result.candidates[0] is zone and not result.placement_eligible
    assert result.accepted_before_limit == len(result.candidates) and not result.truncated
    assert coordinates(zone) == (-100, 100, 200, 400)
    inward = next(candidate for candidate in result.candidates if coordinates(candidate) == (100, 200, 400, 500))
    across = 1 if axis is Axis.X else 0
    assert 50 < inward.demand_bbox[across]-zone.demand_bbox[across] < 50.001
    # The selected window may extend outside the host; only actual bar bodies
    # plus cover constrain transverse containment, not an invented window cover.
    assert inward.demand_bbox[across] < 0
    assert min(coordinates(inward))-10/2 >= 25
    assert max(coordinates(inward))+10/2 <= 900-25
    for candidate in result.candidates:
        assert candidate.placement is zone.placement
        assert candidate.id == zone.id and candidate.recipe == zone.recipe
        assert candidate.components[0].bar_count == 4
        for field in ("mass_kg", "installed_length_mm", "anchored_length_mm", "required_length_mm",
                      "longitudinal_interval_mm", "rebar", "placement"):
            assert getattr(candidate.components[0], field) == getattr(zone.components[0], field)
        assert candidate.demand_bbox[across+2]-candidate.demand_bbox[across] == pytest.approx(600)
        assert check(demand, candidate, constraints).geometry_and_pattern_valid
        coverage = evaluate_composite_coverage(demand, (candidate,), constraints=constraints,
            policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY)
        assert coverage.status == "pass" and coverage.uncovered_cell_count == 0
    assert (demand, zone, constraints) == original


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
@pytest.mark.parametrize("step", (100, 150, 300))
def test_inventory_and_original_service_are_preserved_for_every_supported_pattern(axis, step):
    demand, zone, constraints = case(axis=axis, step=step)
    result = propose_transverse_zone_translations(demand, zone, constraints=constraints)
    signatures = [coordinates(candidate) for candidate in result.candidates]
    assert len(set(signatures)) == len(signatures)
    across = 1 if axis is Axis.X else 0
    shifts = [candidate.demand_bbox[across]-zone.demand_bbox[across] for candidate in result.candidates]
    assert shifts == sorted(shifts, key=lambda delta: (abs(delta), delta))
    source_offered = box(*check(demand, zone, constraints).component_service_bboxes_mm[0])
    obligation = Polygon(demand.cells[0].poly).intersection(source_offered)
    for candidate in result.candidates:
        assert candidate.components[0].bar_count == zone.components[0].bar_count
        assert box(*check(demand, candidate, constraints).component_service_bboxes_mm[0]).covers(obligation)
        assert candidate.placement == zone.placement


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_batch_lengths_and_preexisting_asymmetric_longitudinal_intervals_survive_rebuild(axis):
    demand, zone, constraints = case(axis=axis, batch=True)
    old = zone.components[0]
    shifted = tuple(value+100 for value in old.longitudinal_interval_mm)
    zone = replace(zone, components=(replace(old, longitudinal_interval_mm=shifted),))
    assert check(demand, zone, constraints).geometry_and_pattern_valid
    result = propose_transverse_zone_translations(demand, zone, constraints=constraints)
    assert len(result.candidates) > 1
    assert all(candidate.components[0].longitudinal_interval_mm == shifted for candidate in result.candidates)
    assert all(candidate.components[0].installed_length_mm == 3900 for candidate in result.candidates)
    assert all(candidate.components[0].mass_kg == old.mass_kg for candidate in result.candidates)


def test_positive_tiny_original_fragment_cannot_be_discarded_as_noise():
    demand, zone, constraints = case()
    polygon = box(1100, -450, 1200, -150+1e-10)
    tiny = polygon.intersection(box(*check(demand, zone, constraints).component_service_bboxes_mm[0]))
    cell = DemandCell(71, tuple(polygon.exterior.coords)[:-1],
        (polygon.centroid.x, polygon.centroid.y), 2, 1)
    demand = replace(demand, cells=(*demand.cells, cell))
    result = propose_transverse_zone_translations(demand, zone, constraints=constraints)
    assert tiny.area > 0 and tiny.area < 1e-6
    assert not any(coordinates(candidate) == (100, 200, 400, 500) for candidate in result.candidates)
    assert all(box(*check(demand, candidate, constraints).component_service_bboxes_mm[0]).covers(tiny)
        for candidate in result.candidates)


def test_zone_does_not_claim_or_freeze_a_stronger_FE_it_cannot_serve():
    demand, zone, constraints = case()
    stronger = ReinforcementRecipe(Rebar(300, 10), (Rebar(150, 12),))
    level = DemandLevel(2, 3, 2, 3, "stronger", stronger.additions[0], True, stronger)
    polygon = box(1100, -150, 1200, -149)
    cell = DemandCell(71, tuple(polygon.exterior.coords)[:-1],
        (polygon.centroid.x, polygon.centroid.y), 3, 2)
    demand = replace(demand, levels=(*demand.levels, level), cells=(*demand.cells, cell))
    result = propose_transverse_zone_translations(demand, zone, constraints=constraints)
    assert any(coordinates(candidate) == (100, 200, 400, 500) for candidate in result.candidates)
    # A proposal is not a global coverage approval: another sufficient zone is
    # still required for the stronger FE, which has never been silently lowered.
    assert all(evaluate_composite_coverage(demand, (candidate,), constraints=constraints,
        policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY).status != "pass" for candidate in result.candidates)


def test_only_served_intersection_is_frozen_not_the_entire_partly_served_FE():
    demand, zone, constraints = case()
    polygon = box(1100, 400, 1200, 700)
    cell = DemandCell(71, tuple(polygon.exterior.coords)[:-1],
        (polygon.centroid.x, polygon.centroid.y), 2, 1)
    demand = replace(demand, cells=(*demand.cells, cell))
    result = propose_transverse_zone_translations(demand, zone, constraints=constraints)
    assert any(coordinates(candidate) == (100, 200, 400, 500) for candidate in result.candidates)
    fragment = polygon.intersection(box(*check(demand, zone, constraints).component_service_bboxes_mm[0]))
    assert fragment.area < polygon.area
    assert all(box(*check(demand, candidate, constraints).component_service_bboxes_mm[0]).covers(fragment)
        for candidate in result.candidates)


def test_no_demand_zone_retains_all_bars_and_still_reports_candidate_cap():
    demand, zone, constraints = case()
    demand = replace(demand, cells=tuple(replace(cell, level_index=0, aci=1) for cell in demand.cells))
    result = propose_transverse_zone_translations(demand, zone, constraints=constraints,
        maximum_shift_mm=1200, maximum_candidates=2)
    assert result.candidates[0] is zone and len(result.candidates) == 2
    assert result.accepted_before_limit > 2 and result.truncated
    assert all(candidate.components[0].bar_count == zone.components[0].bar_count for candidate in result.candidates)


def test_zero_shift_is_exactly_original_and_cap_does_not_hide_search_alternatives():
    demand, zone, constraints = case()
    zero = propose_transverse_zone_translations(demand, zone, constraints=constraints, maximum_shift_mm=0)
    assert zero.candidates == (zone,) and zero.evaluated_event_count == 1
    assert zero.accepted_before_limit == 1 and not zero.truncated
    capped = propose_transverse_zone_translations(demand, zone, constraints=constraints, maximum_candidates=1)
    assert capped.candidates == (zone,) and capped.accepted_before_limit == 3 and capped.truncated


@pytest.mark.parametrize(("argument", "value"), (("maximum_shift_mm", -1), ("maximum_shift_mm", 1201),
    ("maximum_shift_mm", float("nan")), ("maximum_shift_mm", float("inf")), ("maximum_shift_mm", True),
    ("maximum_candidates", 0), ("maximum_candidates", 129), ("maximum_candidates", True),
    ("maximum_candidates", 4.0)))
def test_invalid_limits_are_rejected_not_silently_clamped(argument, value):
    demand, zone, constraints = case()
    with pytest.raises(ValueError):
        propose_transverse_zone_translations(demand, zone, constraints=constraints, **{argument: value})


@pytest.mark.parametrize("change", ("nan_bounds", "bool_bounds", "nan_interval", "nan_mass", "bool_count",
    "missing_component", "changed_phase", "short_40d", "float_diameter", "unknown_cell", "bool_cell_id"))
def test_invalid_original_geometry_or_demand_never_becomes_a_proposal(change):
    demand, zone, constraints = case()
    component = zone.components[0]
    if change == "nan_bounds":
        zone = replace(zone, demand_bbox=(1000, float("nan"), 2000, 450))
    elif change == "bool_bounds":
        zone = replace(zone, demand_bbox=(1000, False, 2000, 450))
    elif change == "nan_interval":
        zone = replace(zone, components=(replace(component, longitudinal_interval_mm=(float("nan"), 2400)),))
    elif change == "nan_mass":
        zone = replace(zone, components=(replace(component, mass_kg=float("nan")),))
    elif change == "bool_count":
        zone = replace(zone, components=(replace(component, bar_count=True),))
    elif change == "missing_component":
        zone = replace(zone, components=())
    elif change == "changed_phase":
        zone = replace(zone, components=(replace(component, placement=replace(component.placement, origin_mm=1)),))
    elif change == "short_40d":
        zone = replace(zone, components=(replace(component, longitudinal_interval_mm=(601, 2401)),))
    elif change == "float_diameter":
        zone = replace(zone, components=(replace(component, rebar=Rebar(150, 10.0)),))
    elif change == "unknown_cell":
        demand = replace(demand, cells=(replace(demand.cells[0], level_index=99),))
    else:
        demand = replace(demand, cells=(replace(demand.cells[0], id=True),))
    with pytest.raises((ValueError, KeyError)):
        propose_transverse_zone_translations(demand, zone, constraints=constraints)


def test_reduced_anchorage_profile_is_not_accepted_for_transverse_repair():
    demand, zone, constraints = case()
    with pytest.raises(ValueError, match="full40d"):
        propose_transverse_zone_translations(demand, zone, constraints=replace(constraints, anchorage_diameters=15))
