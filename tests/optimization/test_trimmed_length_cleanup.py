"""No short-bar heuristic may hide a positive original FE fragment or lose40d."""
from dataclasses import replace

import pytest
from shapely.geometry import Polygon, box

from rebar.optimization.algorithms.trimmed_length_cleanup import prune_redundant_trimmed_bars
from rebar.optimization.contracts.shaped_physical import Line3D
from rebar.optimization.services.opening_relocation import lane_map
from rebar.optimization.services.shaped_global_coverage import _strict_coverage
from rebar.optimization.services.solid_host import SolidHostSection
from rebar.optimization.services.trimmed_length_cleanup import _all_offers, check_trimmed_length_cleanup
from test_shaped_global_coverage import _case, _shapes


def _piece(bar, identifier, low, high):
    segment = bar.segments[0]
    return replace(bar, id=identifier, segments=(Line3D((low,*segment.start_mm[1:]),
        (high,*segment.end_mm[1:])),), selected_cut_length_mm=high-low)


def test_duplicate_pruning_has_full_source_and_3D_proof_not_a_stock_claim():
    original,lanes,problem,host = _case(duplicate=True)
    before = _shapes(original,host)
    after,report = prune_redundant_trimmed_bars(before,lanes,problem,host,stock_time_limit_s=2)
    assert len(after) == 1 and report["removed_bar_count"] == 1
    assert report["accepted_nonregression"]
    assert report["coverage_after"]["geometric_presence"]["uncovered_cell_count"] == 0
    assert report["coverage_after"]["control_40d"]["uncovered_cell_count"] == 0
    assert report["collisions_before"]["proven_collision_pair_count"] == 1
    assert report["collisions"]["proven_collision_pair_count"] == 0
    assert report["stock_cutting"]["status"] == "fail"
    assert report["physical_metrics"]["mass_kg"] < report["physical_metrics_before"]["mass_kg"]
    assert not report["source_demand_removed"] and not report["old_once_only_owner_certificate_reused"]
    assert not report["placement_eligible"] and not report["engineering_approval"]
    assert all(item in before for item in after)
    assert {row["input_bar_id"] for row in report["piece_mapping"]} == {bar.id for bar in before}


def test_redundant_short_piece_can_disappear_but_the_required_short_one_stays():
    original,lanes,problem,host = _case(demand_bounds=(700,300,750,350))
    large,other = _shapes(original,host)
    necessary = _piece(other,"critical-50mm",700,750)
    redundant = _piece(large,"redundant-25mm",500,525)
    after,report = prune_redundant_trimmed_bars((large,necessary,redundant),lanes,problem,host,stock_time_limit_s=2)
    assert necessary in after and redundant not in after
    assert report["coverage_before"]["control_40d"]["status"] == "fail"
    assert report["coverage_after"]["control_40d"]["status"] == "fail"
    assert not report["search"]["minimum_allowed_length_invented"]
    assert all(not row["output_bar_ids"] for row in report["piece_mapping"] if row["input_bar_id"] == redundant.id)


def test_preserving_presence_but_losing_control40d_is_rejected():
    original,lanes,problem,host = _case()
    long = _shapes(original,host)[0]
    first,second = _piece(long,"left",25,1500),_piece(long,"right",1500,2950)
    before = (long,first,second)
    old_offers = _all_offers(before,lane_map(lanes))
    new_offers = _all_offers((first,second),lane_map(lanes))
    assert _strict_coverage(problem,old_offers["geometric_presence"])["status"] == "pass"
    assert _strict_coverage(problem,new_offers["geometric_presence"])["status"] == "pass"
    with pytest.raises(ValueError,match="control_40d"):
        check_trimmed_length_cleanup(before,(first,second),lanes,problem,host)


def test_same_uncovered_cell_count_does_not_hide_more_missing_geometry():
    original,lanes,problem,host = _case(demand_bounds=(500,-50,2000,450))
    before = _shapes(original,host)
    old = _strict_coverage(problem,_all_offers(before,lane_map(lanes))["geometric_presence"])
    new = _strict_coverage(problem,_all_offers(before[:1],lane_map(lanes))["geometric_presence"])
    assert old["uncovered_cell_count"] == new["uncovered_cell_count"] == 1
    with pytest.raises(ValueError,match="previously covered original FE geometry"):
        check_trimmed_length_cleanup(before,before[:1],lanes,problem,host)


