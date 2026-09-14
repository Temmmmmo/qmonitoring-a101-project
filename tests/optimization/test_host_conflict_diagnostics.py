"""Readable geometric causes never remove FE or certify a structural design."""
from copy import deepcopy
from dataclasses import replace

import pytest
from shapely.affinity import affine_transform
from shapely.geometry import box

from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.contracts.physical import PhysicalBar
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS, PlateProblem
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutProblem
from rebar.optimization.services.host_conflict_diagnostics import (
    diagnose_physical_host_failures, inspect_source_material_mismatch, inspect_straight_40d_bbox_obstruction,
)
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection


def host(footprint=None, cover=25):
    footprint = box(0, 0, 2000, 2000) if footprint is None else footprint
    return OrthogonalSolidHost((SolidHostSection(0, 200, footprint),), 25, 25, cover, footprint.area*200, 6)


def bar(identifier="a", *, q=1000, interval=(100, 1900), axis=Axis.X):
    return PhysicalBar(identifier, Direction(Layer.TOP, axis), "A500", 10, q, interval, (identifier,))


def problem(polygons, active=True):
    recipe = ReinforcementRecipe(Rebar(300, 10), (Rebar(300, 10),))
    levels = (DemandLevel(0, 1, 0, 1, "background", None, False, replace(recipe, additions=())),
        DemandLevel(1, 2, 1, 2, "additional", recipe.additions[0], True, recipe))
    directions = []
    for direction in PLATE_DIRECTIONS:
        cells = tuple(DemandCell(index, tuple(p.exterior.coords)[:-1], (p.centroid.x, p.centroid.y),
            2 if active else 1, int(active)) for index, p in enumerate(polygons))
        directions.append(LayoutProblem(DemandMap(direction, levels, cells, (0, 0, 2000, 2000))))
    return PlateProblem(tuple(directions))


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_disjoint_edge_hole_both_and_clear_categories(axis):
    material = box(0, 0, 2000, 2000).difference(box(500, 400, 600, 600))
    if axis is Axis.Y:
        material = affine_transform(material, (0, 1, 1, 0, 0, 0))
    bars = (bar("clear", axis=axis), bar("edge", interval=(-10, 300), axis=axis),
        bar("hole", q=500, interval=(100, 900), axis=axis),
        bar("both", q=500, interval=(-10, 900), axis=axis))
    frozen = deepcopy(bars)
    report = diagnose_physical_host_failures(bars, host(material))
    assert report["physical_bar_count"] == 4
    assert report["host_failed_bar_count"] == 3
    assert report["host_contained_bar_count"] == 1
    assert report["categories"] == {"outer_edge": 1, "opening": 1, "both": 1}
    assert report["failures_by_direction"] == {str(bars[0].direction): 3}
    assert bars == frozen
    assert not report["placement_eligible"] and not report["engineering_approval"]
    assert not report["source_demand_removed"]


def test_cover_only_is_not_reported_as_axis_or_steel_through_void():
    report = diagnose_physical_host_failures((bar(q=10),), host())
    entry = report["failures"][0]
    assert report["cover_only_failure_count"] == 1
    assert entry["cover_only"]
    assert entry["outside_material_body_area_mm2"] == 0
    assert entry["centreline_outside_material_length_mm"] == 0
    assert entry["outer_envelope_area_mm2"] > 0


def test_radius_failure_with_axis_in_material_is_not_a_cover_only_failure():
    entry = diagnose_physical_host_failures((bar(q=1),), host())["failures"][0]
    assert not entry["cover_only"]
    assert entry["outside_material_body_area_mm2"] > 0
    assert entry["centreline_outside_material_length_mm"] == 0


def test_recess_is_an_outer_edge_not_an_enclosed_hole():
    material = box(0, 0, 2000, 2000).difference(box(500, 0, 600, 600))
    report = diagnose_physical_host_failures((bar(q=500),), host(material))
    assert report["categories"] == {"outer_edge": 1, "opening": 0, "both": 0}


