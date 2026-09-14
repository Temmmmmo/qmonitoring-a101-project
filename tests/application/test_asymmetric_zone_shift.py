"""Full-source joint zone translations, with no new collision identities."""
from copy import deepcopy
from dataclasses import replace
import importlib

import pytest
from shapely.affinity import affine_transform
from shapely.geometry import box

from rebar.application.analyze_composite_plate import CompositeDirectionSettings, _placements
from rebar.application.asymmetric_zone_shift import shift_composite_plate_to_host
from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.contracts import PLATE_DIRECTIONS, PlateProblem
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutConstraints, LayoutProblem
from rebar.optimization.services.axis_patterns import pattern_coordinates
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.composite_zone_translation import TransverseZoneTranslationCandidates
from rebar.optimization.services.cutting import PLATE_11700_BATCH_PROFILE, PLATE_11700_CUT_LENGTHS_MM
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection


def case(axis=Axis.X, layer=Layer.TOP, *, length=1950, along=False, footprint=None):
    chosen = Direction(layer, axis)
    polygon = box(0, 0, 3900, 300) if along else box(1000, 0, 2000, 300)
    bounds = (0, 0, 3900, 600) if along else (1000, -150, 2000, 450)
    footprint = (box(-800, -200, 6000, 1200) if along else box(0, 0, 4000, 900)) if footprint is None else footprint
    diameter = 18 if along else 10
    length = 5850 if along else length
    if axis is Axis.Y:
        polygon = affine_transform(polygon, (0, 1, 1, 0, 0, 0))
        footprint = affine_transform(footprint, (0, 1, 1, 0, 0, 0))
        bounds = bounds[1], bounds[0], bounds[3], bounds[2]
    recipe = ReinforcementRecipe(Rebar(300, diameter), (Rebar(150, diameter),))
    levels = (DemandLevel(0, 1, 0, 1, "background", None, False, replace(recipe, additions=())),
              DemandLevel(1, 2, 1, 2, "addition", recipe.additions[0], True, recipe))
    constraints = LayoutConstraints(cutting_profile="plate-11700", allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM)
    batch = replace(constraints, cutting_profile=PLATE_11700_BATCH_PROFILE)
    problems, groups, settings = [], [], []
    for direction in PLATE_DIRECTIONS:
        cell = DemandCell(42, tuple(polygon.exterior.coords)[:-1], (polygon.centroid.x, polygon.centroid.y),
                          2 if direction == chosen else 1, int(direction == chosen))
        demand = DemandMap(direction, levels, (cell,), polygon.bounds, source_path=f"{direction}.dxf")
        original = LayoutProblem(demand, constraints)
        config = CompositeDirectionSettings(direction, 0, 100, 0, "A500", "explicit synthetic source")
        zones = (build_composite_zone(demand, bounds, 1, "original-zone", dict(_placements(demand, config))[1],
                    constraints=batch, installed_lengths_mm=(length,)),) if direction == chosen else ()
        problems.append(original)
        groups.append(zones)
        settings.append(config)
    host = OrthogonalSolidHost((SolidHostSection(0, 200, footprint),), 25, 25, 25, footprint.area*200, 6)
    return PlateProblem(tuple(problems), case_id="synthetic-explicit"), tuple(groups), tuple(settings), host


def _selected(groups):
    return next(zone for group in groups for zone in group)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
