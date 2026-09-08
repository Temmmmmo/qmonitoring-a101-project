"""Последовательный A/B индекса КЭ: один и тот же GA, без wall-clock cutoff/MILP."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rebar.application.analyze_direction import analyze_direction
from rebar.application.genetic_benchmark import GeneticRunConfig
from rebar.optimization import AlgorithmRequest, ComplexityAxis, GeneticParetoOptimizer
from rebar.optimization.services.spatial_index import CellSpatialIndex
from rebar.reporting.serialization import to_jsonable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf", nargs="+", type=Path)
    parser.add_argument("--mapping-id", default="plate-zero-d12-v1")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 5:
        parser.error("repeats должен быть от 1 до 5")
    config = GeneticRunConfig(8, 3, 7, operator_policy="uniform",
                              complexity_axis=ComplexityAxis.PHYSICAL_BAR_COUNT,
                              recombination_variants=1500)
    original_build = CellSpatialIndex.__dict__["build"]
    rows = []
    args.out_dir.mkdir(parents=True, exist_ok=True)
    try:
        for path in args.dxf:
            problem = analyze_direction(path, mapping_id=args.mapping_id, algorithm_names=("bbox",)).problem
            request = AlgorithmRequest(max_details=len(problem.demand.cells), params=config.algorithm_params())
            reference_hash = None
            for repeat in range(args.repeats):
                for indexed in ((False, True) if repeat % 2 == 0 else (True, False)):
                    CellSpatialIndex.build = original_build if indexed else classmethod(lambda cls, cells: None)
                    started = perf_counter()
                    solutions = GeneticParetoOptimizer().solve_many(problem, request)
                    elapsed = perf_counter() - started
                    content = sorted([
                        {"zones": to_jsonable(s.zones), "metrics": to_jsonable(s.metrics),
                         "status": s.status.value, "diagnostics": s.diagnostics}
                        for s in solutions
                    ], key=lambda s: (s["metrics"]["total_mass_kg"], len(s["zones"])))
                    fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True, allow_nan=False).encode()).hexdigest()
                    reference_hash = fingerprint if reference_hash is None else reference_hash
                    row = {"path": str(path), "repeat": repeat, "index": indexed, "runtime_s": elapsed,
                           "solution_count": len(solutions), "result_sha256": fingerprint,
                           "same_as_control": reference_hash == fingerprint,
                           "minimum_mass_kg": min(s.metrics.total_mass_kg for s in solutions)}
                    rows.append(row)
                    print(row, flush=True)
                    (args.out_dir / "profile.json").write_text(json.dumps({
                        "config": config.algorithm_params(),
                        "scope": "sequential_ga_core_without_milp_or_time_cutoff", "rows": rows,
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    if not row["same_as_control"]:
                        raise RuntimeError("пространственный индекс изменил результат")
    finally:
        CellSpatialIndex.build = original_build


if __name__ == "__main__":
    main()
