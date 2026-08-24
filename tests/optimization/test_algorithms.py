"""Общие поведенческие тесты встроенных алгоритмов."""

import pytest

from rebar import Axis, Band, Cell, Direction, Layer, Mosaic, Rebar
from rebar.dxf_ingest import read_mosaic
from rebar.optimization import (
    AgglomerativeOptimizer,
    AlgorithmRequest,
    BspOptimizer,
    GeneticParetoOptimizer,
    GreedyStripOptimizer,
    LayoutConstraints,
    PriorityGreedyOptimizer,
    RowRunGreedyOptimizer,
    SolutionStatus,
    SpatialPartitionGreedyOptimizer,
    StripProfileDpOptimizer,
    StrongestBBoxOptimizer,
    build_layout_problem,
    built_in_optimizer_registry,
    evaluate_layout,
)
from rebar.optimization.services import bboxes_overlap


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
    row_run = RowRunGreedyOptimizer().solve(problem, request)
    strip_dp = StripProfileDpOptimizer().solve(problem, request)
    spatial = SpatialPartitionGreedyOptimizer().solve(problem, request)

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
    assert row_run.status is SolutionStatus.FEASIBLE
    assert row_run.metrics.detail_count <= 2
    assert row_run.metrics.under_reinforced_cell_count == 0
    assert row_run.meta["initial_run_count"] >= row_run.metrics.detail_count
    assert strip_dp.status is SolutionStatus.FEASIBLE
    assert strip_dp.metrics.detail_count <= 2
    assert strip_dp.metrics.under_reinforced_cell_count == 0
    assert strip_dp.meta["optimal_within_strip_partition_class"] is True
    assert spatial.status is SolutionStatus.FEASIBLE
    assert spatial.metrics.detail_count == 2
    assert spatial.metrics.under_reinforced_cell_count == 0
    assert spatial.meta["initial_rectangle_count"] == 2
    for solution in (
        bbox,
        bsp,
        greedy,
        priority,
        agglomerative,
        row_run,
        strip_dp,
        spatial,
    ):
        assert evaluate_layout(problem, solution.zones, request).valid


