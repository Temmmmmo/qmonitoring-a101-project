"""Регрессии пропущенных геометрий и границ layered/full-grid расширений."""

from dataclasses import replace
import random

import pytest

from rebar import Axis, Direction, Layer
from rebar.application.genetic_benchmark import GeneticRunConfig
from rebar.application.genetic_oracle import small_oracle_problems
from rebar.optimization import AlgorithmRequest, GeneticParetoOptimizer, evaluate_layout
from rebar.optimization.algorithms.genetic.exact_oracle import solve_exact_candidate_front
from rebar.optimization.algorithms.genetic.coverage import demand_fragments
from rebar.optimization.algorithms.genetic.grid_oracle import expand_to_complete_grid_space
from rebar.optimization.algorithms.genetic_pareto import _build_search_space, _materialize_genome, _repair
from rebar.optimization.services import prepare_detailing


def _weak_envelope_problem():
    problem = small_oracle_problems()[0]
    cells = []
    for index, level in enumerate((1, 1, 2, 1)):
        x = 500 * index
        cells.append(replace(
            problem.demand.cells[0 if level == 1 else 1], id=index,
            poly=((x, 0), (x + 500, 0), (x + 500, 500), (x, 500)),
            centroid=(x + 250, 250),
        ))
    return replace(problem, demand=replace(
        problem.demand, cells=tuple(cells), bbox=(0, 0, 2000, 500),
        direction=Direction(Layer.BOTTOM, Axis.Y),
    ))


def _space(problem, expansion="none", maximum_variants=256, coverage_atoms="whole_tiles"):
    return _build_search_space(
        problem, AlgorithmRequest(max_details=len(problem.demand.cells)),
        candidate_window=6, candidate_trajectories=3, layer_bridge_span=6,
        maximum_merge_reduction=12, maximum_pool_merges=5000,
        baseline_seed_algorithms=("agglomerative", "bsp", "greedy-priority"),
        random_seed=7, deadline=None, candidate_expansion=expansion,
        maximum_layer_variants=maximum_variants,
        coverage_atoms=coverage_atoms,
    )


def _mass_at_budget(front, maximum_zones):
    return min(
        solution.metrics.total_mass_kg for solution in front.solutions
        if len(solution.zones) <= maximum_zones
    )


def test_layered_expansion_recovers_missing_two_zone_tradeoff():
    problem = _weak_envelope_problem()
    original = _space(problem)
    layered = _space(problem, "layered")
    full = expand_to_complete_grid_space(problem, original)
    request = AlgorithmRequest(max_details=4)
    original_front = solve_exact_candidate_front(problem, original, request, max_candidates=40)
    layered_front = solve_exact_candidate_front(problem, layered, request, max_candidates=40)
    full_front = solve_exact_candidate_front(problem, full, request, max_candidates=40)

    assert _mass_at_budget(original_front, 2) == pytest.approx(63.445248)
    assert _mass_at_budget(layered_front, 2) == pytest.approx(41.4406368)
    assert _mass_at_budget(layered_front, 2) == _mass_at_budget(full_front, 2)
    assert _mass_at_budget(layered_front, 4) == _mass_at_budget(original_front, 4)
    assert layered.candidates[:len(original.candidates)] == original.candidates
    assert layered.baseline_seed_genomes == original.baseline_seed_genomes
    weak = next(
        candidate for candidate in layered.candidates[len(original.candidates):]
        if candidate.rectangle.zone.demand_bbox == (0, 0, 2000, 500)
        and candidate.rectangle.level_index == 1
    )
    assert weak.leaf_ids == frozenset((0, 1, 3))
    assert weak.source_cell_ids == (0, 1, 3)


@pytest.mark.parametrize("policy", ["uniform", "ucb1"])
def test_public_ga_uses_layered_geometry_without_weakening_validation(policy):
    problem = _weak_envelope_problem()
    config = GeneticRunConfig(8, 3, 7, operator_policy=policy, candidate_expansion="layered")
    request = AlgorithmRequest(max_details=4, params=config.algorithm_params())

    solutions = GeneticParetoOptimizer().solve_many(problem, request)

    assert min(s.metrics.total_mass_kg for s in solutions if len(s.zones) <= 2) == pytest.approx(41.4406368)
    assert all(evaluate_layout(problem, solution.zones, request).valid for solution in solutions)
    assert all(solution.meta["candidate_expansion"] == "layered" for solution in solutions)
    assert all(solution.meta["candidate_pool_origin_counts"]["layered-envelope"] > 0 for solution in solutions)


