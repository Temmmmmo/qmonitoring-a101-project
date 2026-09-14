"""Shared asymmetric stock-slack translation, never a crop or independent shift."""
from copy import deepcopy
from dataclasses import replace

import pytest
from shapely.affinity import affine_transform
from shapely.geometry import Polygon, box

from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.contracts.placement import AxisPlacement, PeriodicAxisPattern
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutConstraints
from rebar.optimization.services.axis_patterns import a101_sto_279_slab_recipe_placement
from rebar.optimization.services.composite_detailing import build_composite_zone, evaluate_composite_zone
from rebar.optimization.services.composite_host_fit import (
    CompositeSolidHostFitLimitError, fit_composite_zone_to_solid_host,
)
from rebar.optimization.services.cutting import PLATE_11700_CUT_LENGTHS_MM
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection


def case(axis=Axis.X, layer=Layer.TOP, specs=(Rebar(150, 18),), footprint=None):
    direction = Direction(layer, axis)
    recipe = ReinforcementRecipe(Rebar(300, 18), specs)
    levels = (DemandLevel(0, 1, None, None, "background", None, False,
                          ReinforcementRecipe(recipe.background)),
              DemandLevel(1, 2, None, None, "explicit", specs[0] if len(specs) == 1 else None, True, recipe))
    polygons = (box(0, 0, 3900, 300), box(0, 300, 3900, 600))
    if axis is Axis.Y:
        polygons = tuple(affine_transform(poly, (0, 1, 1, 0, 0, 0)) for poly in polygons)
    cells = tuple(DemandCell(index+1, tuple(p.exterior.coords)[:-1], (p.centroid.x, p.centroid.y), 2, 1)
                  for index, p in enumerate(polygons))
    bounds = (0, 0, 3900, 600) if axis is Axis.X else (0, 0, 600, 3900)
    demand = DemandMap(direction, levels, cells, bounds)
    placement = a101_sto_279_slab_recipe_placement(recipe, background_origin_mm=0, contact_side="left")
    placement = replace(placement, additions=tuple(replace(item, origin_mm=150) if item.origin_mm is None else item
                                                   for item in placement.additions))
    constraints = LayoutConstraints(allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM, cutting_profile="plate-11700")
    zone = build_composite_zone(demand, bounds, 1, "whole-zone", placement, constraints=constraints)
    footprint = box(-800, -200, 6000, 1200) if footprint is None else footprint
    if axis is Axis.Y:
        footprint = affine_transform(footprint, (0, 1, 1, 0, 0, 0))
    solid = OrthogonalSolidHost((SolidHostSection(0, 200, footprint),), 25, 25, 25, footprint.area*200, 6)
    return demand, zone, solid, constraints


def fit(data):
    demand, zone, solid, constraints = data
    return fit_composite_zone_to_solid_host(demand, zone, solid, constraints=constraints)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
