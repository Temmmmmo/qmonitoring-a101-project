"""Проверить GA точным перебором на открытых малых масках без локального датасета."""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rebar.application.genetic_benchmark import GeneticRunConfig
from rebar.application.genetic_oracle import compare_genetic_with_oracle, small_oracle_problems
from rebar.optimization import ComplexityAxis
from rebar.reporting.genetic_oracle import generate_genetic_oracle_report


def main() -> None:
    problems = {problem.case_id: problem for problem in small_oracle_problems()}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", nargs="+", choices=tuple(problems), default=tuple(problems))
    parser.add_argument("--population-size", type=int, default=8)
    parser.add_argument("--generations", type=int, default=5)
    parser.add_argument("--local-search-passes", type=int, default=4)
    parser.add_argument("--seeds", nargs="+", type=int, default=[7, 17, 42])
    parser.add_argument("--operator-policies", nargs="+", choices=("uniform", "ucb1"),
                        default=["uniform", "ucb1"])
    parser.add_argument("--complexity-axes", nargs="+", choices=tuple(axis.value for axis in ComplexityAxis),
                        default=[axis.value for axis in ComplexityAxis])
    parser.add_argument("--max-candidates", type=int, default=24)
    parser.add_argument("--max-subsets", type=int, default=100_000)
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "artifacts" / "genetic_oracle")
    args = parser.parse_args()
    combinations = tuple(itertools.product(
        args.cases, args.seeds, args.operator_policies, args.complexity_axes,
    ))
    if len(combinations) > 512:
        parser.error("не больше 512 запусков за один вызов")
    runs = []
    for case_id, seed, policy, axis in combinations:
        config = GeneticRunConfig(
            args.population_size, args.generations, seed,
            operator_policy=policy, complexity_axis=ComplexityAxis(axis),
            local_search_passes=args.local_search_passes,
        )
        try:
            run = compare_genetic_with_oracle(
                problems[case_id], config,
                max_candidates=args.max_candidates, max_subsets=args.max_subsets,
            )
        except ValueError as error:
            parser.error(f"{case_id} / {config.id}: {error}; точный отчёт не сформирован")
        runs.append(run)
    report = generate_genetic_oracle_report(tuple(runs), args.out_dir)
    print(f"Готово: {report} ({len(runs)} запусков)")


if __name__ == "__main__":
    main()