def test_layered_limit_is_explicit_and_old_pool_is_not_truncated():
    problem = _weak_envelope_problem()
    original = _space(problem)
    limited = _space(problem, "layered", maximum_variants=1)

    assert len(limited.candidates) == len(original.candidates) + 1
    assert limited.candidates[:len(original.candidates)] == original.candidates


def test_full_grid_oracle_keeps_original_candidates_and_checks_size_limit():
    problem = _weak_envelope_problem()
    original = _space(problem)

    with pytest.raises(ValueError, match="max_grid_tiles"):
        expand_to_complete_grid_space(problem, original, max_grid_tiles=1)
    complete = expand_to_complete_grid_space(problem, original)
    assert complete.candidates[:len(original.candidates)] == original.candidates
    assert expand_to_complete_grid_space(problem, complete) == complete


def test_layered_config_is_explicit_and_identifiable():
    baseline = GeneticRunConfig(8, 3, 7, coverage_atoms="whole_tiles", local_search_passes=0)
    layered = replace(baseline, candidate_expansion="layered", maximum_layer_variants=8)
    assert baseline.algorithm_params()["candidate_expansion"] == "none"
    assert layered.id != baseline.id
    assert layered.id.endswith("layered8")
    with pytest.raises(ValueError, match="candidate_expansion"):
        replace(baseline, candidate_expansion="magic")
    with pytest.raises(ValueError, match="maximum_layer_variants"):
        replace(layered, maximum_layer_variants=0)


def test_baseline_grid_hull_does_not_claim_uncovered_part_of_a_tile():
    template = small_oracle_problems()[0]
    weak, strong = template.demand.cells[:2]
    cells = (
        replace(weak, id=0, poly=((0, 0), (600, 0), (600, 500), (0, 500)), centroid=(300, 250)),
        replace(strong, id=1, poly=((600, 0), (1000, 0), (1000, 500), (600, 500)), centroid=(800, 250)),
    )
    problem = replace(template, demand=replace(
        template.demand, cells=cells, bbox=(0, 0, 1000, 500),
        direction=Direction(Layer.BOTTOM, Axis.X),
    ))
    space = _space(problem)
    strong_index = next(
        index for index, candidate in enumerate(space.candidates)
        if candidate.rectangle.zone.demand_bbox == (600, 0, 1000, 500)
    )
    weak_index = next(
        index for index, candidate in enumerate(space.candidates)
        if candidate.rectangle.zone.demand_bbox == (0, 0, 500, 500)
    )
    # Хвост 500..600 мм не принадлежит сильной baseline-зоне 600..1000 мм.
    assert space.candidates[strong_index].leaf_ids == frozenset()
    request = AlgorithmRequest(max_details=2)
    context = prepare_detailing(problem)
    invalid_genome = frozenset((weak_index, strong_index))
    invalid_zones, _ = _materialize_genome(problem, space, invalid_genome, context)
    assert not evaluate_layout(problem, invalid_zones, request).valid
    repaired = _repair(space, invalid_genome, detail_limit=2, rng=random.Random(7))
    zones, _ = _materialize_genome(problem, space, repaired, context)
    assert evaluate_layout(problem, zones, request).valid
    # Консервативный дискретный учёт не удаляет исходную допустимую раскладку.
    assert any(strong_index in genome for genome in space.baseline_seed_genomes)
    for genome in space.baseline_seed_genomes:
        zones, _ = _materialize_genome(problem, space, genome, context)
        assert evaluate_layout(problem, zones, request).valid

    fragments = _space(problem, coverage_atoms="demand_fragments")
    assert [leaf.bbox for leaf in fragments.leaves] == [
        (0, 0, 500, 500), (500, 0, 600, 500), (600, 0, 1000, 500),
    ]
    assert [leaf.level_index for leaf in fragments.leaves] == [1, 1, 2]
    assert [leaf.source_cell_ids for leaf in fragments.leaves] == [(0,), (0,), (1,)]
    for genome in fragments.baseline_seed_genomes:
        covered = frozenset().union(*(fragments.candidates[index].leaf_ids for index in genome))
        assert covered == frozenset(range(fragments.leaf_count))
    with pytest.raises(ValueError, match="maximum_atoms"):
        demand_fragments(problem, space.grid, ((600, 0, 1000, 500),), maximum_atoms=1)
