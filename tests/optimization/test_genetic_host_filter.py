"""Opt-in complete candidate guarding, not host-clipped demand or Revit approval."""
from copy import deepcopy
from dataclasses import replace

import pytest
from shapely.geometry import box

from rebar.application.genetic_oracle import small_oracle_problems
from rebar.optimization import AlgorithmRequest, GeneticParetoOptimizer, SolutionStatus, evaluate_layout
from rebar.optimization.algorithms import genetic_pareto as ga
from rebar.optimization.algorithms.genetic import host_filter
from rebar.optimization.algorithms.genetic.coverage import demand_fragments
from rebar.optimization.services import build_zone_from_bbox


def request(**params):
    return AlgorithmRequest(max_details=4, params={"population_size": 4, "generations": 2,
        "random_seed": 7, "local_search_passes": 1, **params})


def space(problem):
    return ga._build_search_space(problem, request(), candidate_window=6, candidate_trajectories=3,
        layer_bridge_span=6, maximum_merge_reduction=12, maximum_pool_merges=5000,
        baseline_seed_algorithms=("agglomerative", "bsp", "greedy-priority"), random_seed=7,
        deadline=None, candidate_expansion="layered", coverage_atoms="demand_fragments")


def normalized(solutions, *, remove_guard=False):
    result = []
    for solution in solutions:
        meta = dict(solution.meta)
        if remove_guard:
            meta.pop("candidate_guard", None)
        result.append(replace(solution, runtime_ms=0, meta=meta))
    return tuple(result)


@pytest.mark.parametrize("problem_index", [0, 1, 4, 12, 13])
def test_default_and_explicit_none_keep_identical_geometry_rng_genomes_meta(problem_index):
    problem = small_oracle_problems()[problem_index]
    old_shape = GeneticParetoOptimizer().solve_many(problem, request())
    explicit_none = GeneticParetoOptimizer(candidate_guard=None).solve_many(problem, request())
    assert normalized(old_shape) == normalized(explicit_none)
    assert all("candidate_guard" not in solution.meta for solution in old_shape)


@pytest.mark.parametrize("params", [{}, {"candidate_expansion": "layered"}, {"recombination_variants": 20}])
def test_always_true_guard_preserves_original_front_and_declares_checks(params):
    problem = small_oracle_problems()[0]
    original_problem = deepcopy(problem)
    unguarded = GeneticParetoOptimizer().solve_many(problem, request(**params))
    checked = GeneticParetoOptimizer(candidate_guard=lambda p,z: True).solve_many(problem, request(**params))
    assert normalized(unguarded) == normalized(checked, remove_guard=True)
    assert problem == original_problem
    for solution in checked:
        info = solution.meta["candidate_guard"]
        assert info["evolution_started"] and not info["source_demand_removed"]
        assert info["coverage"]["all_original_atoms_have_a_candidate"]
        assert info["final_materialization"]["checked_zone_count"] == len(solution.zones)
        assert info["final_materialization"]["passed"]
        assert all(stage["rejected_candidate_count"] == 0 for stage in info["stages"])