@pytest.mark.parametrize("layer", (Layer.TOP, Layer.BOTTOM))
def test_full_source_transverse_improvement_keeps_FE_mass_count_length_and_global_phases(axis, layer):
    data = case(axis, layer)
    frozen = deepcopy(data)
    result = shift_composite_plate_to_host(*data)
    report = result.report
    before, after = _selected(data[1]), _selected(result.direction_zones)
    assert report["status"] == "improved"
    assert report["host_before"]["blocked_bar_count"] == 1
    assert report["host_after"]["blocked_bar_count"] == 0
    assert report["metrics_before"] == report["metrics_after"] == {
        "zone_count": 1, "position_count": 1, "physical_bar_count": 4, "additional_mass_kg": pytest.approx(4.8087)}
    assert pattern_coordinates(before.components[0].placement, before.components[0].axis_window_mm) == (-100, 100, 200, 400)
    assert pattern_coordinates(after.components[0].placement, after.components[0].axis_window_mm) == (100, 200, 400, 500)
    assert before.placement == after.placement
    assert before.components[0].longitudinal_interval_mm == after.components[0].longitudinal_interval_mm
    # Selection-window bounds need not be inside concrete; physical bar bodies do.
    assert after.demand_bbox[1 if axis is Axis.X else 0] < 0
    assert all(row["status"] == "pass" for row in report["coverage_before"]+report["coverage_after"])
    assert report["new_same_direction_pair_count"] == report["source_bars_removed"] == 0
    assert report["stock_inventory_identical"] and report["stock_before"]["status"] == report["stock_after"]["status"] == "fail"
    assert not report["placement_eligible"] and not report["global_infeasibility_proven"]
    assert (data[0], data[1], data[2], data[3]) == frozen


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_along_only_shared_stock_slack_shift_is_checked_with_batch_constraints(axis):
    data = case(axis=axis, along=True)
    result = shift_composite_plate_to_host(*data, maximum_transverse_shift_mm=0)
    assert result.report["status"] == "improved"
    assert result.report["host_before"]["blocked_bar_count"] == 4
    assert result.report["host_after"]["blocked_bar_count"] == 0
    change = result.report["changed_zones"][0]
    assert change["longitudinal_shift_mm"] == pytest.approx(200)
    assert change["transverse_window_shift_mm"] == 0
    assert change["components"][0]["installed_interval_after_mm"] == pytest.approx((-775, 5075))
    assert result.report["bar_schedule_before"] == result.report["bar_schedule_after"]


def test_stock_pass_is_preserved_for_the_actual_whole_inventory():
    pytest.importorskip("scipy")
    result = shift_composite_plate_to_host(*case(length=2925))
    assert result.report["host_after"]["blocked_bar_count"] == 0
    assert result.report["stock_before"]["status"] == result.report["stock_after"]["status"] == "pass"
    assert result.report["stock_after"]["physical_bar_count"] == 4
    assert result.report["stock_after"]["waste_mm"] == 0
    assert not result.report["placement_eligible"]


def test_new_same_direction_collision_is_rejected_even_if_host_failure_would_fall():
    problem, groups, settings, host = case(footprint=box(0, 0, 3000, 1200))
    index = PLATE_DIRECTIONS.index(Direction(Layer.TOP, Axis.X))
    original = groups[index][0]
    second = build_composite_zone(problem.direction_problems[index].demand, (1000, 450, 2000, 1050), 1,
        "original-neighbor", original.placement,
        constraints=replace(problem.direction_problems[index].constraints, cutting_profile=PLATE_11700_BATCH_PROFILE),
        installed_lengths_mm=(1950,))
    groups = (*groups[:index], (original, second), *groups[index+1:])
    result = shift_composite_plate_to_host(problem, groups, settings, host)
    assert result.direction_zones == groups
    assert result.report["host_before"]["blocked_bar_count"] == result.report["host_after"]["blocked_bar_count"] == 1
    assert result.report["collisions_before"]["pair_count"] == result.report["collisions_after"]["pair_count"] == 0
    assert result.report["metrics_after"]["physical_bar_count"] == 8
    assert any(row["reason"] == "new_same_direction_body_pairs" for row in result.report["rejected_proposals"])


def test_true_edge_failure_retains_every_original_source_and_demand():
    data = case(along=True, footprint=box(-700, -200, 6000, 1200))
    result = shift_composite_plate_to_host(*data, maximum_transverse_shift_mm=0)
    assert result.direction_zones == data[1]
    assert result.report["status"] == "unchanged"
    assert result.report["host_after"]["blocked_bar_count"] == 4
    assert not result.report["global_infeasibility_proven"]
    assert result.report["coverage_after"][2]["demanded_cell_count"] == 1


def test_same_pair_ids_cannot_hide_more_longitudinal_overlap_after_host_improvement():
    problem, groups, settings, host = case(along=True, footprint=box(-800, -200, 8000, 1200))
    index = 2
    original = groups[index][0]
    second = build_composite_zone(problem.direction_problems[index].demand, (2000, 0, 5900, 600), 1,
        "original-neighbor", original.placement,
        constraints=replace(problem.direction_problems[index].constraints, cutting_profile=PLATE_11700_BATCH_PROFILE),
        installed_lengths_mm=(5850,))
    groups = (*groups[:index], (original, second), *groups[index+1:])
    result = shift_composite_plate_to_host(problem, groups, settings, host, maximum_transverse_shift_mm=0)
    assert result.direction_zones == groups
    assert result.report["host_before"]["blocked_bar_count"] == result.report["host_after"]["blocked_bar_count"] == 4
    assert result.report["collisions_before"]["pair_count"] == result.report["collisions_after"]["pair_count"] == 4
    assert any(row["reason"] == "existing_same_direction_collision_worsened"
               for row in result.report["rejected_proposals"])
    assert result.report["worsened_existing_collision_footprint_count"] == 0


