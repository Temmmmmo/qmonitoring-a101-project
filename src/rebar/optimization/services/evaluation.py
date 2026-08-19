"""Независимая проверка решений и единый расчёт сравнимых метрик."""

from __future__ import annotations

import math
from collections.abc import Sequence

from rebar.models import Axis

from ..contracts import (
    AlgorithmRequest,
    LayoutEvaluation,
    LayoutMetrics,
    LayoutProblem,
    LayoutZone,
)
from .detailing import demanded_cells, prepare_detailing, rebar_mass_kg
from .geometry import (
    GEOMETRY_TOLERANCE_MM,
    bboxes_distance,
    bboxes_overlap,
    polygon_area,
    polygon_bbox_intersection_area,
    polygon_in_bbox,
)


def zones_conflict(
    problem: LayoutProblem,
    first: LayoutZone,
    second: LayoutZone,
) -> bool:
    """Проверить заведомо недопустимую близость двух зон одного направления."""

    if problem.constraints.allow_overlaps:
        return False
    if bboxes_overlap(first.bbox, second.bbox):
        return True
    if not problem.constraints.enforce_zone_gap:
        return False
    if first.rebar.step <= 0 or second.rebar.step <= 0:
        return True

    # Для одинакового шага ТЗ требует раздвижку ровно на шаг. Для разных шагов
    # отбрасываем только расстояние, которое меньше обоих возможных правил; промежуток
    # между min(step) и max(step) остаётся допустимым с WARNING от evaluate_layout.
    definitely_required_gap = min(first.rebar.step, second.rebar.step)
    return (
        bboxes_distance(first.bbox, second.bbox)
        + GEOMETRY_TOLERANCE_MM
        < definitely_required_gap
    )


def _append_zone_geometry_diagnostics(
    problem: LayoutProblem,
    zone: LayoutZone,
    diagnostics: list[str],
    typical_transverse_cell_size_mm: float | None,
) -> float:
    """Проверить поля зоны и вернуть независимо рассчитанную массу."""

    try:
        level = problem.demand.level(zone.level_index)
    except KeyError:
        diagnostics.append(f"ERROR: зона {zone.id}: неизвестный уровень {zone.level_index}")
        return 0.0
    if level.additional is None:
        diagnostics.append(f"ERROR: зона {zone.id}: уровень не задаёт дополнительную арматуру")
        return 0.0
    if level.additional != zone.rebar:
        diagnostics.append(f"ERROR: зона {zone.id}: арматура не соответствует уровню")

    numeric_values = (
        *zone.bbox,
        zone.width_mm,
        zone.required_length_mm,
        zone.installed_length_mm,
        zone.mass_kg,
    )
    if not all(math.isfinite(value) for value in numeric_values):
        diagnostics.append(f"ERROR: зона {zone.id}: геометрия или масса содержит нечисловое значение")
        return 0.0

    xmin, ymin, xmax, ymax = zone.bbox
    if xmax <= xmin or ymax <= ymin:
        diagnostics.append(f"ERROR: зона {zone.id}: вырожденный bbox")
        return 0.0

    axis = problem.demand.direction.axis
    expected_width = ymax - ymin if axis is Axis.X else xmax - xmin
    expected_installed_length = xmax - xmin if axis is Axis.X else ymax - ymin
    if not math.isclose(zone.width_mm, expected_width, abs_tol=GEOMETRY_TOLERANCE_MM):
        diagnostics.append(f"ERROR: зона {zone.id}: width_mm не совпадает с bbox")
    if not math.isclose(
        zone.installed_length_mm,
        expected_installed_length,
        abs_tol=GEOMETRY_TOLERANCE_MM,
    ):
        diagnostics.append(f"ERROR: зона {zone.id}: installed_length_mm не совпадает с bbox")

    step = level.additional.step
    diameter = level.additional.diameter
    if step <= 0:
        diagnostics.append(f"ERROR: зона {zone.id}: шаг арматуры должен быть положительным")
        return 0.0
    if diameter <= 0:
        diagnostics.append(f"ERROR: зона {zone.id}: диаметр арматуры должен быть положительным")
        return 0.0

    width_space_count = round(expected_width / step)
    if width_space_count < 1 or not math.isclose(
        expected_width,
        width_space_count * step,
        abs_tol=GEOMETRY_TOLERANCE_MM,
    ):
        diagnostics.append(f"ERROR: зона {zone.id}: ширина не кратна шагу {step} мм")
    if zone.bar_count != width_space_count + 1:
        diagnostics.append(
            f"ERROR: зона {zone.id}: bar_count должен быть width/step + 1"
        )

    if typical_transverse_cell_size_mm is not None:
        minimum_width = (
            problem.constraints.min_width_cells * typical_transverse_cell_size_mm
        )
        if expected_width + GEOMETRY_TOLERANCE_MM < minimum_width:
            diagnostics.append(
                f"ERROR: зона {zone.id}: ширина меньше "
                f"{problem.constraints.min_width_cells} КЭ"
            )

    anchorage_each_end = problem.constraints.anchorage_diameters * diameter
    expected_required_length = expected_installed_length - 2.0 * anchorage_each_end
    if expected_required_length <= GEOMETRY_TOLERANCE_MM:
        diagnostics.append(f"ERROR: зона {zone.id}: длина не оставляет рабочей части после 40d")
    if not math.isclose(
        zone.required_length_mm,
        expected_required_length,
        abs_tol=GEOMETRY_TOLERANCE_MM,
    ):
        diagnostics.append(
            f"ERROR: зона {zone.id}: required_length_mm не согласована с анкеровкой"
        )

    try:
        expected_mass = rebar_mass_kg(
            diameter,
            expected_installed_length,
            width_space_count + 1,
        )
    except ValueError as error:
        diagnostics.append(f"ERROR: зона {zone.id}: невозможно рассчитать массу: {error}")
        return 0.0
    if not math.isclose(zone.mass_kg, expected_mass, rel_tol=1e-9, abs_tol=1e-6):
        diagnostics.append(f"ERROR: зона {zone.id}: mass_kg не совпадает с общим расчётом")
    return expected_mass


