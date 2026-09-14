"""Каталог ограничивает кандидатов, но не обрывает поиск и не удаляет спрос."""

from dataclasses import replace
from importlib import import_module

import pytest

from rebar import Axis, Band, Cell, Direction, Layer, Mosaic, Rebar
from rebar.optimization import (
    AlgorithmRequest,
    GeneticParetoOptimizer,
    LayoutConstraints,
    build_layout_problem,
    evaluate_layout,
)
from rebar.optimization.algorithms.genetic.candidates import layered_geometry_variants
from rebar.optimization.algorithms.genetic.grid_oracle import expand_to_complete_grid_space
from rebar.optimization.algorithms.genetic.recombination import expand_recombined_space
from rebar.optimization.algorithms.genetic_pareto import (
    _build_search_space,
    _layer_bridge_candidates,
)
from rebar.optimization.algorithms.spatial_partition_greedy import (
    SpatialPartitionGreedyOptimizer,
    _build_grid,
    _candidate,
    _initial_rectangles,
)
from rebar.optimization.services import prepare_detailing
from rebar.optimization.services.cutting import (
    PLATE_11700_CATALOG,
    PLATE_11700_CUT_LENGTHS_MM,
    CutLengthCatalog,
    CutLengthInfeasibleError,
    select_installed_length_mm,
)
from rebar.optimization.services.geometry import polygon_bboxes_union_intersection_area