@pytest.mark.parametrize("layer", (Layer.TOP, Layer.BOTTOM))
def test_asymmetric_inward_move_keeps_full40d_every_dimension_and_original_demand(axis, layer):
    data = case(axis, layer)
    untouched = deepcopy(data)
    result = fit(data)
    assert result.status == "shifted"
    assert result.shift_mm == 200
    assert result.source_shift_window_mm == (-255, 255)
    assert result.admissible_shift_windows_mm == ((200, 255),)
    assert result.host_blocked_bar_count_before == result.physical_bar_count == 4
    assert result.host_blocked_bar_count_after == 0
    assert not result.containment_before and result.containment_after
    before, after = result.original_zone.components[0], result.fitted_zone.components[0]
    assert before.longitudinal_interval_mm == (-975, 4875)
    assert after.longitudinal_interval_mm == (-775, 5075)
    assert replace(after, longitudinal_interval_mm=before.longitudinal_interval_mm) == before
    assert result.fitted_zone.demand_bbox == result.original_zone.demand_bbox
    assert result.fitted_zone.placement == result.original_zone.placement
    assert evaluate_composite_zone(data[0], result.fitted_zone, constraints=data[3]).geometry_valid
    assert data == untouched
    assert not result.placement_eligible and not result.source_demand_removed


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_all_components_with_unequal_lengths_get_one_shared_shift(axis):
    data = case(axis, specs=(Rebar(150, 10), Rebar(300, 18)), footprint=box(-950, -200, 6000, 1200))
    result = fit(data)
    assert result.status == "shifted" and result.shift_mm == 50
    assert [c.installed_length_mm for c in result.fitted_zone.components] == [4875, 5850]
    assert result.physical_bar_count == 6
    for before, after in zip(result.original_zone.components, result.fitted_zone.components):
        assert after.longitudinal_interval_mm[0]-before.longitudinal_interval_mm[0] == 50
        assert after.longitudinal_interval_mm[1]-before.longitudinal_interval_mm[1] == 50
        assert replace(after, longitudinal_interval_mm=before.longitudinal_interval_mm) == before


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_individually_feasible_components_cannot_be_shifted_different_amounts(axis):
    data = case(axis, specs=(Rebar(150, 18), Rebar(300, 25)), footprint=box(-1100, -200, 6000, 1200))
    result = fit(data)
    assert result.status == "blocked"
    assert result.blocked_reason == "host_translation_would_shorten_required40d"
    assert result.source_shift_window_mm == (-255, 255)
    assert result.host_shift_windows_mm[0][0] == pytest.approx(387.5)
    assert result.admissible_shift_windows_mm == ()
    assert result.fitted_zone is result.original_zone is data[1]
    assert result.shift_mm == 0


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_whole_zone_avoids_small_hole_in_surplus_tail_with_no_cutting(axis):
    footprint = box(-3000, -2000, 8000, 3000).difference(box(-1100, -100, -950, 1000))
    result = fit(case(axis, footprint=footprint))
    assert result.status == "shifted"
    assert result.shift_mm == 50
    assert result.fitted_zone.components[0].longitudinal_interval_mm == (-925, 4925)
    assert result.containment_after


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_nonrectangular_outer_recess_is_checked_exactly(axis):
    footprint = box(-3000, -2000, 8000, 3000).difference(box(-3000, -100, -950, 1000))
    result = fit(case(axis, footprint=footprint))
    assert result.status == "shifted" and result.shift_mm == 50


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_hole_between_actual_axes_does_not_reject_entire_zone_bbox(axis):
    footprint = box(-2000, -2000, 8000, 3000).difference(box(1000, 270, 1100, 330))
    result = fit(case(axis, footprint=footprint))
    assert result.status == "unchanged" and result.shift_mm == 0
    assert result.fitted_zone is result.original_zone


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_actual_sto100_axes_are_preserved_and_not_replaced_by_uniform100(axis):
    data = case(axis, specs=(Rebar(100, 18),))
    result = fit(data)
    assert result.status == "shifted"
    assert result.physical_bar_count == 6
    component = result.fitted_zone.components[0]
    assert component.placement.pattern.offsets_mm == (100, 200, 282)
    assert component.placement == data[1].components[0].placement


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_axis_selection_window_may_touch_host_edge_when_actual_bodies_do_not(axis):
    result = fit(case(axis, footprint=box(-2000, 0, 8000, 600)))
    assert result.status == "unchanged" and result.containment_after


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_true_edge40d_requirement_cannot_be_repaired_by_asymmetric_shift(axis):
    result = fit(case(axis, footprint=box(-700, -200, 6000, 1200)))
    assert result.status == "blocked"
    assert result.blocked_reason == "host_translation_would_shorten_required40d"
    assert result.fitted_zone == result.original_zone


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_hole_through_required_interval_cannot_be_clipped_or_ignored(axis):
    footprint = box(-3000, -2000, 8000, 3000).difference(box(1000, 50, 1100, 550))
    result = fit(case(axis, footprint=footprint))
    assert result.status == "blocked"
    assert result.fitted_zone == result.original_zone
    assert not result.source_demand_removed


def test_all_height_sections_and_ledge_are_checked_not_only_top_outline():
    data = case(footprint=box(-2000, -200, 6000, 1200))
    demand, zone, solid, constraints = data
    lower = box(-800, -200, 6000, 1200)
    upper = solid.sections[0].footprint
    solid = replace(solid, sections=(SolidHostSection(0, 100, lower), SolidHostSection(100, 200, upper)),
                    volume_mm3=(lower.area+upper.area)*100)
    result = fit((demand, zone, solid, constraints))
    assert result.status == "shifted" and result.shift_mm == 200
    assert result.checked_section_indexes == (0, 1)


@pytest.mark.parametrize("change", ({"longitudinal_interval_mm": (float("nan"), 4875)},
    {"longitudinal_interval_mm": (-700, 5150)}, {"installed_length_mm": 1000},
    {"bar_count": True}, {"bar_count": 5}, {"mass_kg": float("inf")}, {"axis_window_mm": (False, 600)}))
def test_invalid_original_geometry_is_rejected_before_fitting(change):
    demand, zone, solid, constraints = case()
    zone = replace(zone, components=(replace(zone.components[0], **change),))
    with pytest.raises(ValueError):
        fit((demand, zone, solid, constraints))