def test_all_height_sections_are_considered():
    base = host()
    cut = box(0, 0, 2000, 2000).difference(box(500, 900, 600, 1100))
    varied = replace(base, sections=(SolidHostSection(0, 100, base.sections[0].footprint),
        SolidHostSection(100, 200, cut)), volume_mm3=base.sections[0].footprint.area*100+cut.area*100)
    assert diagnose_physical_host_failures((bar(),), varied)["host_failed_bar_count"] == 1


def test_empty_party_is_not_placement_approval():
    result = diagnose_physical_host_failures((), host())
    assert result["physical_bar_count"] == result["host_failed_bar_count"] == 0
    assert not result["placement_eligible"]


@pytest.mark.parametrize("change", ({"transverse_axis_mm": float("nan")},
    {"diameter_mm": True}, {"installed_interval_mm": (0, float("inf"))},
    {"installed_interval_mm": (0, 11701)}, {"source_bar_ids": ()}))
def test_malformed_bar_diagnostics_fail_closed(change):
    with pytest.raises(ValueError):
        diagnose_physical_host_failures((replace(bar(), **change),), host())


def test_duplicate_physical_ids_fail_closed():
    with pytest.raises(ValueError):
        diagnose_physical_host_failures((bar(), bar()), host())


def test_positive_source_over_void_is_located_without_deleting_or_changing_it():
    source = problem((box(100, 100, 300, 300), box(-100, 0, 100, 200)))
    frozen = deepcopy(source)
    material = box(0, 0, 2000, 2000).difference(box(150, 150, 250, 250))
    report = inspect_source_material_mismatch(source, host(material))
    assert report["required_cell_count"] == report["mismatched_direction_cell_count"] == 8
    assert report["outside_outer_direction_cell_count"] == report["over_openings_direction_cell_count"] == 4
    assert {r["over_openings_area_mm2"] for r in report["cells"]} == {0, 10000}
    assert source == frozen
    assert not report["source_demand_removed"] and not report["coverage_acceptance_changed"]


def test_background_is_not_reported_as_missing_additional_reinforcement():
    report = inspect_source_material_mismatch(problem((box(-100, -100, 100, 100),), active=False), host())
    assert report["required_cell_count"] == report["mismatched_direction_cell_count"] == 0


def test_along_edge_obstruction_is_independent_of_transverse_axis_and_bar_count():
    source = problem((box(0, 900, 500, 1100),))
    frozen = deepcopy(source)
    report = inspect_straight_40d_bbox_obstruction(source, host())
    assert report["obstructed_direction_cell_count"] == 2
    assert all(row["direction"].endswith("X") for row in report["cells"])
    slack = 0.001/60
    assert all(row["optimistic_along_core_interval_mm"] == [425-slack, 1575+slack] for row in report["cells"])
    assert all(row["outside_optimistic_core_area_mm2"] == pytest.approx(85000-slack*200) for row in report["cells"])
    assert report["status"] == "infeasible_under_stated_straight_40d_policy"
    assert report["full40d_is_normatively_required"] == "not_asserted"
    assert not report["policy_changed"] and not report["placement_eligible"]
    assert source == frozen


def test_clear_bbox_does_not_claim_feasible_even_when_FE_is_entirely_in_a_hole():
    solid = box(0, 0, 2000, 2000).difference(box(800, 800, 1200, 1200))
    report = inspect_straight_40d_bbox_obstruction(problem((box(900, 900, 1100, 1100),)), host(solid))
    assert report["obstructed_direction_cell_count"] == 0
    assert report["status"] == "no_bbox_obstruction_not_a_feasibility_proof"
    assert not report["placement_eligible"]


def test_obstruction_uses_optimistic_union_not_conservative_common_height_slice():
    base = host()
    narrow = box(500, 0, 2000, 2000)
    varied = replace(base, sections=(SolidHostSection(0, 100, narrow),
        SolidHostSection(100, 200, base.sections[0].footprint)),
        volume_mm3=(narrow.area+base.sections[0].footprint.area)*100)
    report = inspect_straight_40d_bbox_obstruction(problem((box(500, 900, 600, 1000),)), varied)
    assert report["optimistic_host_union_bbox_mm"] == [0, 0, 2000, 2000]
    assert report["obstructed_direction_cell_count"] == 0


