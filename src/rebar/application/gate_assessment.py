"""Проверяемые отклонения решения от инженерных MVP-гейтов.

Модуль намеренно не выдаёт общий «процент готовности проекта»: он оценивает только те
численные требования, которые можно восстановить из ``LayoutSolution``. Неподключённый
каталог допустимых позиций А101 всегда остаётся явно непроверенным.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from rebar.golden import GoldenCaseDefinition, PlateMetricReference
from rebar.optimization import (
    LayoutProblem,
    LayoutSolution,
    PlateProblem,
    PlateSolution,
)
from rebar.optimization.services import prepare_detailing
from rebar.optimization.services.geometry import GEOMETRY_TOLERANCE_MM


@dataclass(frozen=True)
class GateDeviation:
    """Одна понятная конструктору проверка и отклонение от норматива/эталона."""

    id: str
    title: str
    status: str
    unit: str
    target: float | None
    actual: float | None
    absolute_deviation: float | None
    relative_deviation_pct: float | None
    note: str


@dataclass(frozen=True)
class GateAssessment:
    """Набор проверок одного результата без подмены инженерного согласования."""

    items: tuple[GateDeviation, ...]
    reference_id: str | None = None
    reference_title: str | None = None

    @property
    def summary(self) -> dict[str, int]:
        statuses = ("pass", "fail", "warning", "not_checked")
        return {
            status: sum(item.status == status for item in self.items)
            for status in statuses
        }


def _deviation(
    *,
    id: str,
    title: str,
    status: str,
    unit: str,
    target: float | None,
    actual: float | None,
    note: str,
) -> GateDeviation:
    if target is None or actual is None:
        absolute = None
        relative = None
    else:
        absolute = actual - target
        relative = None if math.isclose(target, 0.0) else absolute / target * 100.0
    return GateDeviation(
        id=id,
        title=title,
        status=status,
        unit=unit,
        target=target,
        actual=actual,
        absolute_deviation=absolute,
        relative_deviation_pct=relative,
        note=note,
    )


def _coverage_item(*, demanded: int, covered: int) -> GateDeviation:
    actual = 100.0 if demanded == 0 else covered / demanded * 100.0
    return _deviation(
        id="demand-coverage",
        title="Перекрытие требуемых КЭ",
        status="pass" if covered == demanded else "fail",
        unit="%",
        target=100.0,
        actual=actual,
        note=(
            "Недоармирование недопустимо; доля считается от всех КЭ, требующих добавки."
        ),
    )


def _minimum_width_item(
    problem_solutions: tuple[tuple[LayoutProblem, LayoutSolution], ...],
) -> GateDeviation:
    widths: list[float] = []
    targets = {problem.constraints.min_width_cells for problem, _ in problem_solutions}
    for problem, solution in problem_solutions:
        if not solution.zones or not problem.demand.cells:
            continue
        typical_width = prepare_detailing(problem).typical_transverse_cell_size_mm
        widths.extend(zone.width_mm / typical_width for zone in solution.zones)

    target = float(max(targets)) if targets else None
    actual = min(widths) if widths else None
    status = "not_checked" if actual is None or target is None else (
        "pass" if actual + GEOMETRY_TOLERANCE_MM >= target else "fail"
    )
    return _deviation(
        id="minimum-zone-width",
        title="Минимальная ширина зоны",
        status=status,
        unit="КЭ",
        target=target,
        actual=actual,
        note="Сравнивается минимальная ширина всех зон с выбранным порогом 2–3 КЭ.",
    )


def _anchorage_item(
    problem_solutions: tuple[tuple[LayoutProblem, LayoutSolution], ...],
) -> GateDeviation:
    values = [
        (zone.anchored_length_mm - zone.required_length_mm)
        / (2.0 * zone.rebar.diameter)
        for _, solution in problem_solutions
        for zone in solution.zones
        if zone.rebar.diameter > 0
    ]
    targets = {
        problem.constraints.anchorage_diameters for problem, _ in problem_solutions
    }
    target = max(targets) if targets else None
    actual = min(values) if values else None
    status = "not_checked" if actual is None or target is None else (
        "pass" if actual + GEOMETRY_TOLERANCE_MM >= target else "fail"
    )
    return _deviation(
        id="anchorage",
        title="Анкеровка с каждого конца",
        status=status,
        unit="d",
        target=target,
        actual=actual,
        note="Фактическое удлинение каждой зоны сравнивается с требованием ≥40d.",
    )


def _step_multiple_item(
    problem_solutions: tuple[tuple[LayoutProblem, LayoutSolution], ...],
) -> GateDeviation:
    zones = [zone for _, solution in problem_solutions for zone in solution.zones]
    valid_count = sum(
        math.isclose(
            zone.width_mm,
            round(zone.width_mm / zone.rebar.step) * zone.rebar.step,
            abs_tol=GEOMETRY_TOLERANCE_MM,
        )
        for zone in zones
        if zone.rebar.step > 0
    )
    actual = None if not zones else valid_count / len(zones) * 100.0
    return _deviation(
        id="step-multiple",
        title="Ширина кратна шагу стержней",
        status="not_checked" if actual is None else ("pass" if valid_count == len(zones) else "fail"),
        unit="% зон",
        target=100.0,
        actual=actual,
        note="Выполнено, только если условие соблюдено для каждой параметрической зоны.",
    )


def _postprocessing_item(
    problem_solutions: tuple[tuple[LayoutProblem, LayoutSolution], ...],
) -> GateDeviation:
    diagnostics = tuple(
        diagnostic
        for _, solution in problem_solutions
        for diagnostic in solution.diagnostics
    )
    conflicts = sum(
        "конфликтуют после детализации" in diagnostic
        or "расстояние между крайними стержнями" in diagnostic
        for diagnostic in diagnostics
    )
    gap_check_disabled = any(
        "проверка раздвижки зон отключена" in diagnostic
        for diagnostic in diagnostics
    )
    return _deviation(
        id="postprocessing-conflicts",
        title="Коллизии после 40d и раздвижки",
        status="pass" if conflicts == 0 and not gap_check_disabled else "warning",
        unit="пар",
        target=0.0,
        actual=float(conflicts),
        note=(
            "Относительное отклонение не определяется для нулевого норматива. "
            "Отключённая проверка или найденные пары требуют решения до экспорта."
        ),
    )


def _a101_catalog_item() -> GateDeviation:
    return _deviation(
        id="a101-allowed-positions",
        title="Допустимые позиции А101 2.4.2–2.4.7",
        status="not_checked",
        unit="",
        target=None,
        actual=None,
        note="Каталог заказчика ещё не оцифрован; интерфейс не скрывает этот пробел.",
    )


def assess_layout_gates(
    problem: LayoutProblem,
    solution: LayoutSolution,
) -> GateAssessment:
    """Оценить измеримые output-гейты решения одного направления."""

    metrics = solution.metrics
    pairs = ((problem, solution),)
    return GateAssessment(
        items=(
            _coverage_item(
                demanded=metrics.demanded_cell_count,
                covered=metrics.covered_demanded_cell_count,
            ),
            _minimum_width_item(pairs),
            _anchorage_item(pairs),
            _step_multiple_item(pairs),
            _postprocessing_item(pairs),
            _a101_catalog_item(),
        )
    )


def assess_plate_gates(
    problem: PlateProblem,
    solution: PlateSolution,
    *,
    reference: GoldenCaseDefinition | PlateMetricReference | None = None,
) -> GateAssessment:
    """Оценить общеплитный результат и, если выбран, инженерный golden-case."""

    pairs = tuple(
        (direction_problem, solution.solution(direction_problem.demand.direction))
        for direction_problem in problem.direction_problems
    )
    metrics = solution.metrics
    items = [
        _deviation(
            id="plate-directions",
            title="Полный комплект направлений",
            status="pass" if metrics.direction_count == 4 else "fail",
            unit="направления",
            target=4.0,
            actual=float(metrics.direction_count),
            note="Нужны bottom/top × X/Y; агрегировать неполный комплект нельзя.",
        ),
        _coverage_item(
            demanded=metrics.demanded_cell_count,
            covered=metrics.covered_demanded_cell_count,
        ),
        _minimum_width_item(pairs),
        _anchorage_item(pairs),
        _step_multiple_item(pairs),
        _postprocessing_item(pairs),
        _a101_catalog_item(),
    ]
    if reference is not None:
        items.extend(
            (
                _deviation(
                    id="reference-mass",
                    title="Масса относительно решения конструктора",
                    status="pass" if metrics.total_mass_kg <= reference.expected_mass_kg else "warning",
                    unit="кг",
                    target=reference.expected_mass_kg,
                    actual=metrics.total_mass_kg,
                    note="Эталон используется для сравнения, а не как hard-ограничение безопасности.",
                ),
                _deviation(
                    id="reference-bars",
                    title="Стержни относительно решения конструктора",
                    status="pass" if metrics.physical_bar_count <= reference.expected_bar_count else "warning",
                    unit="шт.",
                    target=float(reference.expected_bar_count),
                    actual=float(metrics.physical_bar_count),
                    note="Сравниваются физические стержни; LayoutZone не приравнивается к позиции PDF.",
                ),
            )
        )
    return GateAssessment(
        items=tuple(items),
        reference_id=None if reference is None else reference.id,
        reference_title=None if reference is None else reference.title,
    )
