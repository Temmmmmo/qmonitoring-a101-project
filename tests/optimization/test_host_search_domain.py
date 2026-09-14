"""Any-axis host upper bounds preserve the entire input and closed degeneracies."""
from copy import deepcopy
from dataclasses import replace

import pytest
from shapely.affinity import affine_transform
from shapely.geometry import Polygon, box, shape

from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutProblem
from rebar.optimization.services.detailing import build_zone_from_bbox
from rebar.optimization.services.host_search_domain import (
    HostSearchDomainLimitError, optimistic_host_reachability, prepare_uniform_zone_host_guard,
    recipe_service_half_width_mm, uniform_zone_host_check,
)
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection


def problem(cells=(box(500, 500, 600, 600),), specs=(Rebar(300, 10),), axis=Axis.X):
    background = Rebar(300, 10)
    recipes = [ReinforcementRecipe(background), *(ReinforcementRecipe(background, (spec,)) for spec in specs)]
    levels = tuple(DemandLevel(index, index+1, None, None, "explicit", recipe.additions[0] if recipe.additions else None,
                              bool(recipe.additions), recipe) for index, recipe in enumerate(recipes))
    polygons = tuple(affine_transform(p, (0, 1, 1, 0, 0, 0)) if axis is Axis.Y else p for p in cells)
    rows = tuple(DemandCell(index+101, tuple(p.exterior.coords)[:-1], (p.centroid.x, p.centroid.y), 2, 1)
                 for index, p in enumerate(polygons))
    return LayoutProblem(DemandMap(Direction(Layer.TOP, axis), levels, rows, (0, 0, 2000, 2000)))


def host(footprint=None, axis=Axis.X):
    footprint = box(0, 0, 2000, 2000) if footprint is None else footprint
    if axis is Axis.Y:
        footprint = affine_transform(footprint, (0, 1, 1, 0, 0, 0))
    return OrthogonalSolidHost((SolidHostSection(0, 200, footprint),), 25, 25, 25,
                              footprint.area*200, 6)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_rectangle_bound_is_40d_along_and_radius_cover_minus_service_across(axis):
    original = problem(axis=axis)
    solid = host(axis=axis)
    untouched = deepcopy((original, solid))
    report = optimistic_host_reachability(original, solid)
    axes = shape(report["recipes"][0]["axis_domain"])
    serving = shape(report["recipes"][0]["serving_domain"])
    expected = box(425, 30, 1575, 1970)
    expected_serving = box(425, -120, 1575, 2120)
    if axis is Axis.Y:
        expected = affine_transform(expected, (0, 1, 1, 0, 0, 0))
        expected_serving = affine_transform(expected_serving, (0, 1, 1, 0, 0, 0))
    assert axes.equals(expected)
    assert serving.equals(expected_serving)
    assert report["status"] == "not_ruled_out"
    assert report["unreachable_cell_count"] == 0
    assert not report["placement_eligible"] and not report["source_demand_removed"]
    assert (original, solid) == untouched


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_whole_original_edge_fe_not_its_center_is_checked_without_clipping(axis):
    original = problem(cells=(box(400, 500, 600, 600),), axis=axis)
    report = optimistic_host_reachability(original, host(axis=axis))
    assert report["status"] == "impossible_in_stated_model"
    assert report["cells"][0]["cell_id"] == 101
    assert report["cells"][0]["unreachable_area_mm2"] == 2500
    assert report["original_cell_count"] == report["demanded_cell_count"] == 1
    assert not report["source_demand_removed"]


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_interior_hole_and_outer_recess_are_not_filled_by_bbox_erosion(axis):
    p = box(0, 0, 4000, 2000).difference(box(1900, 500, 2100, 1500))
    p = p.difference(box(3000, 0, 3500, 600))
    original = problem(cells=(box(1950, 900, 2050, 1100), box(3150, 0, 3200, 200)), axis=axis)
    report = optimistic_host_reachability(original, host(p, axis))
    assert report["unreachable_cell_count"] == 2
    assert report["unreachable_area_mm2"] == 30000
    hole = box(1900, 500, 2100, 1500)
    if axis is Axis.Y:
        hole = affine_transform(hole, (0, 1, 1, 0, 0, 0))
    assert shape(report["recipes"][0]["axis_domain"]).intersection(hole).is_empty


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_thin_host_has_line_axis_domain_that_still_serves_positive_area(axis):
    original = problem(cells=(box(500, 0, 600, 60),), axis=axis)
    report = optimistic_host_reachability(original, host(box(0, 0, 2000, 60), axis))
    axes = shape(report["recipes"][0]["axis_domain"])
    assert axes.area == 0
    assert axes.length == 1150
    assert report["recipes"][0]["serving_domain_area_mm2"] == 345000
    assert report["status"] == "not_ruled_out"


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_zero_width_axis_corridor_between_rooms_is_not_lost(axis):
    footprint = box(0, 0, 1000, 1000).union(box(2000, 0, 3000, 1000)).union(box(1000, 470, 2000, 530))
    report = optimistic_host_reachability(problem(cells=(box(1450, 470, 1550, 530),), axis=axis), host(footprint, axis))
    assert report["status"] == "not_ruled_out"
    serving = shape(report["recipes"][0]["serving_domain"])
    assert serving.intersection(box(1450, 470, 1550, 530) if axis is Axis.X else box(470, 1450, 530, 1550)).area == 6000