def test_genetic_optimizer_returns_reproducible_valid_pareto_candidates(
    splittable_mosaic,
):
    problem = build_layout_problem(
        splittable_mosaic,
        LayoutConstraints(min_width_cells=1, enforce_zone_gap=False),
    )
    request = AlgorithmRequest(
        max_details=8,
        params={
            "population_size": 8,
            "generations": 5,
            "random_seed": 17,
        },
    )

    first = GeneticParetoOptimizer().solve_many(problem, request)
    second = GeneticParetoOptimizer().solve_many(problem, request)

    first_points = [
        (solution.metrics.detail_count, solution.metrics.total_mass_kg)
        for solution in first
    ]
    assert first_points == [
        (solution.metrics.detail_count, solution.metrics.total_mass_kg)
        for solution in second
    ]
    assert first_points
    assert all(solution.algorithm == "genetic-pareto" for solution in first)
    assert all(solution.status is SolutionStatus.FEASIBLE for solution in first)
    assert all(evaluate_layout(problem, solution.zones, request).valid for solution in first)
    assert all(
        not (
            first_mass <= second_mass
            and first_count <= second_count
            and (first_mass < second_mass or first_count < second_count)
        )
        for first_count, first_mass in first_points
        for second_count, second_mass in first_points
        if (first_count, first_mass) != (second_count, second_mass)
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
        "genetic-pareto",
        "greedy",
        "greedy-priority",
        "row-run-greedy",
        "spatial-partition-greedy",
        "strip-profile-dp",
    )
    assert set(results) == {
        "agglomerative",
        "bbox",
        "bsp",
        "genetic-pareto",
        "greedy",
        "greedy-priority",
        "row-run-greedy",
        "spatial-partition-greedy",
        "strip-profile-dp",
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
        max_details = (
            32
            if name in {"genetic-pareto", "spatial-partition-greedy"}
            else 1
        )
        params = (
            {"population_size": 4, "generations": 1}
            if name == "genetic-pareto"
            else {}
        )
        solution = registry.create(name).solve(
            problem,
            AlgorithmRequest(max_details=max_details, params=params),
        )
        assert solution.status is SolutionStatus.FEASIBLE
        assert solution.metrics.demanded_cell_count > 0
        assert solution.metrics.under_reinforced_cell_count == 0


def test_spatial_partition_exposes_nontrivial_real_merge_trajectory(dxf_bottom_x):
    problem = build_layout_problem(read_mosaic(str(dxf_bottom_x)))

    solution = SpatialPartitionGreedyOptimizer().solve(
        problem,
        AlgorithmRequest(max_details=32),
    )

    assert solution.status is SolutionStatus.FEASIBLE
    assert solution.metrics.detail_count > 4
    assert solution.meta["initial_rectangle_count"] > solution.metrics.detail_count
    assert len(solution.meta["merge_trajectory"]) > 2
    assert len(solution.meta["trajectory_pareto_front"]) > 1
    assert solution.metrics.under_reinforced_cell_count == 0
    assert all(
        not bboxes_overlap(first.demand_bbox, second.demand_bbox)
        for index, first in enumerate(solution.zones)
        for second in solution.zones[index + 1 :]
    )


def test_c1_spatial_partition_keeps_a_real_pareto_scale(c1_top_y_dxf, shk_full):
    problem = build_layout_problem(
        read_mosaic(str(c1_top_y_dxf), shk_path=str(shk_full))
    )

    solution = SpatialPartitionGreedyOptimizer().solve(
        problem,
        AlgorithmRequest(max_details=32),
    )

    assert solution.status is SolutionStatus.FEASIBLE
    assert solution.metrics.detail_count == 32
    assert solution.metrics.under_reinforced_cell_count == 0
    assert solution.meta["initial_rectangle_count"] >= 100
    assert len(solution.meta["trajectory_pareto_front"]) >= 5
    assert solution.metrics.total_mass_kg < 7_000
    assert all(
        not bboxes_overlap(first.demand_bbox, second.demand_bbox)
        for index, first in enumerate(solution.zones)
        for second in solution.zones[index + 1 :]
    )


def test_c1_genetic_search_builds_valid_multi_point_front(c1_top_y_dxf, shk_full):
    problem = build_layout_problem(
        read_mosaic(str(c1_top_y_dxf), shk_path=str(shk_full))
    )
    request = AlgorithmRequest(
        max_details=32,
        params={
            "population_size": 8,
            "generations": 5,
            "random_seed": 42,
        },
    )

    solutions = GeneticParetoOptimizer().solve_many(problem, request)
    points = [
        (solution.metrics.detail_count, solution.metrics.total_mass_kg)
        for solution in solutions
    ]

    assert len(points) >= 4
    assert len({count for count, _mass in points}) >= 4
    assert max(count for count, _mass in points) <= 32
    assert all(solution.status is SolutionStatus.FEASIBLE for solution in solutions)
    assert all(solution.metrics.under_reinforced_cell_count == 0 for solution in solutions)


def test_spatial_partition_does_not_merge_through_missing_fe_tile():
    background = Rebar(step=300, diameter=18)
    demand = Band(
        1,
        2,
        "s300d18+s100d20",
        40.0,
        background,
        Rebar(100, 20),
    )
    cells = []
    for row in range(3):
        for column in range(3):
            if (row, column) == (1, 1):
                continue
            xmin = column * 100
            ymin = row * 100
            cells.append(
                Cell(
                    [
                        (xmin, ymin),
                        (xmin + 100, ymin),
                        (xmin + 100, ymin + 100),
                        (xmin, ymin + 100),
                    ],
                    (xmin + 50, ymin + 50),
                    2,
                    demand,
                )
            )
    mosaic = Mosaic(
        direction=Direction(Layer.BOTTOM, Axis.X),
        cells=cells,
        legend=[
            Band(0, 181, "s300d18", 8.5, background, None),
            demand,
        ],
        bbox=(0, 0, 300, 300),
    )
    problem = build_layout_problem(
        mosaic,
        LayoutConstraints(min_width_cells=1, enforce_zone_gap=False),
    )

    solution = SpatialPartitionGreedyOptimizer().solve(
        problem,
        AlgorithmRequest(max_details=1),
    )

    assert solution.status is SolutionStatus.ERROR
    assert solution.meta["forbidden_tile_count"] == 1
    assert solution.metrics.detail_count > 1
    assert all(
        not (
            zone.demand_bbox[0] < 150 < zone.demand_bbox[2]
            and zone.demand_bbox[1] < 150 < zone.demand_bbox[3]
        )
        for zone in solution.zones
    )


def test_spatial_partition_returns_empty_optimum_without_demand(mosaic_with_legend):
    mosaic = Mosaic(
        direction=mosaic_with_legend.direction,
        cells=[mosaic_with_legend.cells[0]],
        legend=mosaic_with_legend.legend,
        bbox=(0, 0, 500, 500),
    )
    problem = build_layout_problem(mosaic)

    solution = SpatialPartitionGreedyOptimizer().solve(problem)

    assert solution.status is SolutionStatus.OPTIMAL
    assert solution.zones == ()
    assert solution.metrics.demanded_cell_count == 0
