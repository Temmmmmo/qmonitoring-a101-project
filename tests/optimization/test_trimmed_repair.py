from dataclasses import replace

import pytest
from shapely.geometry import Polygon, box

from rebar.optimization.algorithms.trimmed_repair import rebuild_trimmed_zones
from rebar.optimization.algorithms.tz_boundary_trim import trim_straight_bars_to_outer_boundary
from rebar.optimization.contracts.shaped_physical import Line3D
from rebar.optimization.services.solid_host import SolidHostSection
from rebar.optimization.services.trimmed_repair import check_rebuilt_trimmed_bars
from test_shaped_global_coverage import _case, _shapes


def _fixture():
    before, lanes, problem, host = _case(demand_bounds=(500, -50, 2000, 350))
    before = _shapes(before, host)
    material = Polygon(box(0, -1000, 5000, 1000).exterior.coords,
                       [box(900, 95, 1100, 105).exterior.coords])
    host = replace(host, sections=(SolidHostSection(0, 200, material),), volume_mm3=material.area*200)
    trimmed, _ = trim_straight_bars_to_outer_boundary(before, host, respect_openings=True)
    return before, trimmed, lanes, problem, host


def test_rebuilt_candidate_repairs_hole_cut_without_losing_already_covered_area():
    templates, before, lanes, problem, host = _fixture()
    baseline = check_rebuilt_trimmed_bars(before, before, lanes, problem, host)
    assert baseline["geometric_presence"]["uncovered_cell_count"] == 1
    after, report = rebuild_trimmed_zones(before, templates, lanes, problem, host,
        maximum_mass_kg=20, maximum_candidates=300, time_limit_s=20, stock_time_limit_s=.2)
    assert report["search"]["actions"]
    assert report["geometric_presence"]["directions"][-2]["uncovered_area_mm2"] < (
        baseline["geometric_presence"]["directions"][-2]["uncovered_area_mm2"])
    assert report["previously_covered_area_lost_mm2"] == {"geometric_presence": 0, "control_40d": 0}
    assert report["material_boundary_failures_after"] == 0
    assert not report["placement_eligible"] and not report["engineering_approval"]
    assert not report["source_demand_removed"]
    assert len(after) >= 1


def test_checker_reports_loss_and_holes_independently_of_anchor():
    _, before, lanes, problem, host = _fixture()
    bar = before[-1]
    line = bar.segments[0]
    moved = replace(bar, segments=(Line3D((line.start_mm[0]+500, *line.start_mm[1:]),
                                        (line.end_mm[0]+500, *line.end_mm[1:])),))
    report = check_rebuilt_trimmed_bars(before, (*before[:-1], moved), lanes, problem, host)
    assert report["previously_covered_area_lost_mm2"]["geometric_presence"] > 0
    assert "coverage_regression" in report["blockers"]


def test_strict_source_bounds_and_new_background_collision_are_not_waived():
    _, before, lanes, problem, host = _fixture()
    bar = before[-1]
    a, b = bar.segments[0].start_mm, bar.segments[0].end_mm
    moved = replace(bar, id="bad", segments=(Line3D((a[0], 0., a[2]), (b[0], 0., b[2])),))
    with pytest.raises(ValueError, match="background"):
        check_rebuilt_trimmed_bars(before, (*before[:-1], moved), lanes, problem, host)
    moved = replace(moved, source_bar_ids=("invented",))
    with pytest.raises(ValueError, match="Unknown"):
        check_rebuilt_trimmed_bars(before, (*before[:-1], moved), lanes, problem, host)


@pytest.mark.parametrize("value", [True, float("nan"), -1])
def test_invalid_mass_budget_rejected(value):
    templates, before, lanes, problem, host = _fixture()
    with pytest.raises(ValueError):
        rebuild_trimmed_zones(before, templates, lanes, problem, host, maximum_mass_kg=value)


def test_zero_additions_keeps_entire_original_inventory_and_does_not_claim_pass():
    templates, before, lanes, problem, host = _fixture()
    after, report = rebuild_trimmed_zones(before, templates, lanes, problem, host,
        maximum_mass_kg=20, maximum_candidates=10, maximum_additions=0,
        time_limit_s=1, stock_time_limit_s=.1)
    assert before == after
    assert report["geometric_presence"]["status"] == "fail"
    assert report["search"]["global_infeasibility_proven"] is False