@pytest.mark.parametrize("offset", (0.1, 0.2, 0.3, 0.4, 0.7, 12.34, 1.001, -0.2))
def test_decimal_contact_host_retains_single_axis_line(offset):
    report = optimistic_host_reachability(problem(cells=(box(500, offset, 600, offset+60),)),
                                         host(box(0, offset, 2000, offset+60)))
    assert report["status"] == "not_ruled_out"
    axes = shape(report["recipes"][0]["axis_domain"])
    assert axes.area < 1e-6 and axes.length >= 1150


def test_positive_submicron_along_axis_domain_is_not_collapsed_to_a_point():
    epsilon = 0.0000005
    original = problem(cells=(box(425, 20, 425+epsilon, 40),))
    report = optimistic_host_reachability(original, host(box(0, 0, 850+epsilon, 60)))
    assert report["status"] == "not_ruled_out"
    assert shape(report["recipes"][0]["serving_domain"]).area > 0


def test_sto100_line_domain_serves55mm_not_only50mm():
    original = problem(cells=(box(500, 80, 600, 84),), specs=(Rebar(100, 10),))
    report = optimistic_host_reachability(original, host(box(0, 0, 2000, 60)))
    assert report["status"] == "not_ruled_out"
    assert report["recipes"][0]["optimistic_service_half_width_mm"] == 55


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_point_axis_domain_is_not_replaced_with_fake_positive_along_coverage(axis):
    report = optimistic_host_reachability(problem(cells=(box(424, 20, 426, 40),), axis=axis),
                                           host(box(0, 0, 850, 60), axis))
    assert shape(report["recipes"][0]["axis_domain"]).geom_type == "Point"
    assert shape(report["recipes"][0]["serving_domain"]).area == 0
    assert report["unreachable_area_mm2"] == 40


@pytest.mark.parametrize("step,expected", ((300, 150), (150, 100), (100, 55)))
def test_service_max_half_width_comes_from_actual_sto_pattern(step, expected):
    assert recipe_service_half_width_mm(ReinforcementRecipe(Rebar(300, 10), (Rebar(step, 10),))) == expected


def test_sto100_contact_delta_depends_on_both_diameters_not_nominal_half_step():
    assert recipe_service_half_width_mm(ReinforcementRecipe(Rebar(300, 18), (Rebar(100, 25),))) == 60.75


def test_recipe_union_is_sufficient_per_recipe_not_sum_of_weak_as_or_level_order():
    original = problem(specs=(Rebar(300, 12), Rebar(100, 10), Rebar(150, 12)))
    report = optimistic_host_reachability(original, host())
    assert report["cells"][0]["sufficient_recipe_indexes"] == [0, 2]
    assert report["recipe_policy"] == "one_sufficient_recipe_at_each_point_no_weak_As_summation"


