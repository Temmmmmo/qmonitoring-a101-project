"""No-demand removal, stale geometry and self-recertification never pass as repair."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from shapely.geometry import box

from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.contracts.opening_relocation import SourceServiceLane
from rebar.optimization.contracts.physical import PhysicalBar, PhysicalSourceBar
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS, PlateProblem
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutProblem
from rebar.optimization.services.fe_host_repair import (
    check_fe_host_repair, derive_fe_obligations, installed_fe_coverage,
)
from rebar.optimization.services.opening_relocation import lane_map
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection


def case(axis=Axis.X):
    direction = Direction(Layer.TOP, axis)
    source = PhysicalSourceBar("zone/0/0", direction, "A500", 10, 100,
        (-400, 11300), (0, 10800), 10, 0)
    bar = PhysicalBar("physical", direction, "A500", 10, 100, (-400, 11300), (source.id,))
    lane = SourceServiceLane(source, "zone", 0, 0, 300, (-50, 250), (150, 150))
    polygon = box(1000, 0, 1800, 200) if axis is Axis.X else box(0, 1000, 200, 1800)
    recipe = ReinforcementRecipe(Rebar(300, 10), (Rebar(300, 10),))
    levels = (DemandLevel(0, 1, 0, 1, "background", None, False, replace(recipe, additions=())),
              DemandLevel(1, 2, 1, 2, "addition", recipe.additions[0], True, recipe))
    problems = tuple(LayoutProblem(DemandMap(d, levels, (DemandCell(1, tuple(polygon.exterior.coords)[:-1],
        (polygon.centroid.x, polygon.centroid.y), 2 if d == direction else 1, int(d == direction)),),
        polygon.bounds)) for d in PLATE_DIRECTIONS)
    material = box(0, -1000, 15000, 5000) if axis is Axis.X else box(-1000, 0, 5000, 15000)
    host = OrthogonalSolidHost((SolidHostSection(0, 200, material),), 25, 25, 25, material.area*200, 6)
    return (bar,), (lane,), PlateProblem(direction_problems=problems, case_id="synthetic"), host


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_freezes_actual_FE_instead_of_whole_old_zone_and_keeps_full_party(axis):
    bars, lanes, problem, host = case(axis)
    original = deepcopy((bars, lanes, problem))
    obligations = derive_fe_obligations(bars, lanes, problem)
    value = obligations[(bars[0].direction, bars[0].id)]
    assert value["required_interval_mm"] == (1000, 1800)
    assert value["original_required_interval_mm"] == (0, 10800)
    assert value["served_cell_ids"] == (1,)
    candidate = (replace(bars[0], installed_interval_mm=(25, 11725)),)
    checked = check_fe_host_repair(bars, candidate, lanes, problem, host)
    assert checked["host_blocked_before"] == 1 and checked["host_blocked_after"] == 0
    assert checked["source_coverage"]["status"] == checked["stock_cutting"]["status"] == "pass"
    assert checked["cutting_inventory_unchanged"] and checked["physical_bar_count"] == 1
    assert not checked["legacy_source_certificate_reused"] and not checked["placement_eligible"]
    assert "rectangular_LayoutZone_regrouping_and_minimum_width" in checked["not_checked"]
    assert (bars, lanes, problem) == original


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_lost_FE_cannot_disappear_by_recomputing_obligation_after_shift(axis):
    bars, lanes, problem, host = case(axis)
    proposed = (replace(bars[0], installed_interval_mm=(1500, 13200)),)
    with pytest.raises(ValueError, match="Frozen original FE"):
        check_fe_host_repair(bars, proposed, lanes, problem, host)
    actual = installed_fe_coverage(problem, proposed, lane_map(lanes))
    assert actual["status"] == "fail" and actual["uncovered_cell_count"] == 1


@pytest.mark.parametrize("change", ("missing", "duplicate", "axis", "diameter", "owners", "nan", "shorten", "still_outside"))
def test_rejects_partial_or_unchecked_changes(change):
    bars, lanes, problem, host = case()
    candidate = replace(bars[0], installed_interval_mm=(25, 11725))
    proposed = (candidate,)
    if change == "missing":
        proposed = ()
    elif change == "duplicate":
        proposed = (candidate, candidate)
    elif change == "axis":
        proposed = (replace(candidate, transverse_axis_mm=101),)
    elif change == "diameter":
        proposed = (replace(candidate, diameter_mm=12),)
    elif change == "owners":
        proposed = (replace(candidate, source_bar_ids=("new",)),)
    elif change == "nan":
        proposed = (replace(candidate, installed_interval_mm=(float("nan"), 11725)),)
    elif change == "shorten":
        proposed = (replace(candidate, installed_interval_mm=(25, 3925)),)
    elif change == "still_outside":
        proposed = (replace(candidate, installed_interval_mm=(-200, 11500)),)
    with pytest.raises(ValueError):
        check_fe_host_repair(bars, proposed, lanes, problem, host)


def test_background_only_bar_is_not_automatically_removed_or_reassigned():
    bars, lanes, problem, host = case()
    problems = tuple(replace(p, demand=replace(p.demand, cells=tuple(replace(c, level_index=0)
        for c in p.demand.cells))) for p in problem.direction_problems)
    problem = replace(problem, direction_problems=problems)
    obligations = derive_fe_obligations(bars, lanes, problem)
    assert obligations[(bars[0].direction, bars[0].id)]["required_interval_mm"] is None
    with pytest.raises(ValueError, match="No-demand source bars"):
        check_fe_host_repair(bars, (replace(bars[0], installed_interval_mm=(25, 11725)),), lanes, problem, host)


def test_installed_core_not_legacy_bbox_controls_coverage():
    bars, lanes, problem, _ = case()
    short = replace(bars[0], installed_interval_mm=(1000, 1800))
    assert installed_fe_coverage(problem, (short,), lane_map(lanes))["status"] == "fail"


def test_baseline_lost_demand_or_nonfinite_axis_is_rejected():
    bars, lanes, problem, _ = case()
    with pytest.raises(ValueError):
        derive_fe_obligations((replace(bars[0], transverse_axis_mm=float("nan")),), lanes, problem)
    changed_problems = tuple(replace(p, demand=replace(p.demand, cells=tuple(
        replace(c, poly=tuple((x+20000, y) for x, y in c.poly)) for c in p.demand.cells)))
        for p in problem.direction_problems)
    with pytest.raises(ValueError, match="full FE coverage"):
        derive_fe_obligations(bars, lanes, replace(problem, direction_problems=changed_problems))


def test_merged_owner_cannot_lend_its_stronger_recipe_to_another_longitudinal_region():
    bars, lanes, problem, _ = case()
    strong = replace(lanes[0].source, id="strong/0/0", diameter_mm=12,
                     required_interval_mm=(1000, 1800), installed_interval_mm=(520, 2280))
    strong_lane = SourceServiceLane(strong, "strong", 0, 0, 150, (-50, 250), (100, 50))
    physical = replace(bars[0], diameter_mm=12, installed_interval_mm=(-480, 11220),
                       source_bar_ids=(lanes[0].source.id, strong.id))
    sources = lane_map((*lanes, strong_lane))
    strong_recipe = ReinforcementRecipe(Rebar(300, 10), (Rebar(150, 12),))
    modified = []
    for p in problem.direction_problems:
        strong_level = replace(p.demand.levels[1], recipe=strong_recipe, additional=strong_recipe.additions[0])
        cells = tuple(replace(c, poly=((5000, 50), (5500, 50), (5500, 125), (5000, 125))) for c in p.demand.cells)
        modified.append(replace(p, demand=replace(p.demand, levels=(p.demand.levels[0], strong_level), cells=cells)))
    demand = replace(problem, direction_problems=tuple(modified))
    assert installed_fe_coverage(demand, (physical,), sources)["status"] == "fail"


def test_every_positive_FE_fragment_is_preserved_not_just_its_centroid_or_largest_piece():
    bars, lanes, problem, _ = case()
    p = problem.problem(bars[0].direction)
    first = p.demand.cells[0]
    # Separate high-coordinate fragment has tiny area but is still original demand.
    second = replace(first, id=2, poly=((9000, 0), (9000.01, 0), (9000.01, 1), (9000, 1)))
    updated = replace(p, demand=replace(p.demand, cells=(first, second)))
    problem = replace(problem, direction_problems=tuple(updated if q.demand.direction == bars[0].direction else q
                                                        for q in problem.direction_problems))
    obligation = derive_fe_obligations(bars, lanes, problem)[(bars[0].direction, bars[0].id)]
    assert obligation["required_interval_mm"] == (1000, 9000.01)
    assert obligation["served_cell_ids"] == (1, 2)


def test_untyped_partial_plate_cannot_bypass_full_demand_contract():
    bars, lanes, problem, _ = case()
    partial = SimpleNamespace(direction_problems=(problem.problem(bars[0].direction),))
    with pytest.raises(ValueError, match="four-direction"):
        derive_fe_obligations(bars, lanes, partial)


@pytest.mark.parametrize("change", ("diameter_nan", "ends_nan", "axis_nan", "bool_diameter", "string_direction"))
def test_standalone_installed_coverage_rejects_nonfinite_physical_data(change):
    bars, lanes, problem, _ = case()
    fields = {"diameter_nan": {"diameter_mm": float("nan")},
              "ends_nan": {"installed_interval_mm": (float("nan"), float("nan"))},
              "axis_nan": {"transverse_axis_mm": float("nan")},
              "bool_diameter": {"diameter_mm": True},
              "string_direction": {"direction": "top-X"}}
    with pytest.raises(ValueError):
        installed_fe_coverage(problem, (replace(bars[0], **fields[change]),), lane_map(lanes))


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_solver_result_has_to_pass_fresh_full_checker_not_legacy_zone_certificate(axis):
    from rebar.optimization.algorithms.fe_host_repair import solve_fe_host_repair

    bars, lanes, problem, host = case(axis)
    before = deepcopy((bars, lanes, problem))
    obligations = derive_fe_obligations(bars, lanes, problem)
    result, search = solve_fe_host_repair(bars, obligations, host)
    assert search["status"] == "checked_finite_pool_solution"
    checked = check_fe_host_repair(bars, result, lanes, problem, host)
    assert checked["host_blocked_after"] == 0
    assert checked["source_coverage"]["uncovered_cell_count"] == 0
    assert checked["stock_cutting"]["status"] == "pass"
    assert checked["status"] == "research_checks_passed_not_placement_approved"
    assert not checked["structural_placement_supported"] and not checked["legacy_source_certificate_reused"]
    assert (bars, lanes, problem) == before


def test_preexisting_collision_is_not_permission_to_shift_a_bar_into_that_pair():
    bars, lanes, problem, host = case()
    source = replace(lanes[0].source, id="other/0/0")
    other = replace(bars[0], id="other", source_bar_ids=(source.id,))
    lane = replace(lanes[0], source=source, zone_id="other")
    before = (*bars, other)
    checked = check_fe_host_repair(before, before, (*lanes, lane), problem, host)
    assert checked["same_direction_body_pairs_before"] == 1
    with pytest.raises(ValueError, match="New or changed-bar"):
        check_fe_host_repair(before, (replace(bars[0], installed_interval_mm=(25, 11725)), other),
                            (*lanes, lane), problem, host)
