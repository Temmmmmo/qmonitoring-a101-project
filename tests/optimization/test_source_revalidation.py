"""Saved geometry must earn coverage against the source, not its old cache."""

from copy import deepcopy
from dataclasses import replace

import pytest
from shapely.geometry import Polygon

from rebar.models import Axis, Direction, Layer, Rebar
from rebar.optimization.contracts import (
    DemandCell,
    DemandLevel,
    DemandMap,
    LayoutConstraints,
    LayoutProblem,
    LayoutSolution,
    SolutionStatus,
)
from rebar.optimization.services.detailing import build_zone_from_bbox
from rebar.optimization.services.evaluation import evaluate_layout
from rebar.optimization.services.source_revalidation import revalidate_source_demand


def _problem(levels=(1, 2, 1), *, axis=Axis.X):
    cells = []
    for index, level_index in enumerate(levels):
        x = index * 500.0
        points = ((x, 0.0), (x + 500, 0.0), (x + 500, 500.0), (x, 500.0))
        if axis is Axis.Y:
            points = tuple((y, x) for x, y in points)
        cells.append(DemandCell(10 + index, points, (
            sum(point[0] for point in points) / 4,
            sum(point[1] for point in points) / 4,
        ), 50 + level_index, level_index))
    bound = len(cells) * 500.0
    return LayoutProblem(
        demand=DemandMap(
            direction=Direction(Layer.TOP, axis),
            levels=tuple(DemandLevel(
                index=i, aci=50 + i, lower_as=None, upper_as=None, label=None,
                additional=rebar, requires_extra=rebar is not None,
            ) for i, rebar in enumerate((None, Rebar(100, 12), Rebar(100, 16)))),
            cells=tuple(cells),
            bbox=(0, 0, bound, 500) if axis is Axis.X else (0, 0, 500, bound),
            source_path="same-source.dxf",
            meta={"source_hash": "known-source", "scale": {"id": "test-only"}},
        ),
        constraints=LayoutConstraints(min_width_cells=1),
        case_id="source-test",
        meta={"single_cell_preprocessing": {"changed_count": 0, "changes": ()}},
    )


def _reduced(original):
    return replace(original, demand=replace(original.demand, cells=tuple(
        replace(cell, level_index=1) if cell.level_index == 2 else cell
        for cell in original.demand.cells
    )))


def _solution(problem, *zones):
    evaluation = evaluate_layout(problem, zones)
    return LayoutSolution(
        "saved-research", SolutionStatus.OPTIMAL, tuple(zones), evaluation.metrics,
        diagnostics=evaluation.diagnostics,
        meta={"old_policy": "legacy-research", "source": {"hash": "saved-source"}},
    )


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
def test_actual_isolated_peak_is_reported_without_redesigning_old_zone(axis):
    original = _problem(axis=axis)
    reduced = _reduced(original)
    zone = build_zone_from_bbox(reduced, reduced.demand.bbox, 1, "old")
    zone = replace(zone, meta={**zone.meta, "seed_cell_ids": (10, 11, 12)})
    saved = _solution(reduced, zone)

    audit = revalidate_source_demand(original, saved)

    assert audit.solution.status is SolutionStatus.INFEASIBLE
    assert audit.uncovered_cell_ids == (11,)
    residual, = audit.uncovered_cells
    assert residual.required_level_index == 2
    assert residual.uncovered_area_mm2 == 250000
    assert residual.cell_polygon == original.demand.cells[1].poly
    assert residual.sufficient_zone_ids == ()
    assert residual.residual_polygons[0].bbox == residual.cell_bbox
    updated, = audit.solution.zones
    assert updated.covered_cell_ids == (10, 12)
    assert replace(updated, covered_cell_ids=zone.covered_cell_ids,
                   overcovered_cell_ids=zone.overcovered_cell_ids) == zone
    assert audit.solution.metrics.total_mass_kg == saved.metrics.total_mass_kg
    assert audit.solution.metrics.physical_bar_count == saved.metrics.physical_bar_count
    assert audit.solution.metrics.total_bar_length_mm == saved.metrics.total_bar_length_mm
    assert audit.solution.meta["source_revalidation"]["prior_status"] == "optimal"
    assert audit.solution.meta["source_revalidation"]["geometry_changed"] is False
    assert audit.solution.meta["source_revalidation"]["placement_eligible"] is False
    assert not any("covered_cell_ids" in message for message in audit.solution.diagnostics)


def test_stronger_existing_zone_covers_original_peak_despite_stale_overcoverage_cache():
    original = _problem()
    reduced = _reduced(original)
    zone = build_zone_from_bbox(reduced, reduced.demand.bbox, 2, "strong")
    assert 11 in zone.overcovered_cell_ids
    saved = _solution(reduced, zone)
    raw = evaluate_layout(original, saved.zones)
    assert not raw.valid
    assert any("overcovered_cell_ids" in message for message in raw.diagnostics)

    audit = revalidate_source_demand(original, saved)

    assert audit.evaluation.valid
    assert audit.solution.status is SolutionStatus.FEASIBLE  # No new proof of optimality.
    assert audit.uncovered_cells == ()
    assert audit.uncovered_cell_ids == ()
    assert audit.solution.zones[0].overcovered_cell_ids == (10, 12)
    assert audit.solution.meta["old_policy"] == "legacy-research"
    assert audit.solution.meta["source_revalidation"]["coverage_annotations_refreshed"] is True


