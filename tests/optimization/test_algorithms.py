"""Общие поведенческие тесты встроенных алгоритмов."""

from rebar import Axis, Cell, Direction, Layer, Mosaic
from rebar.dxf_ingest import read_mosaic
from rebar.optimization import (
    AlgorithmRequest,
    BspOptimizer,
    GreedyStripOptimizer,
    LayoutConstraints,
    PriorityGreedyOptimizer,
    SolutionStatus,
    StrongestBBoxOptimizer,
    built_in_optimizer_registry,
    build_layout_problem,
    evaluate_layout,
)


def test_algorithms_share_contract_and_independent_metrics(splittable_mosaic):
    problem = build_layout_problem(splittable_mosaic, LayoutConstraints(min_width_cells=1))
    request = AlgorithmRequest(max_details=2)

    bbox = StrongestBBoxOptimizer().solve(problem, request)
    bsp = BspOptimizer().solve(problem, request)
    greedy = GreedyStripOptimizer().solve(problem, request)
    priority = PriorityGreedyOptimizer().solve(problem, request)

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
        bbox=(0, 0, 1000, 4000),
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

    assert registry.names() == ("bbox", "bsp", "greedy", "greedy-priority")
    assert set(results) == {"bbox", "bsp", "greedy", "greedy-priority"}
    assert all(solution.metrics.under_reinforced_cell_count == 0 for solution in results.values())


def test_real_mosaic_is_accepted_by_every_algorithm(dxf_bottom_x):
    problem = build_layout_problem(read_mosaic(str(dxf_bottom_x)))
    registry = built_in_optimizer_registry()

    for name in registry.names():
        solution = registry.create(name).solve(problem, AlgorithmRequest(max_details=1))
        assert solution.status is SolutionStatus.FEASIBLE
        assert solution.metrics.demanded_cell_count > 0
        assert solution.metrics.under_reinforced_cell_count == 0
