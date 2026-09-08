"""Запустить сетку параметров GA на четырёх DXF и сравнить с golden-case."""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rebar.application import (
    GeneticRunConfig,
    PlateDirectionSource,
    run_genetic_benchmark,
)
from rebar.golden import (
    get_engineer_reference_case,
    get_golden_case,
    plate_metric_reference_from_engineer,
)
from rebar.optimization import ComplexityAxis
from rebar.reporting import generate_genetic_benchmark_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf", nargs=4, type=Path, help="четыре DXF одного комплекта")
    parser.add_argument("--mapping-id", default="plate-zero-d12-v1")
    parser.add_argument("--shk", type=Path, help="общий совместимый .shk вместо mapping")
    parser.add_argument("--reference-id", default="plate-zero-k09")
    parser.add_argument(
        "--engineer-reference-id",
        help="проверенная plate-level PDF-метка из каталога инженерских выдач",
    )
    parser.add_argument(
        "--reference-data-dir",
        type=Path,
        default=REPO_ROOT / "Дополнительные материалы",
        help="корень локальных материалов для встраивания PNG и листов golden PDF",
    )
    parser.add_argument("--population-sizes", nargs="+", type=int, default=[16])
    parser.add_argument("--generations", nargs="+", type=int, default=[30])
    parser.add_argument("--seeds", nargs="+", type=int, default=[42])
    parser.add_argument("--crossover-rates", nargs="+", type=float, default=[0.85])
    parser.add_argument("--mutation-rates", nargs="+", type=float, default=[0.35])
    parser.add_argument("--candidate-windows", nargs="+", type=int, default=[6])
    parser.add_argument("--candidate-trajectories", nargs="+", type=int, default=[3])
    parser.add_argument("--layer-bridge-spans", nargs="+", type=int, default=[6])
    parser.add_argument(
        "--maximum-merge-reductions",
        nargs="+",
        type=int,
        default=[12],
    )
    parser.add_argument("--maximum-pool-merges", type=int, default=5_000)
    parser.add_argument(
        "--candidate-expansions", nargs="+", choices=("none", "layered"), default=["none"],
    )
    parser.add_argument("--maximum-layer-variants", type=int, default=256)
    parser.add_argument("--local-search-passes", nargs="+", type=int, default=[4])
    parser.add_argument("--coverage-atoms", choices=("whole_tiles", "demand_fragments"), default="demand_fragments")
    parser.add_argument("--pool-polish", nargs="+", choices=("none", "milp"), default=["none"])
    parser.add_argument("--pool-polish-solves", type=int, default=6)
    parser.add_argument("--pool-polish-time-s", type=float, default=10)
    parser.add_argument("--recombination-variants", nargs="+", type=int, default=[0])
    parser.add_argument(
        "--operator-policies",
        nargs="+",
        choices=("uniform", "ucb1"),
        default=["ucb1"],
        help="uniform — контроль; ucb1 — адаптивный выбор предметных мутаций",
    )
    parser.add_argument(
        "--ucb-explorations",
        nargs="+",
        type=float,
        default=[2**0.5],
    )
    parser.add_argument(
        "--baseline-seed-algorithms",
        nargs="*",
        default=["agglomerative", "bsp", "greedy-priority"],
    )
    parser.add_argument(
        "--complexity-axis",
        choices=tuple(item.value for item in ComplexityAxis),
        default=ComplexityAxis.ZONE_COUNT.value,
    )
    parser.add_argument("--min-width-cells", type=int, default=2)
    parser.add_argument("--cutting-profile", default="continuous")
    parser.add_argument("--case-id", default="genetic-benchmark")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_ROOT / "artifacts" / "genetic_benchmark",
    )
    args = parser.parse_args()

    if args.shk is not None and args.mapping_id.strip().casefold() != "auto":
        raise SystemExit("При --shk нужно явно передать --mapping-id auto")

    combinations = tuple(
        itertools.product(
            args.population_sizes,
            args.generations,
            args.seeds,
            args.crossover_rates,
            args.mutation_rates,
            args.candidate_windows,
            args.candidate_trajectories,
            args.layer_bridge_spans,
            args.maximum_merge_reductions,
            args.operator_policies,
            args.ucb_explorations,
            args.candidate_expansions,
            args.local_search_passes,
            args.pool_polish,
            args.recombination_variants,
        )
    )
    if len(combinations) > 100:
        raise SystemExit(
            f"Сетка содержит {len(combinations)} запусков; максимум за один вызов — 100"
        )
    axis = ComplexityAxis(args.complexity_axis)
    configs = tuple(
        GeneticRunConfig(
            population_size=population,
            generations=generations,
            random_seed=seed,
            crossover_rate=crossover,
            mutation_rate=mutation,
            candidate_window=window,
            candidate_trajectories=trajectories,
            layer_bridge_span=bridge_span,
            maximum_merge_reduction=merge_reduction,
            maximum_pool_merges=args.maximum_pool_merges,
            complexity_axis=axis,
            operator_policy=operator_policy,
            ucb_exploration=ucb_exploration,
            baseline_seed_algorithms=tuple(args.baseline_seed_algorithms),
            candidate_expansion=candidate_expansion,
            maximum_layer_variants=args.maximum_layer_variants,
            local_search_passes=local_search_passes,
            coverage_atoms=args.coverage_atoms,
            pool_polish=pool_polish,
            pool_polish_solves=args.pool_polish_solves,
            pool_polish_time_s=args.pool_polish_time_s,
            recombination_variants=recombination_variants,
        )
        for (
            population,
            generations,
            seed,
            crossover,
            mutation,
            window,
            trajectories,
            bridge_span,
            merge_reduction,
            operator_policy,
            ucb_exploration,
            candidate_expansion,
            local_search_passes,
            pool_polish,
            recombination_variants,
        ) in combinations
    )
    sources = tuple(
        PlateDirectionSource(
            path,
            shk_path=args.shk,
            mapping_id=args.mapping_id,
        )
        for path in args.dxf
    )
    completed = []
    for index, config in enumerate(configs, 1):
        result, = run_genetic_benchmark(
            sources, (config,), min_width_cells=args.min_width_cells,
            cutting_profile=args.cutting_profile, case_id=args.case_id,
        )
        completed.append(result)
        print(f"[{index}/{len(configs)}] {config.id}", flush=True)
    results = tuple(completed)
    reference = (
        plate_metric_reference_from_engineer(
            get_engineer_reference_case(args.engineer_reference_id)
        )
        if args.engineer_reference_id
        else (get_golden_case(args.reference_id) if args.reference_id else None)
    )
    report = generate_genetic_benchmark_report(
        results,
        args.out_dir,
        reference=reference,
        reference_data_dir=(
            args.reference_data_dir
            if reference is not None and not args.engineer_reference_id
            else None
        ),
    )
    print(f"Готово: {report}")


if __name__ == "__main__":
    main()