def test_too_short_slab_has_empty_40d_core_and_preserves_entire_source():
    report = inspect_straight_40d_bbox_obstruction(problem((box(100, 100, 200, 200),)), host(box(0, 0, 600, 600)))
    assert report["obstructed_direction_cell_count"] == 4
    assert all(row["outside_optimistic_core_area_mm2"] == row["source_cell_area_mm2"] for row in report["cells"])


def test_background_creates_no_40d_obstruction():
    report = inspect_straight_40d_bbox_obstruction(problem((box(0, 0, 100, 100),), active=False), host())
    assert report["required_cell_count"] == report["obstructed_direction_cell_count"] == 0


def test_duplicate_FE_ids_are_rejected():
    source = problem((box(0, 0, 100, 100),))
    first = source.direction_problems[0]
    first = replace(first, demand=replace(first.demand, cells=(first.demand.cells[0],)*2))
    changed = replace(source, direction_problems=(first, *source.direction_problems[1:]))
    with pytest.raises(ValueError, match="Unique integer FE"):
        inspect_source_material_mismatch(changed, host())


def test_total_vertex_budget_checked_before_source_geometry():
    source = problem((box(0, 0, 100, 100),))
    first = source.direction_problems[0]
    cells = tuple(replace(first.demand.cells[0], id=i, poly=((0, 0),)*1001) for i in range(200))
    first = replace(first, demand=replace(first.demand, cells=cells))
    changed = replace(source, direction_problems=(first, *source.direction_problems[1:]))
    with pytest.raises(ValueError, match="Total source vertex budget"):
        inspect_source_material_mismatch(changed, host())


def test_a_micrometre_strip_is_measured_not_silently_snapped():
    source = problem((box(-0.001, 0, 100, 100),))
    report = inspect_source_material_mismatch(source, host())
    assert report["outside_outer_direction_cell_count"] == 4
    assert all(row["outside_outer_area_mm2"] == pytest.approx(0.1) for row in report["cells"])


def test_two_subtolerance_parts_still_explain_a_combined_failure():
    material = box(0, 0, 2000, 2000).difference(box(0.05, 100, 0.056, 100.1))
    report = inspect_source_material_mismatch(problem((box(-0.006, 100, 0.1, 100.1),)), host(material, cover=0))
    assert report["mismatched_direction_cell_count"] == 4
    assert report["outside_outer_direction_cell_count"] == 4
    assert report["over_openings_direction_cell_count"] == 4
    assert report["both_direction_cell_count"] == 4


@pytest.mark.parametrize("level", (True, -1, 2, 1.0))
def test_unknown_or_bool_level_is_not_coerced(level):
    source = problem((box(0, 0, 100, 100),))
    first = source.direction_problems[0]
    first = replace(first, demand=replace(first.demand, cells=(replace(first.demand.cells[0], level_index=level),)))
    changed = replace(source, direction_problems=(first, *source.direction_problems[1:]))
    with pytest.raises(ValueError):
        inspect_source_material_mismatch(changed, host())


@pytest.mark.parametrize("poly", (((0, 0), (float("nan"), 10), (10, 0)),
    ((0, 0), (10, 10), (0, 10), (10, 0)), ((0, 0), (0, 0), (0, 0))))
def test_bad_FE_never_repaired_as_diagnostic_side_effect(poly):
    source = problem((box(0, 0, 100, 100),))
    first = source.direction_problems[0]
    first = replace(first, demand=replace(first.demand, cells=(replace(first.demand.cells[0], poly=poly),)))
    changed = replace(source, direction_problems=(first, *source.direction_problems[1:]))
    with pytest.raises(ValueError):
        inspect_source_material_mismatch(changed, host())