def test_height_sections_use_explicit_conservative_intersection_not_largest_outline():
    base = host()
    solid = replace(base, sections=(SolidHostSection(0, 100, box(0, 0, 2000, 2000)),
                                   SolidHostSection(100, 200, box(0, 0, 1000, 2000))),
                    volume_mm3=600000000)
    report = optimistic_host_reachability(problem(cells=(box(1000, 500, 1100, 600),)), solid)
    assert report["status"] == "impossible_in_stated_model"
    assert report["material_area_mm2"] == 2000000
    assert "intersection_of_all_Z_sections" in report["host_policy"]


@pytest.mark.parametrize("level", (-1, 99, True))
def test_unknown_fe_levels_fail_closed(level):
    original = problem()
    original = replace(original, demand=replace(original.demand, cells=(replace(original.demand.cells[0], level_index=level),)))
    with pytest.raises(ValueError, match="levels"):
        optimistic_host_reachability(original, host())


def test_duplicate_real_ids_and_nan_fe_do_not_get_an_impossibility_certificate():
    original = problem()
    for cells in ((original.demand.cells[0], original.demand.cells[0]),
                  (replace(original.demand.cells[0], poly=((500, 500), (float("nan"), 500), (600, 600))),)):
        with pytest.raises(ValueError):
            optimistic_host_reachability(replace(original, demand=replace(original.demand, cells=cells)), host())


@pytest.mark.parametrize("step", (75, 200))
def test_unsupported_recipe_step_does_not_guess_serving_radius(step):
    with pytest.raises(ValueError, match="Unsupported"):
        optimistic_host_reachability(problem(specs=(Rebar(step, 10),)), host())


def test_nonorthogonal_host_is_not_replaced_by_bbox():
    with pytest.raises(ValueError, match="axis aligned"):
        optimistic_host_reachability(problem(), host(Polygon(((0, 0), (2000, 0), (1800, 2000), (0, 2000)))))


def test_explicit_geometry_limit_raises_instead_of_truncation_or_false_impossibility():
    with pytest.raises(HostSearchDomainLimitError, match="no truncation"):
        optimistic_host_reachability(problem(), host(), maximum_geometry_parts=1)


def zone_case(axis=Axis.X):
    original = problem(axis=axis)
    zone = build_zone_from_bbox(original, (500, 500, 600, 600), 1, "zone", collect_coverage=False)
    return original, zone


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_uniform_zone_guard_checks_actual_axes_and_preserves_inputs(axis):
    original, zone = zone_case(axis)
    untouched = deepcopy((original, zone))
    report = uniform_zone_host_check(original, zone, host(axis=axis))
    assert report["valid"]
    assert report["physical_bar_count_checked"] == zone.bar_count
    assert report["outside_host_area_sum_mm2"] == 0
    assert prepare_uniform_zone_host_guard(original, host(axis=axis))(zone)
    assert (original, zone) == untouched
    assert not report["placement_eligible"]


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_uniform_guard_rejects_hole_mid_bar_even_when_endpoints_inside(axis):
    original, zone = zone_case(axis)
    footprint = box(0, 0, 2000, 2000).difference(box(700, 390, 800, 410))
    solid = host(footprint, axis)
    report = uniform_zone_host_check(original, zone, solid)
    assert not report["valid"]
    assert report["host_blocked_bar_count"] == 1
    assert not prepare_uniform_zone_host_guard(original, solid)(zone)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_uniform_guard_does_not_fill_gaps_between_bars_with_fake_material(axis):
    original, zone = zone_case(axis)
    # Actual axes are400/700; this hole is between their radius+cover envelopes.
    footprint = box(0, 0, 2000, 2000).difference(box(700, 500, 800, 600))
    report = uniform_zone_host_check(original, zone, host(footprint, axis))
    assert report["valid"]


@pytest.mark.parametrize("change", ({"bar_count": True}, {"bar_count": 10}, {"first_bar_coordinate_mm": float("nan")},
                                   {"installed_length_mm": 100}, {"width_mm": 301}, {"anchored_length_mm": 899}))
def test_uniform_guard_rejects_metadata_that_could_hide_axes_or40d(change):
    original, zone = zone_case()
    with pytest.raises(ValueError):
        uniform_zone_host_check(original, replace(zone, **change), host())


def test_uniform_guard_bar_budget_is_not_silent_partial_check():
    original, zone = zone_case()
    with pytest.raises(ValueError):
        uniform_zone_host_check(original, zone, host(), maximum_bars=1)
