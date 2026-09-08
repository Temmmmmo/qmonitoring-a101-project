"""Парное исследование порядка epsilon-MILP на одном и том же конечном пуле.

Не сквозной GA benchmark: измеряется только pool polish, затем все предложения
независимо материализуются и проверяются. Общие baseline-родители сохраняются.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rebar.application.analyze_direction import analyze_direction
from rebar.optimization import AlgorithmRequest, ComplexityAxis, evaluate_layout
from rebar.optimization.algorithms.genetic.pool_solver import polish_candidate_pool
from rebar.optimization.algorithms.genetic.recombination import expand_recombined_space
from rebar.optimization.algorithms.genetic_pareto import _build_search_space, _materialize_genome
from rebar.optimization.services import prepare_detailing
from rebar.reporting.genetic_benchmark import normalized_point_hypervolume, point_hypervolume_bounds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf", nargs="+", type=Path)
    parser.add_argument("--mapping-id", default="plate-zero-d12-v1")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--time-limit", type=float, default=10)
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in args.dxf:
        problem = analyze_direction(path, mapping_id=args.mapping_id, algorithm_names=("bbox",)).problem
        request = AlgorithmRequest(max_details=len(problem.demand.cells))
        space = _build_search_space(
            problem, request, candidate_window=6, candidate_trajectories=3, layer_bridge_span=6,
            maximum_merge_reduction=12, maximum_pool_merges=5000,
            baseline_seed_algorithms=("agglomerative", "bsp", "greedy-priority"),
            random_seed=args.seed, deadline=None,
        )
        space = expand_recombined_space(problem, space, maximum_variants=1500)
        context = prepare_detailing(problem)
        signature = [(c.rectangle.zone.demand_bbox, c.rectangle.level_index, c.mass_kg,
                      c.rectangle.zone.bar_count, sorted(c.leaf_ids)) for c in space.candidates]
        pool_hash = hashlib.sha256(json.dumps(signature).encode()).hexdigest()

        def check(genomes):
            points, rejected = [], 0
            for genome in genomes:
                zones, _ = _materialize_genome(problem, space, genome, context)
                evaluation = evaluate_layout(problem, zones, request)
                if not evaluation.valid:
                    rejected += 1
                    continue
                metrics = evaluation.metrics
                points.append((metrics.total_mass_kg, metrics.physical_bar_count))
            return points, rejected

        parents, parent_rejected = check(space.baseline_seed_genomes)
        for repeat in range(args.repeats):
            orders = ("complexity-first", "mass-first")
            pair = []
            for order in (orders if repeat % 2 == 0 else orders[::-1]):
                result = polish_candidate_pool(
                    space, complexity_axis=ComplexityAxis.PHYSICAL_BAR_COUNT,
                    maximum_zones=request.max_details, maximum_solves=6,
                    time_limit_s=args.time_limit, budget_order=order,
                )
                points, rejected = check(result.genomes)
                row = {"path": str(path), "seed": args.seed, "repeat": repeat, "order": order,
                       "pool_sha256": pool_hash, "points": sorted(set(parents + points)),
                       "checked_proposals": len(result.genomes), "rejected_proposals": rejected,
                       "checked_parents": len(space.baseline_seed_genomes), "rejected_parents": parent_rejected,
                       "telemetry": result.telemetry}
                pair.append(row)
                print({k: v for k, v in row.items() if k not in {"points", "telemetry"}}, flush=True)
            bounds = point_hypervolume_bounds(p for row in pair for p in row["points"])
            for row in pair:
                row["joint_hypervolume_bounds"] = bounds
                row["hypervolume"] = normalized_point_hypervolume(row["points"], bounds)
                row["minimum_mass_kg"] = min((p[0] for p in row["points"]), default=None)
            rows.extend(pair)
            (args.out_dir / "schedule.json").write_text(json.dumps({
                "scope": "pool_polish_only_not_full_ga_or_global_front",
                "rows": rows,
            }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
