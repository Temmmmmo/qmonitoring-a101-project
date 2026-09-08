"""MILP — ограниченный proposal, который не заменяет hard-validation."""

from dataclasses import replace
from importlib import import_module
from time import perf_counter

import pytest

from rebar.application.genetic_benchmark import GeneticRunConfig
from rebar.application.genetic_oracle import small_oracle_problems
from rebar.optimization import AlgorithmRequest, ComplexityAxis, GeneticParetoOptimizer, evaluate_layout
from rebar.optimization.algorithms.genetic.exact_oracle import solve_exact_candidate_front, solution_complexity
from rebar.optimization.algorithms.genetic.local_search import improve_genome
from rebar.optimization.algorithms.genetic.pool_solver import PoolPolishResult, polish_candidate_pool
from rebar.optimization.algorithms.genetic.recombination import expand_recombined_space
from rebar.optimization.algorithms.genetic_pareto import _build_search_space, _materialize_genome
from rebar.optimization.services import prepare_detailing


def space_for(problem):
    return _build_search_space(
        problem, AlgorithmRequest(max_details=len(problem.demand.cells)), candidate_window=6,
        candidate_trajectories=3, layer_bridge_span=6, maximum_merge_reduction=12,
        maximum_pool_merges=5000, baseline_seed_algorithms=("agglomerative", "bsp", "greedy-priority"),
        random_seed=7, deadline=None,
    )


@pytest.mark.parametrize("axis", tuple(ComplexityAxis))
@pytest.mark.parametrize("case", range(14))
@pytest.mark.parametrize("order", ("complexity-first", "mass-first"))
def test_pool_solver_matches_independent_enumeration_at_each_solved_budget(axis, case, order):
    pytest.importorskip("scipy")
    problem = small_oracle_problems()[case]
    space = space_for(problem)
    request = AlgorithmRequest(max_details=len(problem.demand.cells))
    exact = solve_exact_candidate_front(problem, space, request, complexity_axis=axis, max_candidates=40)
    result = polish_candidate_pool(space, complexity_axis=axis, maximum_zones=request.max_details,
                                   maximum_solves=6, time_limit_s=10, budget_order=order)
    context = prepare_detailing(problem)
    assert result.genomes
    for genome in result.genomes:
        zones, _ = _materialize_genome(problem, space, genome, context)
        assert evaluate_layout(problem, zones, request).valid
    for record in result.telemetry["solves"]:
        assert record["runtime_ms"] >= 0
        assert 0 < record["time_limit_s"] <= 10
        if record["objective"] != "mass_kg" or not record["accepted_integer_proposal"]:
            continue
        budget = record["complexity_budget"]
        expected = min(s.metrics.total_mass_kg for s in exact.solutions
                       if budget is None or solution_complexity(s, axis) <= budget)
        assert record["mass_kg"] == pytest.approx(expected, abs=1e-6)
        assert record["status"] == 0


def test_global_exchange_escapes_one_for_many_local_minimum():
    pytest.importorskip("scipy")
    space = space_for(small_oracle_problems()[0])
    template = space.candidates[0]

    def candidate(leaves, mass):
        return replace(template, leaf_ids=frozenset(leaves), rectangle=replace(
            template.rectangle, zone=replace(template.rectangle.zone, mass_kg=mass, bar_count=4),
        ))

    space = replace(space, leaf_count=4, candidates=(
        candidate((0, 1), 10), candidate((2, 3), 10),
        candidate((0, 2), 7), candidate((1, 3), 7),
    ))
    parent = frozenset((0, 1))
    assert improve_genome(space, parent, complexity_axis=ComplexityAxis.ZONE_COUNT,
                          maximum_passes=100).genome == parent
    result = polish_candidate_pool(space, complexity_axis=ComplexityAxis.ZONE_COUNT, maximum_zones=2)
    assert frozenset((2, 3)) in result.genomes
    impossible = polish_candidate_pool(space, complexity_axis=ComplexityAxis.ZONE_COUNT, maximum_zones=1)
    assert not impossible.genomes
    assert all(row["status"] == 2 for row in impossible.telemetry["solves"])


def test_pool_deadline_and_validation():
    space = space_for(small_oracle_problems()[0])
    result = polish_candidate_pool(space, complexity_axis=ComplexityAxis.ZONE_COUNT, maximum_zones=3,
                                   deadline=perf_counter() - 1)
    assert not result.genomes and result.stopped_by_time_limit
    for kwargs in ({"maximum_solves": 2}, {"time_limit_s": float("nan")}, {"time_limit_s": 0},
                   {"budget_order": "unknown"}):
        with pytest.raises(ValueError, match="pool polish"):
            polish_candidate_pool(space, complexity_axis=ComplexityAxis.ZONE_COUNT, maximum_zones=3, **kwargs)
    config = GeneticRunConfig(8, 3, 7, pool_polish="milp", recombination_variants=100)
    assert "milp6x10s-recombine100" in config.id
    with pytest.raises(ValueError, match="pool_polish"):
        replace(config, pool_polish="magic")
    with pytest.raises(ValueError, match="recombination"):
        replace(config, coverage_atoms="whole_tiles")