def test_collision_footprint_reduction_is_allowed_but_same_area_in_new_place_is_not():
    module = importlib.import_module("rebar.application.asymmetric_zone_shift")
    key = (("top-X", "a", 0, 0), ("top-X", "b", 0, 0))
    before = {key: box(0, 0, 100, 10)}
    assert not module._worsened_collisions(before, {key: box(10, 0, 90, 10)})
    assert module._worsened_collisions(before, {key: box(10, 0, 110, 10)}) == (key,)
    assert module._worsened_collisions(before, {key: box(0, 1, 100, 11)}) == (key,)


def test_all_four_nonempty_directions_are_preserved_and_counted_as_source_bars():
    problem, _groups, settings, host = case(footprint=box(-2000, -2000, 4000, 4000))
    polygons = tuple(box(x, y, x+600, y+600) for x in (0, 600) for y in (0, 600))
    problems, groups = [], []
    for original, config in zip(problem.direction_problems, settings, strict=True):
        cells = tuple(DemandCell(index, tuple(poly.exterior.coords)[:-1], (poly.centroid.x, poly.centroid.y), 2, 1)
                      for index, poly in enumerate(polygons))
        demand = replace(original.demand, cells=cells, bbox=(0, 0, 1200, 1200))
        original = replace(original, demand=demand)
        zone = build_composite_zone(demand, demand.bbox, 1, "same-id-distinct-direction",
            dict(_placements(demand, config))[1],
            constraints=replace(original.constraints, cutting_profile=PLATE_11700_BATCH_PROFILE),
            installed_lengths_mm=(2925,))
        problems.append(original)
        groups.append((zone,))
    result = shift_composite_plate_to_host(PlateProblem(tuple(problems)), tuple(groups), settings, host)
    assert result.direction_zones == tuple(groups)
    assert result.report["metrics_after"]["zone_count"] == 4
    assert result.report["metrics_after"]["physical_bar_count"] == 32
    assert len(result.report["host_after"]["by_direction"]) == 4
    assert sum(row["demanded_cell_count"] for row in result.report["coverage_after"]) == 16
    assert result.report["collisions_after"]["pair_count"] == 0


def test_every_solid_section_is_used_no_bbox_or_top_face_fallback():
    problem, groups, settings, host = case()
    narrower = box(0, 50, 4000, 900)
    sections = (host.sections[0], SolidHostSection(200, 300, narrower))
    host = replace(host, sections=sections, volume_mm3=host.volume_mm3+narrower.area*100)
    result = shift_composite_plate_to_host(problem, groups, settings, host)
    assert result.report["host_after"]["checked_section_indexes"] == (0, 1)
    assert not result.report["host_after"]["actual_z_checked"]


@pytest.mark.parametrize("kwargs", [
    {"maximum_transverse_shift_mm": -1}, {"maximum_transverse_shift_mm": True},
    {"maximum_transverse_shift_mm": float("nan")}, {"maximum_transverse_shift_mm": float("inf")},
    {"maximum_transverse_shift_mm": 1201}, {"maximum_candidates_per_zone": 0},
    {"maximum_candidates_per_zone": True}, {"maximum_candidates_per_zone": 129},
    {"maximum_passes": 0}, {"maximum_passes": True}, {"maximum_passes": 9},
])
def test_resource_limits_are_validated_before_search(kwargs):
    with pytest.raises(ValueError):
        shift_composite_plate_to_host(*case(), **kwargs)


def test_empty_source_direction_is_retained_but_missing_direction_is_rejected():
    problem, groups, settings, host = case()
    assert len(shift_composite_plate_to_host(problem, groups, settings, host).direction_zones) == 4
    with pytest.raises(ValueError, match="four-direction"):
        shift_composite_plate_to_host(problem, groups[:3], settings, host)
    with pytest.raises(ValueError, match="четырёх"):
        shift_composite_plate_to_host(problem, groups, settings[:3], host)


def test_invalid_phase_settings_are_not_silently_ignored():
    problem, groups, settings, host = case()
    configs = tuple(replace(item, background_origin_mm=100) if item.direction == Direction(Layer.TOP, Axis.X)
                    else item for item in settings)
    with pytest.raises(ValueError, match="phases/profile"):
        shift_composite_plate_to_host(problem, groups, configs, host)


