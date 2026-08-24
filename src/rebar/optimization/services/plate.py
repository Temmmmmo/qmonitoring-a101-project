"""Сборка и независимая агрегация общеплитных задач и решений."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..contracts import (
    PlateDirectionSolution,
    PlateMetrics,
    PlateProblem,
    PlateSolution,
    SolutionStatus,
)
from ..contracts.plate import _canonical_direction_items
from ..contracts.problem import LayoutProblem


def build_plate_problem(
    problems: Iterable[LayoutProblem],
    *,
    case_id: str = "",
    meta: dict[str, Any] | None = None,
) -> PlateProblem:
    """Собрать полный комплект из четырёх однонаправленных задач."""

    return PlateProblem(
        direction_problems=tuple(problems),
        case_id=case_id,
        meta={} if meta is None else dict(meta),
    )


def _prefixed_diagnostic(direction: str, diagnostic: str) -> str:
    for severity in ("ERROR", "WARNING"):
        prefix = f"{severity}: "
        if diagnostic.startswith(prefix):
            return f"{prefix}{direction}: {diagnostic[len(prefix):]}"
    return f"{direction}: {diagnostic}"


def _aggregate_status(
    items: tuple[PlateDirectionSolution, ...],
    *,
    has_under_reinforcement: bool,
) -> SolutionStatus:
    statuses = {item.solution.status for item in items}
    if has_under_reinforcement or SolutionStatus.ERROR in statuses:
        return SolutionStatus.ERROR
    if SolutionStatus.INFEASIBLE in statuses:
        return SolutionStatus.INFEASIBLE
    if SolutionStatus.TIME_LIMIT in statuses:
        return SolutionStatus.TIME_LIMIT
    if statuses == {SolutionStatus.OPTIMAL}:
        return SolutionStatus.OPTIMAL
    return SolutionStatus.FEASIBLE


def build_plate_solution(
    direction_solutions: Iterable[PlateDirectionSolution],
    *,
    meta: dict[str, Any] | None = None,
) -> PlateSolution:
    """Пересчитать суммы и распространить ошибку любого направления на всю плиту."""

    items = tuple(direction_solutions)
    canonical = _canonical_direction_items(
        items,
        lambda item: item.direction,
        item_name="PlateSolution",
    )

    metrics = PlateMetrics(
        direction_count=len(canonical),
        zone_count=sum(item.solution.metrics.detail_count for item in canonical),
        physical_bar_count=sum(
            item.solution.metrics.physical_bar_count for item in canonical
        ),
        total_mass_kg=sum(item.solution.metrics.total_mass_kg for item in canonical),
        total_bar_length_mm=sum(
            item.solution.metrics.total_bar_length_mm for item in canonical
        ),
        demanded_cell_count=sum(
            item.solution.metrics.demanded_cell_count for item in canonical
        ),
        covered_demanded_cell_count=sum(
            item.solution.metrics.covered_demanded_cell_count for item in canonical
        ),
        under_reinforced_cell_count=sum(
            item.solution.metrics.under_reinforced_cell_count for item in canonical
        ),
        overcovered_cell_count=sum(
            item.solution.metrics.overcovered_cell_count for item in canonical
        ),
        overcovered_area_mm2=sum(
            item.solution.metrics.overcovered_area_mm2 for item in canonical
        ),
        objective_value=sum(item.solution.metrics.objective_value for item in canonical),
    )
    diagnostics = tuple(
        _prefixed_diagnostic(str(item.direction), diagnostic)
        for item in canonical
        for diagnostic in item.solution.diagnostics
    )
    has_under_reinforcement = metrics.under_reinforced_cell_count > 0
    if has_under_reinforcement:
        diagnostics += (
            "ERROR: общеплитное решение содержит недоармированные КЭ: "
            f"{metrics.under_reinforced_cell_count}",
        )

    status = _aggregate_status(
        canonical,
        has_under_reinforcement=has_under_reinforcement,
    )
    return PlateSolution(
        direction_solutions=canonical,
        status=status,
        metrics=metrics,
        runtime_ms=sum(item.solution.runtime_ms for item in canonical),
        diagnostics=diagnostics,
        meta={
            **({} if meta is None else dict(meta)),
            "algorithm_by_direction": {
                str(item.direction): item.solution.algorithm for item in canonical
            },
        },
    )