def test_two_sufficient_fragments_cover_one_cell_by_union():
    original = _problem((2, 0))
    reduced = _reduced(original)
    zones = tuple(build_zone_from_bbox(reduced, bbox, 2, f"half-{index}")
                  for index, bbox in enumerate(((0, 0, 250, 500), (250, 0, 500, 500))))
    audit = revalidate_source_demand(original, _solution(reduced, *zones))
    assert audit.evaluation.valid
    assert audit.uncovered_cell_ids == ()
    assert audit.solution.metrics.covered_demanded_cell_count == 1


def test_overlapping_fragments_do_not_count_the_same_half_twice():
    original = _problem((2, 0))
    reduced = _reduced(original)
    zones = tuple(build_zone_from_bbox(reduced, (0, 0, 250, 500), 2, f"half-{index}")
                  for index in range(2))
    audit = revalidate_source_demand(original, _solution(reduced, *zones))
    assert not audit.evaluation.valid
    assert audit.uncovered_cell_ids == (10,)
    residual, = audit.uncovered_cells
    assert residual.covered_area_mm2 == 125000
    assert residual.uncovered_area_mm2 == 125000
    assert residual.residual_polygons[0].bbox == (250, 0, 500, 500)
    assert residual.sufficient_zone_ids == ("half-0", "half-1")


def test_multiple_weak_zones_do_not_sum_as_into_sufficient_coverage():
    original = _problem((2, 0))
    reduced = _reduced(original)
    zones = tuple(build_zone_from_bbox(reduced, (0, 0, 500, 500), 1, f"weak-{index}")
                  for index in range(2))
    audit = revalidate_source_demand(original, _solution(reduced, *zones))
    assert audit.uncovered_cell_ids == (10,)
    assert audit.uncovered_cells[0].uncovered_area_mm2 == 250000
    assert audit.uncovered_cells[0].sufficient_zone_ids == ()


@pytest.mark.parametrize("remaining_width, is_deficient", [(4e-6, False), (6e-6, True)])
def test_residual_classification_uses_shared_relative_area_tolerance(remaining_width, is_deficient):
    original = _problem((2, 0))
    zone = build_zone_from_bbox(original, (0, 0, 500 - remaining_width, 500), 2, "almost")
    audit = revalidate_source_demand(original, _solution(original, zone))
    assert bool(audit.uncovered_cells) is is_deficient
    assert audit.evaluation.metrics.under_reinforced_cell_count == int(is_deficient)


def test_triangle_residual_uses_polygon_not_cell_bbox():
    original = _problem((2, 0))
    triangle = replace(original.demand.cells[0], poly=((0, 0), (500, 0), (0, 500)))
    original = replace(original, demand=replace(
        original.demand, cells=(triangle, original.demand.cells[1]),
    ))
    zone = build_zone_from_bbox(original, (0, 0, 250, 500), 2, "left")
    audit = revalidate_source_demand(original, _solution(original, zone))
    residual, = audit.uncovered_cells
    assert residual.cell_area_mm2 == 125000
    assert residual.covered_area_mm2 == 93750
    assert residual.uncovered_area_mm2 == 31250
    piece, = residual.residual_polygons
    assert Polygon(piece.shell, piece.holes).area == residual.uncovered_area_mm2


def test_residual_rings_keep_covered_hole():
    original = _problem((2, 0))
    zone = build_zone_from_bbox(original, (100, 100, 400, 400), 2, "middle")
    audit = revalidate_source_demand(original, _solution(original, zone))
    residual, = audit.uncovered_cells
    piece, = residual.residual_polygons
    assert len(piece.holes) == 1
    assert Polygon(piece.shell, piece.holes).area == residual.uncovered_area_mm2 == 160000


def test_source_and_saved_solution_are_not_mutated_or_aliased():
    original = _problem()
    reduced = _reduced(original)
    saved = _solution(reduced, build_zone_from_bbox(reduced, reduced.demand.bbox, 2, "old"))
    before = deepcopy((original, saved))
    audit = revalidate_source_demand(original, saved)
    assert (original, saved) == before
    audit.solution.meta["source"]["hash"] = "changed-audit-copy"
    audit.solution.meta["source_revalidation"]["source_demand_meta"]["scale"]["id"] = "changed"
    audit.solution.zones[0].meta["seed_cell_ids"] = (999,)
    assert (original, saved) == before


