"""Локальные замены — только предложения с сохранением покрытия и родителей."""

from dataclasses import replace
from importlib import import_module
from time import perf_counter

import pytest

from rebar.application.genetic_benchmark import GeneticRunConfig
from rebar.application.genetic_oracle import small_oracle_problems
from rebar.optimization import AlgorithmRequest, ComplexityAxis, GeneticParetoOptimizer, evaluate_layout
from rebar.optimization.algorithms.genetic.local_search import LocalSearchResult, improve_genome
from rebar.optimization.algorithms.genetic_pareto import _build_search_space


def _toy_space():
    problem = small_oracle_problems()[0]
    space = _build_search_space(
        problem, AlgorithmRequest(max_details=4), candidate_window=6,
        candidate_trajectories=3, layer_bridge_span=6, maximum_merge_reduction=12,
        maximum_pool_merges=5000, baseline_seed_algorithms=(), random_seed=7,
        deadline=None,
    )
    template = space.candidates[0]

    def candidate(leaves, mass, bars):
        return replace(
            template, leaf_ids=frozenset(leaves), rectangle=replace(
                template.rectangle,
                zone=replace(template.rectangle.zone, mass_kg=mass, bar_count=bars),
            ),
        )

    # Стоимости намеренно искусственные: проверяется дискретный proposal, не детализация.
    return replace(space, candidates=(
        candidate((0,), 10, 3), candidate((1,), 10, 3), candidate((0, 1), 15, 5),
        candidate((2,), 10, 3), candidate((3,), 10, 3), candidate((2, 3), 16, 5),
    ))


@pytest.mark.parametrize("axis", tuple(ComplexityAxis))
def test_local_moves_preserve_atom_coverage_and_both_objectives(axis):
    space = _toy_space()
    original = frozenset((0, 1, 3, 4))
    one = improve_genome(space, original, complexity_axis=axis, maximum_passes=1)
    two = improve_genome(space, original, complexity_axis=axis, maximum_passes=2)

    assert one.genome == frozenset((2, 3, 4))
    assert two.genome == frozenset((2, 5))
    assert one.moves == 1 and two.moves == 2
    assert two.candidate_checks > one.candidate_checks > 0
    for result in (one, two):
        assert frozenset().union(*(space.candidates[i].leaf_ids for i in result.genome)) == frozenset(range(4))
        assert sum(space.candidates[i].mass_kg for i in result.genome) < 40
        assert sum(space.candidates[i].rectangle.zone.bar_count for i in result.genome) < 12
        assert not result.stopped_by_time_limit


def test_local_search_respects_disabled_mode_deadline_and_input_validation():
    space = _toy_space()
    genome = frozenset((0, 1))
    disabled = improve_genome(space, genome, complexity_axis=ComplexityAxis.ZONE_COUNT, maximum_passes=0)
    assert disabled == LocalSearchResult(genome, 0, 0, False)
    timed = improve_genome(
        space, genome, complexity_axis=ComplexityAxis.ZONE_COUNT,
        maximum_passes=4, deadline=perf_counter() - 1,
    )
    assert timed == LocalSearchResult(genome, 0, 0, True)
    with pytest.raises(ValueError, match="maximum_passes"):
        improve_genome(space, genome, complexity_axis=ComplexityAxis.ZONE_COUNT, maximum_passes=-1)
    with pytest.raises(ValueError, match="индекс"):
        improve_genome(space, frozenset((99,)), complexity_axis=ComplexityAxis.ZONE_COUNT, maximum_passes=1)


def test_physical_bar_axis_rejects_mass_saving_with_more_bars():
    space = _toy_space()
    candidate = space.candidates[2]
    changed = replace(candidate, rectangle=replace(
        candidate.rectangle, zone=replace(candidate.rectangle.zone, bar_count=7),
    ))
    space = replace(space, candidates=(*space.candidates[:2], changed, *space.candidates[3:]))
    genome = frozenset((0, 1))
    assert improve_genome(
        space, genome, complexity_axis=ComplexityAxis.PHYSICAL_BAR_COUNT, maximum_passes=4,
    ).genome == genome
    assert improve_genome(
        space, genome, complexity_axis=ComplexityAxis.ZONE_COUNT, maximum_passes=4,
    ).genome == frozenset((2,))


def test_invalid_local_proposal_cannot_displace_valid_ga_parent(monkeypatch):
    module = import_module("rebar.optimization.algorithms.genetic_pareto")
    problem = small_oracle_problems()[0]
    config = GeneticRunConfig(8, 3, 7, coverage_atoms="whole_tiles", local_search_passes=0)
    baseline = GeneticParetoOptimizer().solve_many(problem, AlgorithmRequest(max_details=4, params=config.algorithm_params()))

    def invalid_proposal(space, genome, **kwargs):
        return LocalSearchResult(frozenset(), 1, 1, False)

    monkeypatch.setattr(module, "improve_genome", invalid_proposal)
    local = replace(config, local_search_passes=4)
    request = AlgorithmRequest(max_details=4, params=local.algorithm_params())
    actual = GeneticParetoOptimizer().solve_many(problem, request)

    def points(solutions):
        return {(s.metrics.detail_count, round(s.metrics.total_mass_kg, 6)) for s in solutions}

    assert points(actual) == points(baseline)
    assert all(evaluate_layout(problem, solution.zones, request).valid for solution in actual)
    assert actual[0].meta["local_search_totals"]["moves"] > 0
    with pytest.raises(ValueError, match="local_search_passes"):
        replace(config, local_search_passes=-1)
    assert local.id.endswith("ls4")