def test_positive_tiny_fragment_is_not_removed_by_area_tolerance():
    original,lanes,problem,host = _case(demand_bounds=(500,250,500.0000001,250.0000001))
    before = _shapes(original,host)
    # a's strip ends at y250, b supplies the positive but microscopic FE fragment.
    with pytest.raises(ValueError,match="previously covered original FE geometry"):
        check_trimmed_length_cleanup(before,before[:1],lanes,problem,host)


@pytest.mark.parametrize("mutation",["q","z","diameter","steel","owner","id","length","nan","duplicate"])
def test_cleanup_does_not_accept_a_modified_or_duplicated_retained_record(mutation):
    original,lanes,problem,host = _case()
    before = _shapes(original,host)
    bar = before[0]
    a,b = bar.segments[0].start_mm,bar.segments[0].end_mm
    if mutation in ("q","z","nan"):
        index = 1 if mutation == "q" else 2
        aa,bb = list(a),list(b)
        aa[index] = bb[index] = float("nan") if mutation == "nan" else aa[index]+1
        bar = replace(bar,segments=(Line3D(tuple(aa),tuple(bb)),))
    elif mutation == "diameter":
        bar = replace(bar,diameter_mm=12)
    elif mutation == "steel":
        bar = replace(bar,steel_class="B500")
    elif mutation == "owner":
        bar = replace(bar,source_bar_ids=before[1].source_bar_ids)
    elif mutation == "id":
        bar = replace(bar,id="new-not-pruned")
    elif mutation == "length":
        bar = _piece(bar,bar.id,a[0],b[0]+1)
    after = (bar,before[1]) if mutation != "duplicate" else (bar,bar,before[1])
    with pytest.raises(ValueError):
        check_trimmed_length_cleanup(before,after,lanes,problem,host)


def test_hole_crossing_body_is_not_treated_as_valid_by_outer_bbox():
    original,lanes,problem,host = _case()
    before = _shapes(original,host)
    footprint = Polygon(box(0,-1000,5000,1000).exterior.coords,[box(1000,95,1200,105).exterior.coords])
    host = replace(host,sections=(SolidHostSection(0,200,footprint),),volume_mm3=footprint.area*200)
    with pytest.raises(ValueError,match="full-body host-valid"):
        prune_redundant_trimmed_bars(before,lanes,problem,host)


def test_hole_split_fragments_are_never_joined_or_extended():
    original,lanes,problem,host = _case(demand_bounds=(500,100,2000,200))
    bar = _shapes(original,host)[0]
    pieces = (_piece(bar,"left",25,1000),_piece(bar,"right",1200,2950))
    footprint = Polygon(box(0,-1000,5000,1000).exterior.coords,[box(1000,50,1200,150).exterior.coords])
    host = replace(host,sections=(SolidHostSection(0,200,footprint),),volume_mm3=footprint.area*200)
    after,report = prune_redundant_trimmed_bars(pieces,lanes,problem,host,stock_time_limit_s=2)
    assert after == pieces
    assert report["coverage_after"]["geometric_presence"]["status"] == "fail"
    assert report["openings_retained"] and not report["lengths_rounded_or_extended"]


def test_prior_exact_stock_pass_cannot_be_lost_by_pruning():
    original,lanes,problem,host = _case(duplicate=True)
    bars = _shapes(original,host)
    before = (*bars,replace(bars[0],id="duplicate-c"),replace(bars[1],id="duplicate-d"))
    after,report = prune_redundant_trimmed_bars(before,lanes,problem,host,stock_time_limit_s=2)
    assert after == before
    assert report["stock_cutting"]["status"] == "pass"
    assert report["removed_bar_count"] == 0
    assert report["search"]["stock_regression_proposal_rolled_back"]


@pytest.mark.parametrize("kwargs",[{"maximum_removed_bars":True},{"maximum_candidate_checks":-1},
    {"time_limit_s":float("nan")},{"time_limit_s":False},{"stock_time_limit_s":61}])
def test_search_limits_are_explicit(kwargs):
    original,lanes,problem,host = _case()
    with pytest.raises(ValueError):
        prune_redundant_trimmed_bars(_shapes(original,host),lanes,problem,host,**kwargs)


def test_zero_candidate_budget_retains_every_exact_length_and_original_owner():
    original,lanes,problem,host = _case(duplicate=True)
    before = _shapes(original,host)
    after,report = prune_redundant_trimmed_bars(before,lanes,problem,host,maximum_candidate_checks=0)
    assert before == after and report["removed_bar_count"] == 0
    assert report["search"]["stop_reason"] == "finite_candidate_or_removal_limit"
