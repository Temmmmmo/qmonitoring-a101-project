"""Общие поведенческие тесты встроенных алгоритмов."""

from rebar.dxf_ingest import read_mosaic
from rebar.optimization import (
    AlgorithmRequest,
    BspOptimizer,
    LayoutConstraints,
    SolutionStatus,
    StrongestBBoxOptimizer,
    built_in_optimizer_registry,
    build_layout_problem,
    evaluate_layout,
)


def test_algorithms_share_contract_and_independent_metrics(splittable_mosaic):
    problem = build_layout_problem(
        splittable_mosaic, LayoutConstraints(min_width_cells=1)
    )
    request = AlgorithmRequest(max_details=2)

    bbox = StrongestBBoxOptimizer().solve(problem, request)
    bsp = BspOptimizer().solve(problem, request)

    assert bbox.status is SolutionStatus.FEASIBLE
    assert bbox.metrics.detail_count == 1
    assert bbox.metrics.under_reinforced_cell_count == 0
    assert bsp.status is SolutionStatus.FEASIBLE
    assert bsp.metrics.detail_count == 2
    assert bsp.metrics.total_mass_kg < bbox.metrics.total_mass_kg
    assert evaluate_layout(problem, bsp.zones, request).metrics == bsp.metrics


def test_built_in_registry_switches_real_algorithms(splittable_mosaic):
    registry = built_in_optimizer_registry()
    problem = build_layout_problem(splittable_mosaic)

    results = {
        name: registry.create(name).solve(problem, AlgorithmRequest(max_details=1))
        for name in registry.names()
    }

    assert registry.names() == ("bbox", "bsp")
    assert set(results) == {"bbox", "bsp"}
    assert all(solution.metrics.under_reinforced_cell_count == 0 for solution in results.values())


def test_real_mosaic_is_accepted_by_every_algorithm(dxf_bottom_x):
    problem = build_layout_problem(read_mosaic(str(dxf_bottom_x)))
    registry = built_in_optimizer_registry()

    for name in registry.names():
        solution = registry.create(name).solve(problem, AlgorithmRequest(max_details=1))
        assert solution.status is SolutionStatus.FEASIBLE
        assert solution.metrics.demanded_cell_count > 0
        assert solution.metrics.under_reinforced_cell_count == 0