def test_none_does_not_call_any_guard_filter_or_final_check(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("default branch called optional guard code")
    monkeypatch.setattr(ga, "filter_candidate_space", forbidden)
    monkeypatch.setattr(ga, "check_materialized_zones", forbidden)
    monkeypatch.setattr(ga, "preserve_atom_boundaries", forbidden)
    solutions = GeneticParetoOptimizer().solve_many(small_oracle_problems()[0], request(recombination_variants=10))
    assert any(solution.status is SolutionStatus.FEASIBLE for solution in solutions)


def test_all_rejected_gives_explicit_full_demand_infeasible_without_evolution(monkeypatch):
    problem = small_oracle_problems()[0]
    original = deepcopy(problem)
    def forbidden(*args, **kwargs):
        raise AssertionError("GA or final polishing must not run on incomplete candidate coverage")
    monkeypatch.setattr(ga, "_evolve", forbidden)
    monkeypatch.setattr(ga, "polish_candidate_pool", forbidden)
    solutions = GeneticParetoOptimizer(candidate_guard=lambda p,z: False).solve_many(problem, request(pool_polish="milp"))
    assert len(solutions) == 1
    result = solutions[0]
    assert result.status is SolutionStatus.INFEASIBLE and result.zones == ()
    assert result.metrics.under_reinforced_cell_count == len(problem.demand.cells)
    info = result.meta["candidate_guard"]
    assert not info["evolution_started"] and not info["global_infeasibility_claimed"]
    assert info["coverage"]["missing_atom_count"] == result.meta["coverage_atom_count"]
    assert info["coverage"]["missing_source_cell_ids"] == sorted(cell.id for cell in problem.demand.cells)
    assert problem == original
    assert "исходные КЭ не удалены" in result.diagnostics[-1]


def test_partial_missing_atoms_not_deleted_or_passed_to_evolution(monkeypatch):
    problem = small_oracle_problems()[0]
    original = space(problem)
    def guard(p,z):
        return z.demand_bbox[2] <= 500
    filtered, info = host_filter.filter_candidate_space(problem, original, guard, stage="test")
    assert filtered.leaves is original.leaves
    assert filtered.grid is original.grid
    assert filtered.leaf_count == original.leaf_count
    assert 0 < info["coverage"]["missing_atom_count"] < original.leaf_count
    def forbidden(*args, **kwargs):
        raise AssertionError("incomplete pool reached evolution")
    monkeypatch.setattr(ga, "_evolve", forbidden)
    result = GeneticParetoOptimizer(candidate_guard=guard).solve(problem, request())
    assert result.status is SolutionStatus.INFEASIBLE
    assert result.meta["candidate_guard"]["coverage"]["missing_atom_ids"]


def test_all_origins_screened_indices_remapped_partial_baselines_not_protected():
    problem = small_oracle_problems()[0]
    original = space(problem)
    origins = ("spatial-atom", "layer-bridge", "merge-alternative", "merge-trajectory",
               "minimal-level-variant", "baseline:bsp", "layered-envelope", "geometric-recombination")
    candidates = tuple(replace(original.candidates[0], rectangle=replace(original.candidates[0].rectangle,
        zone=replace(original.candidates[0].rectangle.zone, id=str(index))), origins=frozenset((origin,)))
        for index,origin in enumerate(origins))
    original = replace(original, candidates=candidates,
        seed_genomes=(frozenset((0,1,2)), frozenset((1,3))),
        baseline_seed_genomes=(frozenset((0,2)), frozenset((3,4))),
        baseline_seed_algorithms=("complete", "partial"),
        baseline_seed_metrics=({"algorithm":"complete"},{"algorithm":"partial"}))
    checked = []
    def guard(problem, zone):
        checked.append(zone.id)
        return int(zone.id)%2 == 0
    filtered, info = host_filter.filter_candidate_space(problem, original, guard, stage="all_origins")
    assert checked == list(map(str,range(8)))
    assert [c.rectangle.zone.id for c in filtered.candidates] == ["0","2","4","6"]
    assert filtered.seed_genomes == (frozenset((0,1)),frozenset())
    assert filtered.baseline_seed_genomes == (frozenset((0,1)),)
    assert filtered.baseline_seed_algorithms == ("complete",)
    assert filtered.baseline_seed_metrics == ({"algorithm":"complete"},)
    assert info["exact_baselines_dropped"] == ["partial"]
    assert info["candidate_index_remap"] == [(0,0),(2,1),(4,2),(6,3)]
    assert info["origin_counts_before"] == dict.fromkeys(origins,1)
    assert all(row["source_cell_ids"] == original.candidates[0].source_cell_ids for row in info["rejected_candidates"])


@pytest.mark.parametrize("failure", [None, 1, "yes", RuntimeError("unknown physical frame")])
def test_guard_unknown_or_nonboolean_is_fail_closed_and_explained(failure):
    def guard(problem, zone):
        if isinstance(failure,Exception):
            raise failure
        return failure
    solution = GeneticParetoOptimizer(candidate_guard=guard).solve(small_oracle_problems()[0], request())
    assert solution.status is SolutionStatus.INFEASIBLE
    stage = solution.meta["candidate_guard"]["stages"][0]
    expected = "guard_exception" if isinstance(failure,Exception) else "guard_returned_non_boolean"
    assert set(stage["reason_counts"]) == {expected}
    assert all(row["reason"] == expected and row["detail"] for row in stage["rejected_candidates"])


@pytest.mark.parametrize("invalid", [1, {}, "host"])
def test_constructor_requires_callable_or_none(invalid):
    with pytest.raises(TypeError,match="candidate_guard"):
        GeneticParetoOptimizer(candidate_guard=invalid)


def test_recombination_new_geometry_cannot_bypass_second_filter(monkeypatch):
    problem = small_oracle_problems()[0]
    real_expansion = ga.expand_recombined_space
    def expansion(*args, **kwargs):
        result = real_expansion(*args, **kwargs)
        candidate = result.candidates[0]
        candidate = replace(candidate, rectangle=replace(candidate.rectangle,
            zone=replace(candidate.rectangle.zone,id="reject-after-recombination")),
            origins=frozenset(("geometric-recombination",)))
        return replace(result,candidates=(*result.candidates,candidate))
    monkeypatch.setattr(ga,"expand_recombined_space",expansion)
    solutions = GeneticParetoOptimizer(candidate_guard=lambda p,z:z.id != "reject-after-recombination").solve_many(
        problem,request(recombination_variants=10))
    for solution in solutions:
        stages = solution.meta["candidate_guard"]["stages"]
        assert len(stages) == 2
        assert stages[1]["rejected_candidate_count"] == 1
        assert stages[1]["rejected_candidates"][0]["zone_id"] == "reject-after-recombination"
        assert all(zone.id != "reject-after-recombination" for zone in solution.zones)


def test_recombination_cannot_coarsen_preexisting_fragment_boundaries():
    problem = small_oracle_problems()[12]
    original = space(problem)
    assert any(atom.bbox[0] == 600 for atom in original.leaves)
    coarse_leaves = demand_fragments(problem, original.grid, ())
    coarsened = replace(original,leaves=coarse_leaves,leaf_count=len(coarse_leaves),
        candidates=tuple(c for c in original.candidates if c.rectangle.zone.demand_bbox[0] != 600))
    refined = host_filter.preserve_atom_boundaries(problem,original,coarsened)
    assert refined.leaves == original.leaves
    assert any(atom.bbox == (500,0,600,500) for atom in refined.leaves)


def test_final_phase_repair_rechecks_actual_axes_even_if_shared_evaluation_passes(monkeypatch):
    problem = small_oracle_problems()[0]
    real_phases = ga.resolve_zone_phases
    def moved(problem,zones,**kwargs):
        return tuple(build_zone_from_bbox(problem,zone.demand_bbox,zone.level_index,zone.id,
            seed_cell_ids=zone.meta["seed_cell_ids"],first_bar_coordinate_mm=zone.first_bar_coordinate_mm+25,
            context=kwargs["context"]) for zone in real_phases(problem,zones,**kwargs))
    monkeypatch.setattr(ga,"resolve_zone_phases",moved)
    configured = request(baseline_seed_algorithms=(), local_search_passes=0)
    # The caller owns an allowed phase lattice; shared detailing/coverage accepts
    # a 25mm translation, but that is not permission to ignore the caller guard.
    solutions = GeneticParetoOptimizer(candidate_guard=lambda p,z:z.first_bar_coordinate_mm % 50 == 0).solve_many(problem,configured)
    assert any(evaluate_layout(problem,s.zones,configured).valid for s in solutions)
    assert all(solution.status is SolutionStatus.ERROR for solution in solutions)
    assert all(not solution.meta["candidate_guard"]["final_materialization"]["passed"] for solution in solutions)
    assert all(solution.meta["candidate_guard"]["stages"][0]["rejected_candidate_count"] == 0 for solution in solutions)


def test_baseline_exact_materialization_is_also_rechecked(monkeypatch):
    problem = small_oracle_problems()[0]
    real_materialize = ga._materialize_genome
    stage = {"final":False}
    def materialize(*args,**kwargs):
        stage["final"] = True
        return real_materialize(*args,**kwargs)
    monkeypatch.setattr(ga,"_materialize_genome",materialize)
    solutions = GeneticParetoOptimizer(candidate_guard=lambda p,z:not stage["final"]).solve_many(problem,request())
    assert any(solution.meta["exact_baseline_seed"] for solution in solutions)
    assert all(solution.status is SolutionStatus.ERROR for solution in solutions)
    assert all(not solution.meta["candidate_guard"]["final_materialization"]["passed"] for solution in solutions)


def test_valid_front_keeps_reasons_for_other_rejected_final_materializations(monkeypatch):
    problem = small_oracle_problems()[0]
    real_materialize = ga._materialize_genome
    calls = []
    def materialize(*args,**kwargs):
        zones, diagnostic = real_materialize(*args,**kwargs)
        calls.append(1)
        if len(calls) == 1:
            zones = tuple(replace(zone,meta={**zone.meta,"caller_guard_rejected":True}) for zone in zones)
        return zones, diagnostic
    monkeypatch.setattr(ga,"_materialize_genome",materialize)
    results = GeneticParetoOptimizer(candidate_guard=lambda p,z:not z.meta.get("caller_guard_rejected",False)).solve_many(
        problem,request())
    assert len(calls) > 1
    assert all(result.status is SolutionStatus.FEASIBLE for result in results)
    for result in results:
        archive = result.meta["candidate_guard"]["archive_materialization"]
        assert archive["checked_solutions"] == len(calls) and archive["rejected_solutions"] == 1
        rejected = archive["rejected_candidates"][0]["final_check"]["rejected_zones"]
        assert rejected and all(row["reason"] == "guard_rejected" for row in rejected)
        assert all(row["source_cell_ids"] for row in rejected)


def test_external_exact_polygon_hole_predicate_not_bbox_host_approximation():
    problem = small_oracle_problems()[0]
    actual_material = box(-10000,-10000,10000,10000).difference(box(700,-200,800,600))
    def guard(p,z):
        return box(*z.bbox).difference(actual_material).area <= 0.001
    solution = GeneticParetoOptimizer(candidate_guard=guard).solve(problem,request())
    assert solution.status is SolutionStatus.INFEASIBLE
    assert solution.meta["candidate_guard"]["coverage"]["missing_source_cell_ids"]
    assert solution.metrics.under_reinforced_cell_count == len(problem.demand.cells)


def test_empty_demand_needs_no_guard_call():
    template = small_oracle_problems()[0]
    problem = replace(template,demand=replace(template.demand,cells=()))
    def forbidden(*args):
        raise AssertionError("empty demand has no candidate or materialized zone")
    result = GeneticParetoOptimizer(candidate_guard=forbidden).solve(problem,request())
    assert result.status is SolutionStatus.OPTIMAL and result.zones == ()
