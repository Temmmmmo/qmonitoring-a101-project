"""Ускорение детализации не меняет геометрию и не ослабляет независимую проверку."""

from dataclasses import replace
from importlib import import_module
import random

import pytest

from rebar.application.genetic_oracle import small_oracle_problems
from rebar.optimization import AlgorithmRequest, evaluate_layout
from rebar.optimization.algorithms.genetic_pareto import _build_search_space, _materialize_genome
from rebar.optimization.services import build_zone_from_bbox, prepare_detailing, resolve_zone_phases
from rebar.optimization.services.geometry import polygon_bbox_intersection_area
from rebar.optimization.services.spatial_index import CellSpatialIndex


def _cells():
    template = small_oracle_problems()[0].demand.cells[0]
    cells = []
    for y in range(-5, 5):
        for x in range(-5, 5):
            # Треугольники: bbox служит лишь фильтром, не подменяет площадь полигона.
            poly = ((100 * x, 100 * y), (100 * x + 80, 100 * y), (100 * x, 100 * y + 90))
            cells.append(replace(template, id=1000 - len(cells), poly=poly))
    return tuple(cells)


def test_spatial_candidates_never_drop_a_real_polygon_intersection_and_keep_order():
    cells = _cells()
    index = CellSpatialIndex.build(cells)
    rng = random.Random(7)
    assert len(index.query((0, 0, 10, 10))) < len(cells) // 4
    queries = [(-500, -500, -500 + 1e-7, -500 + 1e-7), (80 + 1e-7, 0, 81, 10)]
    for _ in range(300):
        x, y = rng.uniform(-1000, 1000), rng.uniform(-1000, 1000)
        queries.append((x, y, x + rng.uniform(.001, 800), y + rng.uniform(.001, 800)))
    for box in queries:
        expected = tuple(c for c in cells if polygon_bbox_intersection_area(c.poly, box) > 0)
        actual = tuple(c for c in index.query(box) if polygon_bbox_intersection_area(c.poly, box) > 0)
        assert actual == expected


def test_huge_cells_and_queries_use_bounded_fallback():
    cells = _cells()
    huge = replace(cells[0], id=2000, poly=((-1e9, -1e9), (1e9, -1e9), (1e9, 1e9), (-1e9, 1e9)))
    index = CellSpatialIndex.build((*cells, huge))
    assert index.global_positions == (len(cells),)
    assert len(index.buckets) < 1000
    assert huge in index.query((5e8, 5e8, 5e8 + 10, 5e8 + 10))
    assert index.query((-1e10, -1e10, 1e10, 1e10)) == (*cells, huge)
    assert index.query((float("nan"), 0, 1, 1)) == (*cells, huge)
    assert CellSpatialIndex.build(()).query((0, 0, 1, 1)) == ()


@pytest.mark.parametrize("case", range(14))
def test_indexed_and_full_scan_detailing_match_exactly(case):
    problem = small_oracle_problems()[case]
    context = prepare_detailing(problem)
    legacy = replace(context, spatial_index=None)
    for level in problem.demand.levels:
        if level.requires_extra is not True:
            continue
        for box in (problem.demand.bbox, (-20, -30, 450, 450), (100, 200, 550, 600)):
            indexed = build_zone_from_bbox(problem, box, level.index, "test", context=context)
            reference = build_zone_from_bbox(problem, box, level.index, "test", context=legacy)
            assert indexed == reference
            for phase in (indexed.first_bar_coordinate_mm, indexed.first_bar_coordinate_mm + 10):
                assert build_zone_from_bbox(problem, box, level.index, "test", context=context,
                                             first_bar_coordinate_mm=phase) == build_zone_from_bbox(
                    problem, box, level.index, "test", context=legacy, first_bar_coordinate_mm=phase,
                )


def test_bad_index_cannot_hide_undercoverage_from_hard_validator(monkeypatch):
    problem = small_oracle_problems()[0]
    context = prepare_detailing(problem)
    monkeypatch.setattr(CellSpatialIndex, "query", lambda *args: ())
    zone = build_zone_from_bbox(problem, problem.demand.bbox, 2, "test", context=context)
    assert not zone.covered_cell_ids
    check = evaluate_layout(problem, (zone,), AlgorithmRequest(max_details=3))
    assert not check.valid
    assert any("covered_cell_ids" in message for message in check.diagnostics)


@pytest.mark.parametrize("failed_phase", (False, True))
def test_materialize_collects_coverage_only_after_phase_with_complete_fallback(monkeypatch, failed_phase):
    problem = small_oracle_problems()[0]
    request = AlgorithmRequest(max_details=3)
    space = _build_search_space(
        problem, request, candidate_window=6, candidate_trajectories=3, layer_bridge_span=6,
        maximum_merge_reduction=12, maximum_pool_merges=5000,
        baseline_seed_algorithms=("bsp",), random_seed=7, deadline=None,
    )
    genome = space.baseline_seed_genomes[0]
    space = replace(space, baseline_seed_genomes=())
    context = prepare_detailing(problem)
    legacy = tuple(build_zone_from_bbox(
        problem, space.candidates[i].rectangle.zone.demand_bbox, space.candidates[i].rectangle.level_index,
        f"genetic-{n + 1}", seed_cell_ids=space.candidates[i].source_cell_ids, context=context,
    ) for n, i in enumerate(sorted(genome)))
    expected = legacy if failed_phase else tuple(resolve_zone_phases(problem, legacy, context=context))
    module = import_module("rebar.optimization.algorithms.genetic_pareto")
    calls = []

    def tracked(*args, **kwargs):
        calls.append(kwargs.get("collect_coverage", True))
        return build_zone_from_bbox(*args, **kwargs)

    monkeypatch.setattr(module, "build_zone_from_bbox", tracked)
    if failed_phase:
        def fail(*args, **kwargs):
            raise ValueError("controlled phase failure")
        monkeypatch.setattr(module, "resolve_zone_phases", fail)
    actual, warning = _materialize_genome(problem, space, genome, context)
    assert actual == expected
    assert evaluate_layout(problem, actual, request).valid
    assert calls == [False] * len(genome) + ([True] * len(genome) if failed_phase else [])
    assert (warning is not None) == failed_phase
