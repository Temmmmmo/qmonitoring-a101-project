"""Воспроизводимые серии запусков генетического оптимизатора."""

from __future__ import annotations

import math
from dataclasses import dataclass

from rebar.optimization import ComplexityAxis

from .analyze_plate import PlateAnalysis, PlateDirectionSource, analyze_plate


@dataclass(frozen=True)
class GeneticRunConfig:
    """Одна явно зафиксированная конфигурация эксперимента."""

    population_size: int
    generations: int
    random_seed: int
    crossover_rate: float = 0.85
    mutation_rate: float = 0.35
    candidate_window: int = 6
    candidate_trajectories: int = 3
    layer_bridge_span: int = 6
    maximum_merge_reduction: int = 12
    maximum_pool_merges: int = 5_000
    complexity_axis: ComplexityAxis = ComplexityAxis.ZONE_COUNT
    operator_policy: str = "ucb1"
    ucb_exploration: float = math.sqrt(2.0)
    baseline_seed_algorithms: tuple[str, ...] = (
        "agglomerative",
        "bsp",
        "greedy-priority",
    )

    def __post_init__(self) -> None:
        if self.population_size < 4:
            raise ValueError("population_size должен быть не меньше 4")
        if self.generations < 1:
            raise ValueError("generations должен быть не меньше 1")
        if self.random_seed < 0:
            raise ValueError("random_seed не может быть отрицательным")
        if not 0.0 <= self.crossover_rate <= 1.0:
            raise ValueError("crossover_rate должен быть от 0 до 1")
        if not 0.0 <= self.mutation_rate <= 1.0:
            raise ValueError("mutation_rate должен быть от 0 до 1")
        if self.candidate_window < 1:
            raise ValueError("candidate_window должен быть не меньше 1")
        if self.candidate_trajectories < 1:
            raise ValueError("candidate_trajectories должен быть не меньше 1")
        if self.layer_bridge_span < 0:
            raise ValueError("layer_bridge_span не может быть отрицательным")
        if self.maximum_merge_reduction < 1:
            raise ValueError("maximum_merge_reduction должен быть не меньше 1")
        if self.maximum_pool_merges < 1:
            raise ValueError("maximum_pool_merges должен быть не меньше 1")
        if self.operator_policy not in {"uniform", "ucb1"}:
            raise ValueError("operator_policy должен быть 'uniform' или 'ucb1'")
        if not math.isfinite(self.ucb_exploration) or self.ucb_exploration < 0.0:
            raise ValueError("ucb_exploration должен быть конечным неотрицательным числом")
        if len(set(self.baseline_seed_algorithms)) != len(
            self.baseline_seed_algorithms
        ):
            raise ValueError("baseline_seed_algorithms не должны повторяться")

    @property
    def id(self) -> str:
        """Вернуть короткий стабильный идентификатор запуска."""

        return (
            f"p{self.population_size}-g{self.generations}-s{self.random_seed}-"
            f"cx{self.crossover_rate:g}-mut{self.mutation_rate:g}-"
            f"w{self.candidate_window}-t{self.candidate_trajectories}-"
            f"b{self.layer_bridge_span}-"
            f"r{self.maximum_merge_reduction}-m{self.maximum_pool_merges}-"
            f"{self.complexity_axis.value}-{self.operator_policy}"
        )

    def algorithm_params(
        self,
    ) -> dict[str, int | float | str | tuple[str, ...]]:
        """Сериализовать только параметры, которые получает GA."""

        return {
            "population_size": self.population_size,
            "generations": self.generations,
            "random_seed": self.random_seed,
            "crossover_rate": self.crossover_rate,
            "mutation_rate": self.mutation_rate,
            "candidate_window": self.candidate_window,
            "candidate_trajectories": self.candidate_trajectories,
            "layer_bridge_span": self.layer_bridge_span,
            "maximum_detail_reduction_per_merge": self.maximum_merge_reduction,
            "maximum_pool_merges": self.maximum_pool_merges,
            "complexity_axis": self.complexity_axis.value,
            "operator_policy": self.operator_policy,
            "ucb_exploration": self.ucb_exploration,
            "baseline_seed_algorithms": self.baseline_seed_algorithms,
        }


@dataclass(frozen=True)
class GeneticRunResult:
    """Результат одного запуска вместе с полной общеплитной моделью."""

    config: GeneticRunConfig
    analysis: PlateAnalysis


def run_genetic_benchmark(
    sources: tuple[PlateDirectionSource, ...],
    configs: tuple[GeneticRunConfig, ...],
    *,
    min_width_cells: int = 2,
    cutting_profile: str = "continuous",
    case_id: str = "",
) -> tuple[GeneticRunResult, ...]:
    """Последовательно выполнить серию GA с одинаковыми входами и правилами."""

    if not configs:
        raise ValueError("для benchmark нужна хотя бы одна конфигурация GA")

    results = []
    for config in configs:
        analysis = analyze_plate(
            sources,
            algorithm_names=("genetic-pareto",),
            max_details_per_direction=None,
            min_width_cells=min_width_cells,
            cutting_profile=cutting_profile,
            case_id=case_id,
            complexity_axis=config.complexity_axis,
            algorithm_params={"genetic-pareto": config.algorithm_params()},
        )
        results.append(GeneticRunResult(config=config, analysis=analysis))
    return tuple(results)