def evaluate_layout(
    problem: LayoutProblem,
    zones: Sequence[LayoutZone],
    request: AlgorithmRequest | None = None,
) -> LayoutEvaluation:
    """Независимо проверить решение и заново вычислить сравнимые метрики."""

    request = request or AlgorithmRequest()
    demanded = demanded_cells(problem)
    covered: set[int] = set()
    overcovered_area_by_cell: dict[int, float] = {}
    diagnostics: list[str] = []
    total_mass = 0.0

    zone_ids = [zone.id for zone in zones]
    if len(zone_ids) != len(set(zone_ids)):
        diagnostics.append("ERROR: идентификаторы зон должны быть уникальными")

    typical_cell_width: float | None = None
    if zones and problem.demand.cells:
        typical_cell_width = prepare_detailing(problem).typical_transverse_cell_size_mm

    for zone in zones:
        total_mass += _append_zone_geometry_diagnostics(
            problem,
            zone,
            diagnostics,
            typical_cell_width,
        )
        try:
            level = problem.demand.level(zone.level_index)
        except KeyError:
            continue
        if level.additional is None:
            continue
        xmin, ymin, xmax, ymax = zone.bbox
        if xmax <= xmin or ymax <= ymin:
            continue

        zone_covered: set[int] = set()
        zone_overcovered: set[int] = set()
        weaker_intersections: list[int] = []
        for cell in problem.demand.cells:
            intersection_area = polygon_bbox_intersection_area(cell.poly, zone.bbox)
            if intersection_area <= GEOMETRY_TOLERANCE_MM:
                continue
            cell_level = problem.demand.level(cell.level_index)
            if cell_level.requires_extra is True and zone.level_index < cell.level_index:
                weaker_intersections.append(cell.id)
                continue
            if (
                cell_level.requires_extra is True
                and polygon_in_bbox(cell.poly, zone.bbox)
            ):
                covered.add(cell.id)
                zone_covered.add(cell.id)
            if cell_level.requires_extra is not True or zone.level_index > cell.level_index:
                zone_overcovered.add(cell.id)
                overcovered_area_by_cell[cell.id] = (
                    overcovered_area_by_cell.get(cell.id, 0.0) + intersection_area
                )

        if weaker_intersections:
            diagnostics.append(
                f"ERROR: зона {zone.id}: слабее требования пересекаемых КЭ "
                f"{sorted(weaker_intersections)[:10]}"
            )
        if set(zone.covered_cell_ids) != zone_covered:
            diagnostics.append(
                f"ERROR: зона {zone.id}: covered_cell_ids не совпадает с геометрией"
            )
        if set(zone.overcovered_cell_ids) != zone_overcovered:
            diagnostics.append(
                f"ERROR: зона {zone.id}: overcovered_cell_ids не совпадает с геометрией"
            )

    under_reinforced = {cell.id for cell in demanded} - covered
    if under_reinforced:
        diagnostics.append(
            f"ERROR: недоармированы КЭ: {len(under_reinforced)} "
            f"(первые: {sorted(under_reinforced)[:10]})"
        )
    if request.max_details is not None and len(zones) > request.max_details:
        diagnostics.append(
            f"ERROR: деталей {len(zones)}, ограничение max_details={request.max_details}"
        )

    overcovered = set(overcovered_area_by_cell)
    if not problem.constraints.allow_overcoverage and overcovered:
        diagnostics.append(
            f"ERROR: избыточно накрыты КЭ: {len(overcovered)} при запрете перерасхода"
        )

    if problem.constraints.allow_overlaps and len(zones) > 1:
        diagnostics.append(
            "WARNING: пересечения и зазоры отключены исследовательским allow_overlaps"
        )
    elif not problem.constraints.allow_overlaps:
        unresolved_mixed_gap_pairs: list[str] = []
        for index, first in enumerate(zones):
            for second in zones[index + 1 :]:
                if bboxes_overlap(first.bbox, second.bbox):
                    diagnostics.append(f"ERROR: зоны {first.id} и {second.id} пересекаются")
                    continue
                if not problem.constraints.enforce_zone_gap:
                    continue

                distance = bboxes_distance(first.bbox, second.bbox)
                smaller_step = min(first.rebar.step, second.rebar.step)
                larger_step = max(first.rebar.step, second.rebar.step)
                if distance + GEOMETRY_TOLERANCE_MM < smaller_step:
                    diagnostics.append(
                        f"ERROR: зазор между зонами {first.id} и {second.id} "
                        f"{distance:.3f} мм меньше требуемого минимума {smaller_step} мм"
                    )
                elif (
                    first.rebar.step != second.rebar.step
                    and distance + GEOMETRY_TOLERANCE_MM < larger_step
                ):
                    unresolved_mixed_gap_pairs.append(f"{first.id}/{second.id}")
        if not problem.constraints.enforce_zone_gap and len(zones) > 1:
            diagnostics.append("WARNING: проверка раздвижки зон отключена")
        if unresolved_mixed_gap_pairs:
            diagnostics.append(
                "WARNING: правило зазора для разных шагов не подтверждено; "
                f"пары: {unresolved_mixed_gap_pairs[:10]}"
            )

    cell_by_id = {cell.id: cell for cell in problem.demand.cells}
    overcovered_area = sum(
        min(area, polygon_area(cell_by_id[cell_id].poly))
        for cell_id, area in overcovered_area_by_cell.items()
    )
    if problem.constraints.allow_overlaps and overcovered:
        diagnostics.append(
            "WARNING: площадь перерасхода в режиме пересечений ограничена площадью КЭ"
        )

    objective = (
        request.objective.mass * total_mass
        + request.objective.detail_penalty_kg * len(zones)
    )
    metrics = LayoutMetrics(
        detail_count=len(zones),
        total_mass_kg=total_mass,
        demanded_cell_count=len(demanded),
        covered_demanded_cell_count=len(covered),
        under_reinforced_cell_count=len(under_reinforced),
        overcovered_cell_count=len(overcovered),
        overcovered_area_mm2=overcovered_area,
        objective_value=objective,
    )
    return LayoutEvaluation(
        valid=not any(message.startswith("ERROR:") for message in diagnostics),
        metrics=metrics,
        diagnostics=tuple(diagnostics),
    )