def test_uncovered_original_FE_is_an_error_not_partial_output():
    problem, _groups, settings, host = case()
    with pytest.raises(ValueError, match="Full original FE"):
        shift_composite_plate_to_host(problem, ((), (), (), ()), settings, host)


def test_duplicate_zone_identity_is_rejected():
    problem, groups, settings, host = case()
    index = 2
    groups = (*groups[:index], groups[index]*2, *groups[index+1:])
    with pytest.raises(ValueError):
        shift_composite_plate_to_host(problem, groups, settings, host)


def test_generation_limit_is_visible_and_along_only_preserves_all_sources(monkeypatch):
    module = importlib.import_module("rebar.application.asymmetric_zone_shift")
    def limited(*_args, **_kwargs):
        raise ValueError("Transverse event budget exceeded; no events were silently dropped")
    monkeypatch.setattr(module, "propose_transverse_zone_translations", limited)
    data = case(along=True)
    result = shift_composite_plate_to_host(*data)
    assert result.report["host_after"]["blocked_bar_count"] == 0
    assert not result.report["search_completed_without_errors"]
    assert all(row["fallback"] == "original_zone_along_only" for row in result.report["candidate_pools"])
    assert result.report["source_bars_removed"] == 0


def test_candidate_truncation_is_reported_not_global_infeasibility():
    result = shift_composite_plate_to_host(*case(), maximum_candidates_per_zone=1)
    assert result.report["candidate_pool_truncated"]
    assert result.report["host_after"]["blocked_bar_count"] == 1
    assert not result.report["global_infeasibility_proven"]


def test_malicious_inventory_changing_proposal_is_not_accepted(monkeypatch):
    module = importlib.import_module("rebar.application.asymmetric_zone_shift")
    original_fit = module.fit_composite_zone_to_solid_host
    def bad_fit(*args, **kwargs):
        fitted = original_fit(*args, **kwargs)
        return replace(fitted, fitted_zone=replace(fitted.fitted_zone, id="invented-new-source"))
    monkeypatch.setattr(module, "fit_composite_zone_to_solid_host", bad_fit)
    data = case()
    result = shift_composite_plate_to_host(*data)
    assert result.direction_zones == data[1]
    assert any(row["reason"] == "proposal_validation_failed" for row in result.report["rejected_proposals"])


def test_full_plate_coverage_recheck_failure_retains_original_not_best_unchecked_move(monkeypatch):
    module = importlib.import_module("rebar.application.asymmetric_zone_shift")
    original = module.evaluate_composite_coverage
    def reject_moved(demand, zones, **kwargs):
        checked = original(demand, zones, **kwargs)
        if zones and zones[0].demand_bbox[1] != -150:
            return replace(checked, status="fail", coverage_passed=False)
        return checked
    monkeypatch.setattr(module, "evaluate_composite_coverage", reject_moved)
    data = case()
    result = shift_composite_plate_to_host(*data)
    assert result.direction_zones == data[1]
    assert any(row["reason"] == "full_plate_recheck_failed" for row in result.report["rejected_proposals"])


def test_cumulative_transverse_cap_applies_to_original_source_not_each_pass(monkeypatch):
    module = importlib.import_module("rebar.application.asymmetric_zone_shift")
    original = module.propose_transverse_zone_translations
    calls = []
    def tracked(demand, zone, **kwargs):
        calls.append(zone.demand_bbox)
        return original(demand, zone, **kwargs)
    monkeypatch.setattr(module, "propose_transverse_zone_translations", tracked)
    data = case()
    result = shift_composite_plate_to_host(*data, maximum_transverse_shift_mm=60, maximum_passes=8)
    assert len(calls) >= 2
    assert all(abs(row["transverse_window_shift_mm"]) <= 60 for row in result.report["changed_zones"])


def test_exact_fit_cache_avoids_repeated_identical_geometry_checks(monkeypatch):
    module = importlib.import_module("rebar.application.asymmetric_zone_shift")
    generate = module.propose_transverse_zone_translations
    def duplicated(*args, **kwargs):
        pool = generate(*args, **kwargs)
        return TransverseZoneTranslationCandidates(pool.candidates*2, pool.evaluated_event_count,
                                                   pool.accepted_before_limit, pool.truncated)
    monkeypatch.setattr(module, "propose_transverse_zone_translations", duplicated)
    result = shift_composite_plate_to_host(*case())
    assert result.report["distinct_exact_fit_calls"] < result.report["proposal_count"]
