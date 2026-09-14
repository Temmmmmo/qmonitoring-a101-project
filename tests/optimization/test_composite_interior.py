from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from rebar.models import Axis, Direction, Layer
from rebar.optimization.algorithms.composite_pool import solve_composite_pool
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.services.composite_host import evaluate_composite_host
from rebar.optimization.services.composite_interior import admissible_composite_bboxes, partition_interior_demand
from rebar.optimization.services.composite_windows import covering_composite_window
from rebar.optimization.services.detailing import typical_transverse_cell_size_mm
from rebar.optimization.services.finite_cover import solve_finite_cover_front

from test_composite_host import host_sample
from test_composite_search import problem_sample


def interior_sample(axis=Axis.X, layer=Layer.TOP):
    problem = problem_sample(axis)
    demand = replace(problem.demand, direction=Direction(layer, axis))
    # One original FE at the edge, two inside. Never remove it from the validator.
    poly = ((-2000, 0), (-1600, 0), (-1600, 400), (-2000, 400))
    if axis is Axis.Y:
        poly = tuple((y, x) for x, y in poly)
    edge = replace(demand.cells[0], id=999, poly=poly, centroid=poly[0])
    return replace(problem, demand=replace(demand, cells=(*demand.cells, edge)),
                   host_envelope=host_sample(), boundary_mode="interior-exceptions")


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
@pytest.mark.parametrize("layer", [Layer.TOP, Layer.BOTTOM])
def test_interior_keeps_original_cells_area_and_width_scale(axis, layer):
    problem = interior_sample(axis, layer)
    before = deepcopy(problem)
    partition = partition_interior_demand(problem)
    assert partition["original_demanded_cell_count"] == 3
    assert partition["target_cell_count"] == 2 and partition["boundary_exception_cell_count"] == 1
    assert partition["boundary_exceptions"][0]["cell_id"] == 999
    assert partition["target_area_mm2"] + partition["boundary_exception_area_mm2"] == pytest.approx(
        partition["original_demanded_area_mm2"])
    assert not partition["source_demand_modified"] and problem == before
    assert typical_transverse_cell_size_mm(problem.demand) == 400
    bounds = partition["admissible_bboxes_by_level_mm"]
    along, across = (0, 1) if axis is Axis.X else (1, 0)
    assert bounds[1][along] == -2000 + 25 + 40 * 18
    assert bounds[2][along] == -2000 + 25 + 40 * 25
    assert bounds[2][across] < -1700  # transverse margin is NOT 40d
    assert partition["common_target_bbox_mm"][along] == bounds[2][along]


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
def test_partial_solution_cannot_turn_excluded_cells_into_mass_savings_or_full_pass(axis):
    pytest.importorskip("scipy")
    problem = interior_sample(axis)
    result = solve_composite_pool(problem, maximum_candidates=16, maximum_zones=4)
    assert result.points and not result.placement_eligible
    for point in result.points:
        check = evaluate_composite_coverage(problem.demand, point.zones, policy_id=problem.policy_id)
        assert check == point.coverage and check.status == "fail"
        assert check.uncovered_cell_count == 1 and check.uncovered_area_mm2 == 160000
        assert check.additional_mass_kg > 0 and check.physical_bar_count > 0
        assert evaluate_composite_host(problem.demand, point.zones, problem.host_envelope)["checks"]["planar_host_and_openings"] == "pass"


def test_empty_interior_is_no_solution_not_zero_mass_front():
    problem = interior_sample()
    problem = replace(problem, demand=replace(problem.demand, cells=(problem.demand.cells[-1],)))
    result = solve_composite_pool(problem)
    assert not result.points and result.selected_index is None and not result.telemetry["solver_executed"]
    assert result.telemetry["interior_partition"]["boundary_exception_cell_count"] == 1


def test_openings_are_not_hidden_by_the_outer_inset():
    problem = replace(interior_sample(), host_envelope=host_sample(openings_mm=((0, -100, 3900, 1000),)))
    result = solve_composite_pool(problem, maximum_candidates=16)
    assert not result.points and result.telemetry["host_rejected_candidates"] > 0
    assert result.telemetry["interior_partition"]["target_cell_count"] == 2


@pytest.mark.parametrize("box", [(0, 0, 0, 10), (0, 0, 10, float("nan")), (0, 0, 100, 50)])
def test_restricted_window_cannot_shorten_required_geometry(box):
    problem = problem_sample()
    with pytest.raises(ValueError):
        covering_composite_window(problem.demand, problem.demand.bbox, 2, dict(problem.placements)[2],
                                  constraints=problem.constraints, admissible_bbox=box)