def test_uniform150_is_not_accepted_as_actual_sto100_200_pattern():
    demand, zone, solid, constraints = case()
    wrong = AxisPlacement(PeriodicAxisPattern.uniform(150), 100)
    placement = replace(zone.placement, additions=(wrong,))
    zone = build_composite_zone(demand, zone.demand_bbox, 1, zone.id, placement, constraints=constraints)
    with pytest.raises(ValueError, match="100/200"):
        fit((demand, zone, solid, constraints))


def test_missing_phase_is_not_assumed_zero():
    demand, zone, solid, constraints = case()
    placement = replace(zone.placement, additions=(replace(zone.placement.additions[0], origin_mm=None),))
    with pytest.raises(ValueError, match="origins"):
        fit((demand, replace(zone, placement=placement), solid, constraints))


def test_nonorthogonal_or_invalid_solid_is_not_substituted_by_rectangle():
    demand, zone, solid, constraints = case()
    p = Polygon(((-800, -200), (6000, -200), (5900, 1200), (-800, 1200)))
    solid = replace(solid, sections=(SolidHostSection(0, 200, p),), volume_mm3=p.area*200)
    with pytest.raises(ValueError, match="axis aligned"):
        fit((demand, zone, solid, constraints))


def test_resource_limit_raises_not_partial_geometry(monkeypatch):
    monkeypatch.setattr("rebar.optimization.services.composite_host_fit.SOLID_FIT_MAX_BARS", 2)
    with pytest.raises(CompositeSolidHostFitLimitError, match="bar budget"):
        fit(case())


def test_full40d_policy_cannot_be_silently_weakened():
    demand, zone, solid, constraints = case()
    with pytest.raises(ValueError, match="full40d"):
        fit((demand, zone, solid, replace(constraints, anchorage_diameters=0)))


def test_shift_can_use_exactly_all_surplus_but_not_a_single_mm_of40d():
    result = fit(case(footprint=box(-745, -200, 6000, 1200)))
    assert result.status == "shifted" and result.shift_mm == 255
    assert result.fitted_zone.components[0].longitudinal_interval_mm[0] == -720
    blocked = fit(case(footprint=box(-744, -200, 6000, 1200)))
    assert blocked.status == "blocked"
    assert blocked.blocked_reason == "host_translation_would_shorten_required40d"


def test_negative_common_translation_moves_zone_in_from_opposite_edge():
    result = fit(case(footprint=box(-3000, -200, 4850, 1200)))
    assert result.status == "shifted" and result.shift_mm == -50
    assert result.fitted_zone.components[0].longitudinal_interval_mm == (-1025, 4825)


def test_each_axis_may_fit_individually_but_no_one_common_zone_shift_exists():
    footprint = (box(-900, 50, 5000, 150).union(box(-1100, 150, 4800, 250))
                 .union(box(-2000, 300, 7000, 600)))
    result = fit(case(footprint=footprint))
    assert result.status == "blocked"
    assert result.blocked_reason == "no_common_host_shift_for_all_components_and_axes"
    assert result.host_shift_windows_mm == ()
    assert result.fitted_zone is result.original_zone


@pytest.mark.parametrize("left", (-800.1, -800.2, -800.3, -800.4, -800.7))
@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_decimal_single_tangent_position_uses_full_original_length(left, axis):
    data = case(axis, footprint=box(left, -200, left+5900, 1200))
    result = fit(data)
    assert result.status == "shifted"
    assert result.shift_mm == pytest.approx(left+1000, abs=1e-8)
    assert result.containment_after
    assert result.outside_host_area_sum_after_mm2 <= result.containment_area_tolerance_mm2
    assert result.fitted_zone.components[0].installed_length_mm == 5850
    assert result.fitted_zone.components[0].bar_count == 4
    assert result.fitted_zone.components[0].mass_kg == data[1].components[0].mass_kg
    assert evaluate_composite_zone(data[0], result.fitted_zone, constraints=data[3]).geometry_valid


@pytest.mark.parametrize("change", ({"placement": None}, {"direction": Direction("top", "X")},
                                   {"level_index": True}))
def test_malformed_typed_zone_fails_closed(change):
    demand, zone, solid, constraints = case()
    with pytest.raises(ValueError):
        fit((demand, replace(zone, **change), solid, constraints))


@pytest.mark.parametrize("level", (-1, 99, True))
def test_unknown_source_fe_is_not_silently_ignored(level):
    demand, zone, solid, constraints = case()
    demand = replace(demand, cells=(replace(demand.cells[0], level_index=level), *demand.cells[1:]))
    with pytest.raises(ValueError):
        fit((demand, zone, solid, constraints))
