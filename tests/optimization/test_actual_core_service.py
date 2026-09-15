"""Uniform steel does not stop at an old zone's longitudinal provenance bbox."""
from dataclasses import replace

import pytest
from shapely.geometry import box
from shapely.ops import unary_union

from rebar.optimization.services.opening_relocation import lane_map
from rebar.optimization.services.shaped_fe_repair import ACTUAL_CORE_SERVICE, shaped_service_offers
from rebar.optimization.services.tz_outer_scope import check_tz_outer_batch, reselect_straight_length
from test_shaped_global_coverage import _case, _shapes


def test_actual_core_serves_beyond_old_owner_but_retains_full_40d():
    original, lanes, _, host = _case()
    bar = reselect_straight_length(_shapes(original, host)[0], 3900)
    sources = lane_map(lanes)
    legacy = unary_union([p for _, _, p in shaped_service_offers(bar, sources)])
    actual = unary_union([p for _, _, p in shaped_service_offers(bar, sources,
                          longitudinal_service_policy=ACTUAL_CORE_SERVICE)])
    assert legacy.bounds == (500, -50, 2000, 250)
    assert actual.bounds == (425, -50, 3525, 250)
    assert actual.covers(box(2200, 100, 3000, 200))
    assert not actual.intersects(box(3550, 100, 3900, 200))
    assert not actual.intersects(box(2200, 251, 3000, 300))


def test_required_FE_outside_old_owner_is_supported_by_actual_anchored_core():
    original, lanes, problem, host = _case(demand_bounds=(600, 100, 3000, 200))
    before = _shapes(original, host)
    after = tuple(reselect_straight_length(b, 3900) for b in before)
    old_model = check_tz_outer_batch(before, after, lanes, problem, host, allow_length_reassignment=True)
    assert old_model["source_coverage"]["uncovered_cell_count"] == 1
    actual = check_tz_outer_batch(before, after, lanes, problem, host, allow_length_reassignment=True,
                                 longitudinal_service_policy=ACTUAL_CORE_SERVICE)
    assert actual["source_coverage"]["uncovered_cell_count"] == 0
    assert actual["source_coverage"]["straight_end_control_anchor_diameters"] == 40
    assert not actual["source_coverage"]["transverse_source_windows_enlarged"]
    assert not actual["source_demand_removed"]
    assert not actual["placement_eligible"]


def test_actual_core_does_not_credit_increased_diameter_without_source_recipe():
    original, lanes, _, host = _case()
    bar = replace(_shapes(original, host)[0], diameter_mm=12)
    offered = shaped_service_offers(bar, lane_map(lanes), longitudinal_service_policy=ACTUAL_CORE_SERVICE)
    assert offered[0][0:2] == (10, 300)
    assert offered[0][2].bounds[0] == 25 + 40*12


def test_unknown_service_policy_never_disables_anchor_check():
    original, lanes, _, host = _case()
    with pytest.raises(ValueError, match="policy"):
        shaped_service_offers(_shapes(original, host)[0], lane_map(lanes), longitudinal_service_policy="ignore40d")