def test_narrow_host_has_no_admissible_rectangle_and_missing_host_fails():
    problem = replace(interior_sample(), host_envelope=host_sample(outer_mm=(0, 0, 1000, 1000)))
    assert all(box is None for box in admissible_composite_bboxes(problem).values())
    assert partition_interior_demand(problem)["common_target_bbox_mm"] is None
    with pytest.raises(ValueError, match="host"):
        solve_composite_pool(replace(problem, host_envelope=None))


def test_incompatible_candidates_are_prohibited_inside_milp():
    pytest.importorskip("scipy")
    covers = (frozenset({0}), frozenset({1}), frozenset({0, 1}))
    proposals, telemetry = solve_finite_cover_front(covers, (1, 1, 3), 2, maximum_zones=2,
        time_limit_s=5, budgets=(1, 2), incompatible_pairs=((0, 1),), bar_counts=(3, 3, 7))
    assert proposals == ((2,),) and telemetry["incompatible_pair_count"] == 1
    assert all(row["physical_bar_count"] == 7 for row in telemetry["solves"])


@pytest.mark.parametrize("options,expected", [({"maximum_physical_bars": 5}, ((2,),)),
    ({"maximum_mass_kg": 2.5}, ((0, 1),)), ({"maximum_physical_bars": 5, "maximum_mass_kg": 2.5}, ())])
def test_mass_and_actual_bars_have_separate_hard_budgets(options, expected):
    pytest.importorskip("scipy")
    proposals, _ = solve_finite_cover_front((frozenset({0}), frozenset({1}), frozenset({0, 1})), (1, 1, 3), 2,
        maximum_zones=2, time_limit_s=5, budgets=(2,), bar_counts=(3, 3, 5), **options)
    assert proposals == expected


@pytest.mark.parametrize("options", [{"incompatible_pairs": ((0, 1),)}, {"maximum_mass_kg": 1.5}, {"maximum_physical_bars": 1}])
def test_invalid_solver_incumbent_cannot_bypass_new_constraints(monkeypatch, options):
    np = pytest.importorskip("numpy")
    scipy = pytest.importorskip("scipy.optimize")
    monkeypatch.setattr(scipy, "milp", lambda *a, **kw: SimpleNamespace(status=0, x=np.array([1, 1])))
    proposals, telemetry = solve_finite_cover_front((frozenset({0}), frozenset({1})), (1, 1), 2,
        maximum_zones=2, time_limit_s=5, budgets=(2,), bar_counts=(1, 1), **options)
    assert not proposals and not telemetry["solves"][0]["accepted"]


@pytest.mark.parametrize("options", [{"maximum_physical_bars": True}, {"maximum_physical_bars": 0},
    {"maximum_physical_bars": 2.5}, {"maximum_mass_kg": float("nan")}, {"maximum_mass_kg": -1}, {"maximum_mass_kg": True},
    {"maximum_bar_length_mm": float("nan")}, {"maximum_bar_length_mm": 0}, {"maximum_bar_length_mm": True}])
def test_invalid_quality_limits_are_not_silently_ignored(options):
    with pytest.raises(ValueError):
        solve_composite_pool(interior_sample(), **options)


def test_budget_rechecked_after_independent_detailing(monkeypatch):
    monkeypatch.setattr("rebar.optimization.algorithms.composite_pool.solve_finite_cover_front",
                        lambda *a, **kw: (((0,),), {"solves": [{}]}))
    result = solve_composite_pool(interior_sample(), maximum_physical_bars=1, maximum_candidates=16)
    assert not result.points and result.telemetry["rejected_proposals"] == 1


def test_bar_length_limit_does_not_fabricate_splices_or_shorter_bars():
    problem = interior_sample()
    result = solve_composite_pool(problem, maximum_bar_length_mm=5800, maximum_candidates=16)
    assert not result.points and result.telemetry["bar_length_rejected_candidates"] > 0
    assert not result.placement_eligible


@pytest.mark.parametrize("options", [{"incompatible_pairs": ((0, 0),)}, {"incompatible_pairs": ((0, 2),)},
    {"incompatible_pairs": ((0, True),)}, {"bar_counts": (True,)}, {"bar_counts": ()}, {"maximum_physical_bars": 1}])
def test_invalid_finite_model_constraints_fail(options):
    with pytest.raises(ValueError):
        solve_finite_cover_front((frozenset({0}),), (1,), 1, maximum_zones=1,
                                time_limit_s=1, budgets=(1,), **options)
