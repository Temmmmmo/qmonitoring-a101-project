"""Диагностика резерва поиска: GA против MILP того же конечного пула.

Исследовательская зависимость: scipy>=1.11. Не production-оптимизатор и не
сертификат пригодности к Revit; каждое решение проверяет общий hard-валидатор.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from time import perf_counter

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from rebar.application.analyze_direction import analyze_direction
from rebar.application.genetic_benchmark import GeneticRunConfig
from rebar.optimization import AlgorithmRequest, ComplexityAxis, GeneticParetoOptimizer, evaluate_layout
from rebar.optimization.algorithms.genetic_pareto import (
    _build_search_space, _leaf_ids_for_rectangle, _materialize_genome,
)
from rebar.optimization.services import prepare_detailing
from rebar.optimization.algorithms.genetic.recombination import expand_recombined_space


def solve_pool(space, *, max_zones, time_limit, bar_limit=None):
    rows = [[] for _ in range(space.leaf_count)]
    for index, candidate in enumerate(space.candidates):
        for leaf in candidate.leaf_ids:
            rows[leaf].append(index)
    # Идентичные ограничения покрытия не требуют отдельных строк MILP.
    patterns = tuple(sorted({tuple(row) for row in rows}))
    row_ids, column_ids = [], []
    for row, pattern in enumerate(patterns):
        for column in pattern:
            row_ids.append(row)
            column_ids.append(column)
    matrix = coo_matrix((np.ones(len(row_ids)), (row_ids, column_ids)),
                        shape=(len(patterns), len(space.candidates))).tocsc()
    constraints = [LinearConstraint(matrix, 1, np.inf),
                   LinearConstraint(np.ones(len(space.candidates)), 0, max_zones)]
    if bar_limit is not None:
        constraints.append(LinearConstraint(
            [c.rectangle.zone.bar_count for c in space.candidates], 0, bar_limit,
        ))
    result = milp(
        [c.mass_kg for c in space.candidates], integrality=np.ones(len(space.candidates)),
        bounds=Bounds(0, 1), constraints=constraints,
        options={"time_limit": time_limit, "mip_rel_gap": 1e-7},
    )
    genome = None if result.x is None else frozenset(np.flatnonzero(result.x > 0.5).tolist())
    return genome, {
        "status": int(result.status), "message": result.message,
        "objective": result.fun, "dual_bound": getattr(result, "mip_dual_bound", None),
        "gap": getattr(result, "mip_gap", None), "nodes": getattr(result, "mip_node_count", None),
        "coverage_rows": len(patterns), "bar_limit": bar_limit,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf", nargs="+", type=Path)
    parser.add_argument("--mapping-id", default="plate-zero-d12-v1")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--time-limit", type=float, default=30)
    parser.add_argument("--candidate-expansion", choices=("none", "layered"), default="none")
    parser.add_argument("--recombine", type=int, default=0)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in args.dxf:
        started = perf_counter()
        analysis = analyze_direction(path, mapping_id=args.mapping_id, algorithm_names=("bbox",))
        problem = analysis.problem
        config = GeneticRunConfig(8, 3, args.seed, complexity_axis=ComplexityAxis.PHYSICAL_BAR_COUNT,
                                  operator_policy="uniform", candidate_expansion=args.candidate_expansion)
        request = AlgorithmRequest(max_details=len(problem.demand.cells), params=config.algorithm_params())
        solutions = GeneticParetoOptimizer().solve_many(problem, request)
        valid = [s for s in solutions if evaluate_layout(problem, s.zones, request).valid]
        best = min(valid, key=lambda s: s.metrics.total_mass_kg)
        row = {"path": str(path), "config": config.algorithm_params(),
               "scope": "finite_atom_cover_model_with_independent_hard_validation",
               "recombination_variants_requested": args.recombine, "mip_time_limit_s": args.time_limit,
               "ga_mass_kg": best.metrics.total_mass_kg, "ga_bars": best.metrics.physical_bar_count,
               "ga_zones": best.metrics.detail_count, "ga_runtime_s": (perf_counter() - started)}
        print(row, flush=True)
        space = _build_search_space(
            problem, request, candidate_window=6, candidate_trajectories=3, layer_bridge_span=6,
            maximum_merge_reduction=12, maximum_pool_merges=5000,
            baseline_seed_algorithms=config.baseline_seed_algorithms,
            random_seed=args.seed, deadline=None, candidate_expansion=args.candidate_expansion,
        )
        context = prepare_detailing(problem)
        if args.recombine:
            space = expand_recombined_space(problem, space, maximum_variants=args.recombine)
        row.update(pool_size=len(space.candidates), atoms=space.leaf_count, proposals=[])
        for actual_coverage in (False, True):
            current = replace(space, candidates=tuple(replace(
                c, leaf_ids=_leaf_ids_for_rectangle(c.rectangle, space.leaves),
            ) for c in space.candidates)) if actual_coverage else space
            for bar_limit in (None, best.metrics.physical_bar_count):
                mip_started = perf_counter()
                genome, info = solve_pool(current, max_zones=request.max_details,
                                          time_limit=args.time_limit, bar_limit=bar_limit)
                info.update(actual_coverage=actual_coverage, runtime_s=perf_counter() - mip_started)
                if genome is not None:
                    zones, phase = _materialize_genome(problem, current, genome, context)
                    evaluation = evaluate_layout(problem, zones, request)
                    info.update(valid=evaluation.valid, mass_kg=evaluation.metrics.total_mass_kg,
                                bars=evaluation.metrics.physical_bar_count, zones=evaluation.metrics.detail_count,
                                under_reinforced=evaluation.metrics.under_reinforced_cell_count,
                                errors=[s for s in evaluation.diagnostics if s.startswith("ERROR:")],
                                phase=phase, genome=sorted(genome))
                row["proposals"].append(info)
                print({k: v for k, v in info.items() if k != "genome"}, flush=True)
        rows.append(row)
        (args.out_dir / "bounds.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
