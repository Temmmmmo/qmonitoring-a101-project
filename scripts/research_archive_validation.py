"""Сверить приближённый архив GA с фактической детализацией одного DXF."""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rebar.application.analyze_direction import analyze_direction
from rebar.optimization import AlgorithmRequest, ComplexityAxis, evaluate_layout
from rebar.optimization.algorithms.genetic import MUTATION_OPERATORS, build_operator_policy
from rebar.optimization.algorithms.genetic_pareto import _atomic_leaves, _build_search_space, _evolve, _materialize_genome
from rebar.optimization.algorithms.spatial_partition_greedy import _contains
from rebar.optimization.services.detailing import prepare_detailing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf", type=Path)
    parser.add_argument("--mapping-id", default="plate-zero-d12-v1")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--generations", nargs="+", type=int, default=[3, 12])
    parser.add_argument("--coverage-atoms", choices=("whole_tiles", "demand_fragments"), default="whole_tiles")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    analysis = analyze_direction(args.dxf, mapping_id=args.mapping_id, algorithm_names=("bbox",))
    problem = analysis.problem
    request = AlgorithmRequest(max_details=len(problem.demand.cells))
    space = _build_search_space(
        problem, request, candidate_window=6, candidate_trajectories=3, layer_bridge_span=6,
        maximum_merge_reduction=12, maximum_pool_merges=5000,
        baseline_seed_algorithms=("agglomerative", "bsp", "greedy-priority"),
        random_seed=args.seed, deadline=None,
        coverage_atoms=args.coverage_atoms,
    )
    context = prepare_detailing(problem)
    baseline_audit = []
    whole_tiles = _atomic_leaves(space.grid)
    for genome in space.baseline_seed_genomes:
        original = tuple(space.candidates[index].rectangle.zone for index in sorted(genome))
        retained = []
        for index in sorted(genome):
            rectangle = space.candidates[index].rectangle
            hull = (rectangle.row_start, rectangle.row_end, rectangle.column_start, rectangle.column_end)
            if any(leaf.level_index <= rectangle.level_index and _contains(hull, leaf) for leaf in whole_tiles):
                retained.append(rectangle.zone)
        legacy_check = evaluate_layout(problem, tuple(retained), request)
        current_check = evaluate_layout(problem, original, request)
        baseline_audit.append({
            "original_zones": len(original), "legacy_retained_zones": len(retained),
            "original_mass_kg": current_check.metrics.total_mass_kg,
            "original_valid": current_check.valid, "legacy_valid": legacy_check.valid,
            "legacy_errors": [message for message in legacy_check.diagnostics if message.startswith("ERROR:")],
        })
    print({"baseline_audit": baseline_audit}, flush=True)
    rows = []
    for generations in args.generations:
        population, _, _, _ = _evolve(
            space, detail_limit=request.max_details, population_size=8, generations=generations,
            crossover_rate=0.85, mutation_rate=0.35, complexity_axis=ComplexityAxis.PHYSICAL_BAR_COUNT,
            operator_policy=build_operator_policy("uniform", MUTATION_OPERATORS),
            rng=random.Random(args.seed), deadline=None,
        )
        for individual in population:
            zones, diagnostic = _materialize_genome(problem, space, individual.genome, context)
            evaluation = evaluate_layout(problem, zones, request)
            rows.append({
                "generations": generations, "genome": sorted(individual.genome),
                "approximate_mass_kg": individual.mass_kg, "actual_mass_kg": evaluation.metrics.total_mass_kg,
                "approximate_bars": individual.complexity, "actual_bars": evaluation.metrics.physical_bar_count,
                "valid": evaluation.valid, "diagnostics": evaluation.diagnostics, "phase_diagnostic": diagnostic,
            })
        current = [row for row in rows if row["generations"] == generations]
        print({"generations": generations, "size": len(current),
               "rejected": sum(not row["valid"] for row in current),
               "max_mass_error_kg": max(abs(row["actual_mass_kg"] - row["approximate_mass_kg"]) for row in current)}, flush=True)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "archive.json").write_text(json.dumps({
        "direction": str(problem.demand.direction), "seed": args.seed,
        "pool_size": len(space.candidates), "rows": rows,
        "coverage_atoms": args.coverage_atoms,
        "baseline_audit": baseline_audit,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
