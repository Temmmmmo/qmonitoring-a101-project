"""Общие поведенческие тесты встроенных алгоритмов."""

import pytest

from rebar import Axis, Band, Cell, Direction, Layer, Mosaic, Rebar
from rebar.dxf_ingest import read_mosaic
from rebar.optimization import (
    AgglomerativeOptimizer,
    AlgorithmRequest,
    BspOptimizer,
    GreedyStripOptimizer,
    LayoutConstraints,
    PriorityGreedyOptimizer,
    SolutionStatus,
    StrongestBBoxOptimizer,
    build_layout_problem,
    built_in_optimizer_registry,
    evaluate_layout,
)


def test_algorithms_share_contract_and_independent_metrics(splittable_mosaic):
    problem = build_layout_problem(
        splittable_mosaic,
        LayoutConstraints(min_width_cells=1, enforce_zone_gap=False),
    )
    request = AlgorithmRequest(max_details=2)

    bbox = StrongestBBoxOptimizer().solve(problem, request)
    bsp = BspOptimizer().solve(problem, request)
    greedy = GreedyStripOptimizer().solve(problem, request)
    priority = PriorityGreedyOptimizer().solve(problem, request)
    agglomerative = AgglomerativeOptimizer().solve(problem, request)

    assert bbox.status is SolutionStatus.FEASIBLE
    assert bbox.metrics.detail_count == 1
    assert bbox.metrics.under_reinforced_cell_count == 0
    assert bsp.status is SolutionStatus.FEASIBLE
    assert bsp.metrics.detail_count == 2
    assert bsp.metrics.total_mass_kg < bbox.metrics.total_mass_kg
    assert evaluate_layout(problem, bsp.zones, request).metrics == bsp.metrics
    assert greedy.status is SolutionStatus.FEASIBLE
    assert greedy.metrics.detail_count == 2
    assert greedy.metrics.total_mass_kg < bbox.metrics.total_mass_kg
    assert evaluate_layout(problem, greedy.zones, request).metrics == greedy.metrics
    assert priority.status is SolutionStatus.FEASIBLE
    assert priority.metrics.detail_count == 2
    assert priority.metrics.total_mass_kg < bbox.metrics.total_mass_kg
    assert {zone.level_index for zone in priority.zones} == {1, 2}
    assert any(
        split["priority_gain_kg"] > 0 for split in priority.meta["split_log"]
    )
    assert evaluate_layout(problem, priority.zones, request).metrics == priority.metrics
    assert agglomerative.status is SolutionStatus.FEASIBLE
    assert agglomerative.metrics.detail_count == 2
    assert agglomerative.metrics.total_mass_kg < bbox.metrics.total_mass_kg
    assert evaluate_layout(problem, agglomerative.zones, request).metrics == (
        agglomerative.metrics
    )


def test_greedy_respects_limit_and_supports_both_axes(splittable_mosaic):
    transposed = Mosaic(
        direction=Direction(Layer.BOTTOM, Axis.Y),
        cells=[
            Cell(
                poly=[(y, x) for x, y in cell.poly],
                centroid=(cell.centroid[1], cell.centroid[0]),
                aci=cell.aci,
                band=cell.band,
            )
            for cell in splittable_mosaic.cells
        ],
        legend=splittable_mosaic.legend,
        bbox=(0, 0, 1200, 4000),
    )

    for mosaic in (splittable_mosaic, transposed):
        problem = build_layout_problem(mosaic, LayoutConstraints(min_width_cells=1))
        solution = GreedyStripOptimizer().solve(problem, AlgorithmRequest(max_details=1))

        assert solution.status is SolutionStatus.FEASIBLE
        assert solution.metrics.detail_count == 1
        assert solution.metrics.under_reinforced_cell_count == 0


def test_built_in_registry_switches_real_algorithms(splittable_mosaic):
    registry = built_in_optimizer_registry()
    problem = build_layout_problem(splittable_mosaic)

    results = {
        name: registry.create(name).solve(problem, AlgorithmRequest(max_details=1))
        for name in registry.names()
    }

    assert registry.names() == (
        "agglomerative",
        "bbox",
        "bsp",
        "greedy",
        "greedy-priority",
    )
    assert set(results) == {
        "agglomerative",
        "bbox",
        "bsp",
        "greedy",
        "greedy-priority",
    }
    assert all(solution.metrics.under_reinforced_cell_count == 0 for solution in results.values())


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
def test_agglomerative_starts_from_components_and_merges_to_limit(axis):
    background = Rebar(step=300, diameter=18)
    levels = [
        Band(0, 181, "s300d18", 8.5, background, None),
        Band(1, 2, "s300d18+s100d20", 40.0, background, Rebar(100, 20)),
    ]
    polygons = [
        [(0, 0), (1000, 0), (1000, 300), (0, 300)],
        [(0, 400), (1000, 400), (1000, 700), (0, 700)],
    ]
    if axis is Axis.Y:
        polygons = [[(y, x) for x, y in polygon] for polygon in polygons]
    cells = [
        Cell(
            poly=polygon,
            centroid=(
                sum(point[0] for point in polygon) / 4,
                sum(point[1] for point in polygon) / 4,
            ),
            aci=2,
            band=levels[1],
        )
        for polygon in polygons
    ]
    mosaic = Mosaic(
        direction=Direction(Layer.BOTTOM, axis),
        cells=cells,
        legend=levels,
        bbox=(0, 0, 1000, 700) if axis is Axis.X else (0, 0, 700, 1000),
    )
    problem = build_layout_problem(mosaic, LayoutConstraints(min_width_cells=1))

    separate = AgglomerativeOptimizer().solve(problem, AlgorithmRequest(max_details=2))
    merged = AgglomerativeOptimizer().solve(problem, AlgorithmRequest(max_details=1))

    assert separate.status is SolutionStatus.FEASIBLE
    assert separate.metrics.detail_count == 2
    assert separate.meta["initial_component_count"] == 2
    assert separate.meta["merge_log"] == []
    assert merged.status is SolutionStatus.FEASIBLE
    assert merged.metrics.detail_count == 1
    assert merged.meta["merge_log"][0]["reason"] == "detail_limit"


def test_real_mosaic_is_accepted_by_every_algorithm(dxf_bottom_x):
    problem = build_layout_problem(read_mosaic(str(dxf_bottom_x)))
    registry = built_in_optimizer_registry()

    for name in registry.names():
        solution = registry.create(name).solve(problem, AlgorithmRequest(max_details=1))
        assert solution.status is SolutionStatus.FEASIBLE
        assert solution.metrics.demanded_cell_count > 0
        assert solution.metrics.under_reinforced_cell_count == 0