def test_only_solution_metrics_are_recomputed_not_trusted_or_used_to_repair_geometry():
    original = _problem()
    zone = build_zone_from_bbox(original, original.demand.bbox, 2, "strong")
    saved = _solution(original, zone)
    saved = replace(saved, metrics=replace(saved.metrics, total_mass_kg=1, physical_bar_count=0))
    audit = revalidate_source_demand(original, saved)
    assert audit.solution.metrics.total_mass_kg == zone.mass_kg
    assert audit.solution.metrics.physical_bar_count == zone.bar_count
    assert audit.solution.meta["source_revalidation"]["prior_metrics"]["total_mass_kg"] == 1


@pytest.mark.parametrize("changes, match", [
    ({"bbox": (-479, 0, 1980, 500)}, "bbox"),
    ({"width_mm": 501}, "width_mm"),
    ({"required_length_mm": 1499}, "required_length_mm"),
    ({"anchored_length_mm": 1500}, "anchored_length_mm"),
    ({"installed_length_mm": 1600}, "installed_length_mm"),
    ({"first_bar_coordinate_mm": 1}, "bbox"),
    ({"bar_count": 2}, "bar_count"),
    ({"bar_count": True}, "bar_count"),
    ({"mass_kg": 1}, "mass_kg"),
    ({"mass_kg": float("nan")}, "mass_kg"),
    ({"mass_kg": float("inf")}, "mass_kg"),
    ({"rebar": Rebar(100, 25)}, "reinforcement"),
    ({"level_index": 999}, "reconstruct"),
    ({"level_index": True}, "level index"),
    ({"demand_bbox": (0, 0, 0, 500)}, "reconstruct"),
    ({"demand_bbox": (0, 0, float("nan"), 500)}, "reconstruct"),
    ({"covered_cell_ids": (999,)}, "unknown source cell ID"),
    ({"overcovered_cell_ids": (999,)}, "unknown source cell ID"),
    ({"meta": {"seed_cell_ids": (999,)}}, "unknown source cell ID"),
    ({"meta": {"seed_cell_ids": "11"}}, "invalid seed_cell_ids"),
])
def test_corrupt_physical_fields_or_unknown_ids_are_rejected(changes, match):
    original = _problem()
    zone = build_zone_from_bbox(original, original.demand.bbox, 1, "old")
    saved = _solution(original, zone)
    corrupted = replace(saved, zones=(replace(zone, **changes),))
    with pytest.raises(ValueError, match=match):
        revalidate_source_demand(original, corrupted)


@pytest.mark.parametrize("kind", ["unknown-flag", "bad-mapping", "unknown-level", "duplicate-id",
                                  "bad-id", "invalid-polygon", "reduced-source"])
def test_unknown_or_reduced_source_is_not_accepted(kind):
    original = _problem()
    saved = _solution(original, build_zone_from_bbox(original, original.demand.bbox, 2, "old"))
    if kind == "unknown-flag":
        original = replace(original, demand=replace(original.demand, levels=(
            replace(original.demand.levels[0], requires_extra=None), *original.demand.levels[1:],
        )))
    elif kind == "bad-mapping":
        original = replace(original, demand=replace(original.demand, levels=(
            replace(original.demand.levels[0], requires_extra=True), *original.demand.levels[1:],
        )))
    elif kind == "unknown-level":
        original = replace(original, demand=replace(original.demand, cells=(
            replace(original.demand.cells[0], level_index=999), *original.demand.cells[1:],
        )))
    elif kind == "duplicate-id":
        original = replace(original, demand=replace(original.demand, cells=(
            original.demand.cells[1], *original.demand.cells[1:],
        )))
    elif kind == "bad-id":
        original = replace(original, demand=replace(original.demand, cells=(
            replace(original.demand.cells[0], id=True), *original.demand.cells[1:],
        )))
    elif kind == "invalid-polygon":
        original = replace(original, demand=replace(original.demand, cells=(
            replace(original.demand.cells[0], poly=((0, 0), (0, 0), (0, 0))),
            *original.demand.cells[1:],
        )))
    elif kind == "reduced-source":
        original = replace(original, meta={"single_cell_preprocessing": {
            "changed_count": 1, "changes": ({"cell_id": 11},),
        }})
    with pytest.raises(ValueError):
        revalidate_source_demand(original, saved)


def test_no_demand_and_no_zones_is_valid_but_not_optimal_or_placement_approved():
    original = _problem((0, 0))
    audit = revalidate_source_demand(original, _solution(original))
    assert audit.evaluation.valid
    assert audit.solution.status is SolutionStatus.FEASIBLE
    assert audit.solution.metrics.total_mass_kg == 0
    assert audit.uncovered_cells == ()


def test_valid_coverage_does_not_hide_an_independent_constraint_failure():
    original = _problem((2, 0))
    original = replace(original, constraints=replace(original.constraints, allow_overcoverage=False))
    zone = build_zone_from_bbox(original, original.demand.bbox, 2, "too-wide")
    audit = revalidate_source_demand(original, _solution(original, zone))
    assert audit.uncovered_cells == ()
    assert not audit.evaluation.valid
    assert audit.solution.status is SolutionStatus.INFEASIBLE
    assert any("избыточно накрыты" in message for message in audit.solution.diagnostics)
