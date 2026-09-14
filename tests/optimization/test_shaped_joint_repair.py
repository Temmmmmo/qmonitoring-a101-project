from dataclasses import replace

import pytest
from shapely.geometry import box

from rebar.optimization.algorithms.shaped_joint_repair import _Candidate, _missing, propose_joint_shaped_repair
from rebar.optimization.services.shaped_global_coverage import check_shaped_global_repair
from rebar.optimization.services.solid_host import SolidHostSection
from test_shaped_global_coverage import _case, _shapes


def test_simultaneous_U_inventory_passes_independent_full_source_and_3D_check():
    pytest.importorskip("scipy.optimize")
    before, lanes, problem, host = _case(edge=True, duplicate=True, demand_bounds=(100, -50, 1500, 250))
    after, search = propose_joint_shaped_repair(before, lanes, problem, host, time_limit_s=20)
    report = check_shaped_global_repair(before, after, lanes, problem, host)
    assert search["after_score"][:2] == (0, 0)
    assert search["rounds"]
    assert report["source_coverage"]["uncovered_cell_count"] == 0
    assert report["shaped_host_not_proven_after"] == 0
    assert report["3d_collisions_after"]["complete_no_body_collision_proof"]
    assert report["physical_stock_inventory_preserved"]
    assert not report["placement_eligible"]
    assert not search["finite_point_coverage_is_final_proof"]


def test_polygon_separator_detects_loss_away_from_centroid():
    before, _, problem, host = _case()
    bar = _shapes(before, host)[0]
    # The source centroid is supplied, yet the rest of its polygon is missing.
    candidate = _Candidate(0, bar, False, True, ((10, 300, box(1000, 140, 1500, 160)),))
    residues = _missing(problem, (candidate,))
    assert sum(r[3].area for r in residues) == 150000-10000


def test_lazy_polygon_cuts_reject_midpoint_only_U_coverage():
    pytest.importorskip("scipy.optimize")
    before, lanes, problem, host = _case(edge=True, demand_bounds=(100, 100, 2225, 200))
    lanes = tuple(replace(lane, source=replace(lane.source, required_interval_mm=(100, 2225)))
                  for lane in lanes)
    after, search = propose_joint_shaped_repair(before, lanes, problem, host, time_limit_s=20)
    assert any(row["new_witnesses"] > 0 for row in search["rounds"])
    report = check_shaped_global_repair(before, after, lanes, problem, host)
    assert report["source_coverage"]["uncovered_cell_count"] == 0
    assert report["shaped_host_not_proven_after"] >= 1
    assert report["changed_bar_3d_conflict_count"] == 0


def test_positive_microstrip_is_still_a_polygon_obligation():
    before, _, problem, host = _case()
    bar = _shapes(before, host)[0]
    candidate = _Candidate(0, bar, False, True, ((10, 300, box(500+1e-8, 100, 2000, 200)),))
    assert sum(r[3].area for r in _missing(problem, (candidate,))) > 0


def test_over_limit_source_float_retains_inventory_without_rounding_for_U():
    pytest.importorskip("scipy.optimize")
    before, lanes, problem, host = _case()
    interval = (25, 11725.000000000002)
    before = tuple(replace(b, installed_interval_mm=interval) for b in before)
    lanes = tuple(replace(lane, source=replace(lane.source, installed_interval_mm=interval)) for lane in lanes)
    material = box(0, -1000, 20000, 1000)
    host = replace(host, sections=(SolidHostSection(0, 200, material),), volume_mm3=material.area*200)
    after, search = propose_joint_shaped_repair(before, lanes, problem, host, time_limit_s=20)
    assert len(search["U_shape_length_limit_bar_ids"]) == 2
    assert all(b.shape_kind == "straight" and b.selected_cut_length_mm > 11700 for b in after)
    report = check_shaped_global_repair(before, after, lanes, problem, host)
    assert report["physical_stock_inventory_preserved"]


def test_optional_solver_or_budget_never_returns_partial_party():
    before, lanes, problem, host = _case(edge=True)
    after, search = propose_joint_shaped_repair(before, lanes, problem, host, time_limit_s=.001)
    assert len(after) == len(before)
    assert tuple(b.id for b in after) == tuple(b.id for b in before)
    assert search["complete_incumbent_retained"]
    assert search["reason"] == "candidate_time_budget"
    report = check_shaped_global_repair(before, after, lanes, problem, host)
    assert report["shaped_host_not_proven_after"] == report["shaped_host_not_proven_before"]


def test_background_only_cells_do_not_manufacture_constraints():
    before, lanes, problem, host = _case()
    problems = tuple(replace(p, demand=replace(p.demand, cells=tuple(
        replace(c, level_index=0) for c in p.demand.cells))) for p in problem.direction_problems)
    empty = replace(problem, direction_problems=problems)
    assert _missing(empty, ()) == []


@pytest.mark.parametrize("key,value", (("maximum_rounds", 0), ("maximum_axes_per_bar", True),
    ("maximum_candidates_per_bar", 65), ("time_limit_s", float("nan"))))
def test_bad_resource_limits_rejected(key, value):
    with pytest.raises(ValueError, match="bounded"):
        propose_joint_shaped_repair(*_case(), **{key: value})
