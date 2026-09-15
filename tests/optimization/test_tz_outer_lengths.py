from dataclasses import replace

import pytest
from shapely.geometry import box

from rebar.optimization.algorithms.tz_outer_lengths import propose_tz_outer_length_exchanges
from rebar.optimization.services.shaped_fe_repair import ACTUAL_CORE_SERVICE
from rebar.optimization.services.shaped_geometry import shaped_cut_length_mm
from rebar.optimization.services.solid_host import SolidHostSection
from rebar.optimization.services.tz_outer_scope import (
    check_tz_outer_batch, reselect_straight_length, translate_straight_whole,
)
from test_shaped_global_coverage import _case, _shapes


def _exchange_case():
    original, lanes, problem, host = _case()
    # A concave external boundary: a 5850 mm bar does not fit row 100, but
    # DOES fit row 200. The replacement exchanges whole catalogue lengths.
    material = box(0, -1000, 4000, 1000).union(box(4000, 150, 8000, 1000))
    host = replace(host, sections=(SolidHostSection(0, 200, material),), volume_mm3=material.area*200)
    bars = _shapes(original, host)
    bars = reselect_straight_length(bars[0], 5850), bars[1]
    return bars, lanes, problem, host


def test_atomic_new_lengths_fix_external_edge_without_discarded_cut_or_FE():
    before, lanes, problem, host = _exchange_case()
    after, search = propose_tz_outer_length_exchanges(before, lanes, problem, host, allow_transverse=False)
    assert len(search["operations"]) == 1
    assert search["exact_cutting_inventory_preserved"]
    assert after[0].selected_cut_length_mm == 2925
    assert after[1].selected_cut_length_mm == 5850
    report = check_tz_outer_batch(before, after, lanes, problem, host, allow_length_reassignment=True)
    assert report["external_boundary_failures_before"] == 1
    assert report["external_boundary_failures_after"] == 0
    assert report["source_coverage"]["uncovered_cell_count"] == 0
    assert report["collisions"]["status"] == "pass"
    assert len(report["length_changes"]) == 2
    assert report["physical_metrics"]["true_cut_length_mm"] == 8775
    # The artificial 2-bar test party itself is not a complete 11700 stock.
    assert report["stock_cutting"]["status"] != "pass"
    assert not report["placement_eligible"]
    with pytest.raises(ValueError, match="Rigid"):
        check_tz_outer_batch(before, after, lanes, problem, host)


def test_no_half_exchange_if_longer_donor_cannot_fit():
    before, lanes, problem, host = _exchange_case()
    material = box(0, -1000, 4000, 1000)
    host = replace(host, sections=(SolidHostSection(0, 200, material),), volume_mm3=material.area*200)
    after, search = propose_tz_outer_length_exchanges(before, lanes, problem, host, allow_transverse=False)
    assert after == before
    assert search["operations"] == []


def test_atomic_exchange_can_transfer_FE_responsibility_to_the_lengthened_donor():
    """Do not reject the shorter target before considering the longer donor.

    All demanded FE lie in the actual material: y=160..200 is above the
    concavity at y=150. The old long bar at y=100 crosses the external edge;
    a long bar at y=200 fits. A third unrelated bar completes exact stock.
    No source FE moves or loses demand and no two weak recipes are added.
    """
    before, lanes, problem, host = _exchange_case()
    third = replace(translate_straight_whole(before[1], start_mm=25, transverse_axis_mm=800),
                    id="c", source_bar_ids=("zone-c/0/0",))
    third_lane = replace(lanes[1], zone_id="zone-c", axis_window_mm=(650, 950),
        source=replace(lanes[1].source, id="zone-c/0/0", transverse_axis_mm=800))
    before, lanes = (*before, third), (*lanes, third_lane)
    polygon = box(500, 160, 5000, 200)
    problem = replace(problem, direction_problems=tuple(replace(row, demand=replace(row.demand,
        bbox=polygon.bounds, cells=tuple(replace(cell, poly=tuple(polygon.exterior.coords)[:-1],
            centroid=(2750, 180)) for cell in row.demand.cells))) for row in problem.direction_problems))
    baseline = check_tz_outer_batch(before, before, lanes, problem, host,
                                    longitudinal_service_policy=ACTUAL_CORE_SERVICE)
    assert baseline["tz_blockers"] == ["external_boundary"]
    assert baseline["external_boundary_failures_after"] == 1
    after, search = propose_tz_outer_length_exchanges(before, lanes, problem, host,
        allow_transverse=False, longitudinal_service_policy=ACTUAL_CORE_SERVICE)
    assert len(search["operations"]) == 1
    assert [bar.selected_cut_length_mm for bar in after] == [2925, 5850, 2925]
    assert search["exact_cutting_inventory_preserved"]
    assert after[2] == third
    checked = check_tz_outer_batch(before, after, lanes, problem, host, allow_length_reassignment=True,
                                   longitudinal_service_policy=ACTUAL_CORE_SERVICE)
    assert checked["checked_gates_status"] == "pass" and checked["tz_blockers"] == []
    assert checked["external_boundary_failures_after"] == 0
    assert checked["source_coverage"]["uncovered_cell_count"] == 0
    assert checked["collisions"]["status"] == "pass" and checked["stock_cutting"]["status"] == "pass"
    assert len(checked["length_changes"]) == 2
    assert checked["physical_metrics"]["true_cut_length_mm"] == 11700
    assert not checked["source_demand_removed"] and not checked["placement_eligible"]


@pytest.mark.parametrize("length", (True, 2924, float("nan"), float("inf"), 0, -1))
def test_no_arbitrary_clipping_or_unbounded_length(length):
    before, _, _, _ = _exchange_case()
    with pytest.raises(ValueError, match="catalogue"):
        reselect_straight_length(before[0], length)


def test_catalogue_exchange_preserves_roundtripped_length_without_float_membership_failure():
    before, _, _, _ = _exchange_case()
    length = 11700.000000000002
    selected = reselect_straight_length(before[0], length)
    assert selected.selected_cut_length_mm == length
    assert shaped_cut_length_mm(selected) == pytest.approx(length, abs=1e-7)


@pytest.mark.parametrize("field", ("diameter_mm", "selected_cut_length_mm", "placement_profile_id"))
def test_length_opt_in_does_not_authorize_other_mutations(field):
    before, lanes, problem, host = _exchange_case()
    value = {"diameter_mm": 12, "selected_cut_length_mm": 3900,
             "placement_profile_id": "invented-layer-profile"}[field]
    altered = (replace(reselect_straight_length(before[0], 2925), **{field: value}), before[1])
    with pytest.raises(ValueError):
        check_tz_outer_batch(before, altered, lanes, problem, host, allow_length_reassignment=True)


def test_new_length_without_old_40d_is_reported_not_accepted():
    before, lanes, problem, host = _exchange_case()
    after = tuple(translate_straight_whole(reselect_straight_length(b, 1170), start_mm=500) for b in before)
    report = check_tz_outer_batch(before, after, lanes, problem, host, allow_length_reassignment=True)
    assert report["external_boundary_failures_after"] == 0
    assert report["source_coverage"]["status"] == "fail"
    assert "original_FE_coverage_with_retained_40d" in report["tz_blockers"]
    assert shaped_cut_length_mm(after[0]) == 1170


@pytest.mark.parametrize("limit", (True, 0, -1, float("nan"), float("inf")))
def test_search_budget_is_bounded(limit):
    before, lanes, problem, host = _exchange_case()
    with pytest.raises(ValueError, match="bounded"):
        propose_tz_outer_length_exchanges(before, lanes, problem, host, time_limit_s=limit)