def _problem(axis=Axis.X, levels=(1,) * 48, cell_length=500.0, *, stock=True):
    background = Rebar(300, 12)
    bands = [
        Band(0, 181, "s300d12", 3.77, background, None),
        Band(1, 254, "s300d12+s300d12", 7.54, background, Rebar(300, 12)),
        Band(2, 2, "s300d12+s300d20", 14.24, background, Rebar(300, 20)),
    ]
    cells = []
    for index, level in enumerate(levels):
        start, end = index * cell_length, (index + 1) * cell_length
        bbox = (start, 0, end, 500) if axis is Axis.X else (0, start, 500, end)
        x0, y0, x1, y1 = bbox
        cells.append(Cell(
            [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
            ((x0 + x1) / 2, (y0 + y1) / 2), bands[level].aci, bands[level],
        ))
    span = cell_length * len(levels)
    bounds = (0, 0, span, 500) if axis is Axis.X else (0, 0, 500, span)
    return build_layout_problem(
        Mosaic(Direction(Layer.BOTTOM, axis), cells, bands, bounds),
        LayoutConstraints(
            allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM if stock else (),
            cutting_profile="plate-11700" if stock else "continuous",
        ),
    )


def _space(problem, baselines=("agglomerative", "bsp", "greedy-priority")):
    return _build_search_space(
        problem, AlgorithmRequest(max_details=16),
        candidate_window=3, candidate_trajectories=1, layer_bridge_span=6,
        maximum_merge_reduction=12, maximum_pool_merges=20,
        baseline_seed_algorithms=baselines, random_seed=7, deadline=None,
        candidate_expansion="layered",
    )


def test_catalog_raises_distinct_infeasible_length_without_changing_value_error_contract():
    with pytest.raises(CutLengthInfeasibleError, match="превышает максимум каталога") as caught:
        PLATE_11700_CATALOG.select_length_mm(11701)
    assert isinstance(caught.value, ValueError)
    assert PLATE_11700_CATALOG.select_length_mm(11700) == 11700


@pytest.mark.parametrize("length", (0, -1, float("nan"), float("inf"), -float("inf")))
def test_bad_lengths_are_not_reported_as_optional_candidate_infeasibility(length):
    for select in (PLATE_11700_CATALOG.select_length_mm, select_installed_length_mm):
        with pytest.raises(ValueError) as caught:
            select(length)
        assert not isinstance(caught.value, CutLengthInfeasibleError)


@pytest.mark.parametrize("length", (float("nan"), float("inf")))
def test_catalog_rejects_nonfinite_entries(length):
    with pytest.raises(ValueError) as caught:
        CutLengthCatalog("bad", (length,))
    assert not isinstance(caught.value, CutLengthInfeasibleError)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
@pytest.mark.parametrize("aligned", (True, False))
def test_long_initial_cover_is_split_on_grid_without_losing_any_cell(axis, aligned):
    problem = _problem(axis)
    grid = _build_grid(problem, AlgorithmRequest())
    rectangles = _initial_rectangles(
        problem, grid, prepare_detailing(problem), align_with_bar_axis=aligned,
    )

    assert len(rectangles) == 3
    assert [rectangle.key for rectangle in rectangles] == [0, 1, 2]
    assert [r.zone.installed_length_mm for r in rectangles] == [11700, 11700, 4875]
    assert {cell_id for r in rectangles for cell_id in r.source_cell_ids} == set(range(48))
    # Каждый прямоугольник получает только пересекаемые им исходные КЭ.
    assert [len(r.source_cell_ids) for r in rectangles] == [21, 21, 6]
    for cell in problem.demand.cells:
        assert polygon_bboxes_union_intersection_area(
            cell.poly, tuple(r.zone.demand_bbox for r in rectangles),
        ) == pytest.approx(500 * 500)
    for rectangle in rectangles:
        zone = rectangle.zone
        assert zone.anchored_length_mm == zone.required_length_mm + 80 * zone.rebar.diameter
        assert zone.width_mm >= 2 * 500
        assert zone.installed_length_mm >= zone.anchored_length_mm


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_atomic_grid_cell_too_long_fails_explicitly_without_partial_result(axis):
    problem = _problem(axis, levels=(1,), cell_length=20000)
    grid = _build_grid(problem, AlgorithmRequest())
    with pytest.raises(CutLengthInfeasibleError, match="атом обязательной сетки"):
        _initial_rectangles(problem, grid, prepare_detailing(problem))
    with pytest.raises(CutLengthInfeasibleError, match="атом обязательной сетки"):
        GeneticParetoOptimizer().solve_many(problem)
    assert len(problem.demand.cells) == 1


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_public_ga_covers_full_24m_demand_using_only_stock_lengths(axis):
    problem = _problem(axis)
    original_cells = problem.demand.cells
    request = AlgorithmRequest(max_details=12, params={
        "population_size": 4, "generations": 1, "candidate_trajectories": 1,
        "maximum_pool_merges": 20, "candidate_expansion": "layered",
    })
    solutions = GeneticParetoOptimizer().solve_many(problem, request)
    assert solutions
    for solution in solutions:
        checked = evaluate_layout(problem, solution.zones, request)
        assert checked.valid
        assert checked.metrics.under_reinforced_cell_count == 0
        assert all(z.installed_length_mm in PLATE_11700_CUT_LENGTHS_MM for z in solution.zones)
        assert all(z.installed_length_mm <= 11700 for z in solution.zones)
    assert problem.demand.cells == original_cells
    # Этот тест подтверждает покрытие/длины; сообщения о физических стыках не удалены.
    assert any("40d" in message or "стержн" in message
               for solution in solutions for message in solution.diagnostics)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_spatial_baseline_does_not_abort_when_merge_is_longer_than_stock(axis):
    problem = _problem(axis)
    solution = SpatialPartitionGreedyOptimizer().solve(problem, AlgorithmRequest(max_details=12))
    assert solution.metrics.under_reinforced_cell_count == 0
    assert len(solution.zones) == 3
    assert all(zone.installed_length_mm <= 11700 for zone in solution.zones)


def test_too_small_zone_budget_remains_an_error_with_all_demand_visible():
    problem = _problem()
    solutions = GeneticParetoOptimizer().solve_many(problem, AlgorithmRequest(
        max_details=1, params={"population_size": 4, "generations": 1,
                              "candidate_trajectories": 1, "maximum_pool_merges": 10},
    ))
    assert solutions
    assert all(solution.status.value == "error" for solution in solutions)
    assert all(solution.metrics.under_reinforced_cell_count == 0 for solution in solutions)
    assert all(len(solution.zones) == 3 for solution in solutions)
    assert all(any("max_details=1" in message for message in solution.diagnostics)
               for solution in solutions)


def test_long_optional_merges_bridges_and_layered_envelopes_are_skipped():
    problem = _problem(levels=(1,) * 23 + (2,) * 2 + (1,) * 23)
    space = _space(problem)
    assert space.candidates
    assert all(c.rectangle.zone.installed_length_mm <= 11700 for c in space.candidates)
    assert set().union(*(c.leaf_ids for c in space.candidates)) == set(range(space.leaf_count))
    # BSP начинает с чрезмерного общего bbox: отсутствие этого optional seed
    # не удаляет обязательные атомы, которые покрывает пространственный seed.
    assert "bsp" not in space.baseline_seed_algorithms
    simple = _problem()
    grid = _build_grid(simple, AlgorithmRequest())
    context = prepare_detailing(simple)
    rectangles = _initial_rectangles(simple, grid, context)
    assert _candidate(simple, AlgorithmRequest(), grid, context, rectangles, 0, 1, 3) is None


@pytest.mark.parametrize("target", ("initial", "merge", "bridge", "layered", "baseline"))
def test_non_cutting_errors_are_never_swallowed(monkeypatch, target):
    problem = _problem(levels=(1, 2, 1))
    grid = _build_grid(problem, AlgorithmRequest())
    context = prepare_detailing(problem)
    rectangles = _initial_rectangles(problem, grid, context)

    def invalid(*args, **kwargs):
        raise ValueError("not a stock-length error")

    spatial = import_module("rebar.optimization.algorithms.spatial_partition_greedy")
    genetic = import_module("rebar.optimization.algorithms.genetic_pareto")
    candidates = import_module("rebar.optimization.algorithms.genetic.candidates")
    with pytest.raises(ValueError, match="not a stock-length error"):
        if target == "initial":
            monkeypatch.setattr(spatial, "_make_rectangle", invalid)
            _initial_rectangles(problem, grid, context)
        elif target == "merge":
            monkeypatch.setattr(spatial, "_make_rectangle", invalid)
            _candidate(problem, AlgorithmRequest(), grid, context, rectangles, 0, 1, 10)
        elif target == "bridge":
            monkeypatch.setattr(genetic, "_make_rectangle", invalid)
            _layer_bridge_candidates(problem, grid, context, rectangles, neighbor_span=6)
        elif target == "layered":
            monkeypatch.setattr(candidates, "_make_rectangle", invalid)
            layered_geometry_variants(problem, grid, context, tuple(rectangles), maximum_variants=10)
        else:
            monkeypatch.setattr(genetic.BspOptimizer, "solve", invalid)
            _space(problem, baselines=("bsp",))


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_continuous_profile_keeps_the_original_long_rectangle(axis):
    problem = _problem(axis, stock=False)
    grid = _build_grid(problem, AlgorithmRequest())
    context = prepare_detailing(problem)
    rectangles = _initial_rectangles(problem, grid, context, align_with_bar_axis=True)
    assert len(rectangles) == 1
    assert rectangles[0].zone.required_length_mm == 24000
    assert rectangles[0].zone.installed_length_mm == 24960
    assert rectangles[0].zone.demand_bbox == problem.demand.bbox
    # Каталог, достаточно длинный для исходного bbox, тоже не меняет разбиение.
    large = replace(problem, constraints=replace(
        problem.constraints, allowed_cut_lengths_mm=(24960.0,), cutting_profile="long-test",
    ))
    unchanged = _initial_rectangles(large, grid, prepare_detailing(large), align_with_bar_axis=True)
    assert len(unchanged) == 1
    assert unchanged[0].zone.demand_bbox == rectangles[0].zone.demand_bbox
    assert unchanged[0].zone.mass_kg == rectangles[0].zone.mass_kg


def _only_spatial_candidates(problem):
    space = _space(problem, baselines=())
    return replace(space, candidates=tuple(
        candidate for candidate in space.candidates if "spatial-atom" in candidate.origins
    ), baseline_seed_genomes=(), seed_genomes=())


@pytest.mark.parametrize("expansion", (expand_recombined_space, expand_to_complete_grid_space))
def test_additional_generators_skip_long_proposals_and_preserve_original_candidates(expansion):
    problem = _problem(levels=(1, 2, 1), cell_length=6000)
    space = _only_spatial_candidates(problem)
    expanded = expansion(problem, space)
    assert expanded.candidates[:len(space.candidates)] == space.candidates
    assert all(c.rectangle.zone.installed_length_mm <= 11700 for c in expanded.candidates)
    assert set().union(*(c.leaf_ids for c in expanded.candidates)) == set(range(space.leaf_count))


@pytest.mark.parametrize("target", ("recombination", "grid_oracle"))
def test_additional_generators_do_not_swallow_unrelated_value_errors(monkeypatch, target):
    problem = _problem(levels=(1, 2, 1))
    space = _only_spatial_candidates(problem)
    module = import_module(f"rebar.optimization.algorithms.genetic.{target}")

    def invalid(*args, **kwargs):
        raise ValueError("not a stock-length error")

    if target == "recombination":
        monkeypatch.setattr(module, "build_zone_from_bbox", invalid)
        expand = expand_recombined_space
    else:
        monkeypatch.setattr(module, "_make_rectangle", invalid)
        expand = expand_to_complete_grid_space
    with pytest.raises(ValueError, match="not a stock-length error"):
        expand(problem, space)