def test_invalid_mip_proposal_does_not_displace_valid_ga_parents(monkeypatch):
    module = import_module("rebar.optimization.algorithms.genetic_pareto")
    problem = small_oracle_problems()[0]
    config = GeneticRunConfig(8, 3, 7)
    baseline = GeneticParetoOptimizer().solve_many(problem, AlgorithmRequest(max_details=3, params=config.algorithm_params()))
    monkeypatch.setattr(module, "polish_candidate_pool", lambda *a, **k: PoolPolishResult(
        (frozenset((0,)),), {"proposal_count": 1}, False,
    ))
    request = AlgorithmRequest(max_details=3, params=replace(config, pool_polish="milp").algorithm_params())
    actual = GeneticParetoOptimizer().solve_many(problem, request)
    def points(solutions):
        return {(s.metrics.detail_count, round(s.metrics.total_mass_kg, 6)) for s in solutions}
    assert points(actual) == points(baseline)
    assert all(evaluate_layout(problem, s.zones, request).valid for s in actual)
    assert actual[0].meta["internal_hard_rejection_count"] > 0


def test_recombination_preserves_parents_geometry_and_recomputes_exact_fragment_coverage():
    problem = small_oracle_problems()[1]
    space = space_for(problem)
    actual = expand_recombined_space(problem, space, maximum_variants=30)
    assert actual.baseline_seed_genomes == space.baseline_seed_genomes
    assert [c.rectangle for c in actual.candidates[:len(space.candidates)]] == [c.rectangle for c in space.candidates]
    assert actual == expand_recombined_space(problem, space, maximum_variants=30)
    assert len(actual.candidates) <= len(space.candidates) + 30
    assert len(actual.candidates) > len(space.candidates)
    for candidate in actual.candidates:
        bbox = candidate.rectangle.zone.demand_bbox
        for leaf_id in candidate.leaf_ids:
            leaf = actual.leaves[leaf_id]
            assert leaf.level_index <= candidate.rectangle.level_index
            assert bbox[0] <= leaf.bbox[0] + 1e-6 and bbox[2] >= leaf.bbox[2] - 1e-6
            assert bbox[1] <= leaf.bbox[1] + 1e-6 and bbox[3] >= leaf.bbox[3] - 1e-6
    assert expand_recombined_space(problem, space, maximum_variants=0) is space
    with pytest.raises(ValueError, match="скрещивания"):
        expand_recombined_space(problem, space, maximum_variants=-1)


@pytest.mark.parametrize("kind", ("missing", "fractional", "out_of_bounds", "uncovered"))
def test_solver_limit_does_not_turn_invalid_vector_into_integer_proposal(monkeypatch, kind):
    np = pytest.importorskip("numpy")
    scipy_optimize = pytest.importorskip("scipy.optimize")
    space = space_for(small_oracle_problems()[0])
    vectors = {"missing": None, "fractional": np.full(len(space.candidates), .5),
               "out_of_bounds": np.full(len(space.candidates), 2.0),
               "uncovered": np.zeros(len(space.candidates))}
    monkeypatch.setattr(scipy_optimize, "milp", lambda *a, **k: scipy_optimize.OptimizeResult(
        status=1, x=vectors[kind], fun=None, mip_dual_bound=None, mip_gap=None,
    ))
    result = polish_candidate_pool(space, complexity_axis=ComplexityAxis.ZONE_COUNT, maximum_zones=3)
    assert not result.genomes and result.stopped_by_time_limit
    assert all(not r["accepted_integer_proposal"] for r in result.telemetry["solves"])


def test_missing_optional_dependency_is_explicit(monkeypatch):
    import sys

    space = space_for(small_oracle_problems()[0])
    monkeypatch.setitem(sys.modules, "scipy", None)
    with pytest.raises(ValueError, match="требует SciPy"):
        polish_candidate_pool(space, complexity_axis=ComplexityAxis.ZONE_COUNT, maximum_zones=3)


def test_budget_order_reverses_only_interior_points_with_the_same_limits():
    pytest.importorskip("scipy")
    space = space_for(small_oracle_problems()[0])
    template = space.candidates[0]
    candidates = tuple(replace(template, leaf_ids=frozenset((0,)), rectangle=replace(
        template.rectangle, zone=replace(template.rectangle.zone, mass_kg=12 - bars, bar_count=bars),
    )) for bars in (2, 4, 6, 8, 10))
    space = replace(space, candidates=candidates, leaf_count=1)
    results = [polish_candidate_pool(space, complexity_axis=ComplexityAxis.PHYSICAL_BAR_COUNT,
                                     maximum_zones=1, budget_order=order)
               for order in ("complexity-first", "mass-first")]
    budgets = [[r["complexity_budget"] for r in result.telemetry["solves"]] for result in results]
    assert budgets == [[None, None, 2, 4, 6, 8], [None, None, 2, 8, 6, 4]]
    assert set(results[0].genomes) == set(results[1].genomes)
