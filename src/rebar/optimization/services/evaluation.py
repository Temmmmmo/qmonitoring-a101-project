"""Независимая проверка параметрических групп и единый расчёт метрик."""

from __future__ import annotations

import math
from collections.abc import Sequence

from rebar.standards import (
    A101PositionOutcome,
    a101_profile_id_from_metadata,
    validate_a101_positions,
)

from ..contracts import (
    AlgorithmRequest,
    LayoutEvaluation,
    LayoutMetrics,
    LayoutProblem,
    LayoutZone,
)
from .anchorage import FixedDiameterAnchoragePolicy
from .bar_geometry import (
    bar_coordinates,
    bars_conflict,
    intervals_overlap,
    longitudinal_interval,
    transverse_axis_gap,
    transverse_interval,
    zone_coverage_bbox,
)
from .cutting import select_installed_length_mm
from .detailing import demanded_cells, prepare_detailing, rebar_mass_kg
from .geometry import (
    GEOMETRY_TOLERANCE_MM,
    bboxes_overlap,
    point_in_bbox,
    polygon_area,
    polygon_bbox_intersection_area,
    polygon_bboxes_union_intersection_area,
)


def partition_zones_conflict(
    problem: LayoutProblem,
    first: LayoutZone,
    second: LayoutZone,
) -> bool:
    """Проверить только жёсткое пересечение прямоугольников разбиения."""

    if problem.constraints.allow_overlaps:
        return False
    return bboxes_overlap(first.demand_bbox, second.demand_bbox)


def zones_conflict(
    problem: LayoutProblem,
    first: LayoutZone,
    second: LayoutZone,
) -> bool:
    """Совместимый псевдоним для жёсткого конфликта разбиения."""

    return partition_zones_conflict(problem, first, second)


def _append_zone_geometry_diagnostics(
    problem: LayoutProblem,
    zone: LayoutZone,
    diagnostics: list[str],
    typical_transverse_cell_size_mm: float | None,
) -> float:
    """Проверить поля группы и вернуть независимо рассчитанную массу."""

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
        *zone.demand_bbox,
        zone.width_mm,
        zone.required_length_mm,
        zone.anchored_length_mm,
        zone.installed_length_mm,
        zone.first_bar_coordinate_mm,
        zone.mass_kg,
    )
    if not all(math.isfinite(value) for value in numeric_values):
        diagnostics.append(
            f"ERROR: зона {zone.id}: геометрия или масса содержит нечисловое значение"
        )
        return 0.0

    xmin, ymin, xmax, ymax = zone.bbox
    dxmin, dymin, dxmax, dymax = zone.demand_bbox
    if xmax <= xmin or ymax <= ymin:
        diagnostics.append(f"ERROR: зона {zone.id}: вырожденный bbox")
        return 0.0
    if dxmax <= dxmin or dymax <= dymin:
        diagnostics.append(f"ERROR: зона {zone.id}: вырожденный demand_bbox")
        return 0.0
    axis = problem.demand.direction.axis
    service_bbox = zone_coverage_bbox(axis, zone)
    if not all(
        point_in_bbox(point, service_bbox)
        for point in ((dxmin, dymin), (dxmax, dymax))
    ):
        diagnostics.append(
            f"ERROR: зона {zone.id}: demand_bbox не входит в область обслуживания"
        )
    transverse_start, transverse_end = transverse_interval(axis, zone.bbox)
    longitudinal_start, longitudinal_end = longitudinal_interval(axis, zone.bbox)
    demand_longitudinal_start, demand_longitudinal_end = longitudinal_interval(
        axis, zone.demand_bbox
    )
    expected_width = transverse_end - transverse_start
    expected_installed_length = longitudinal_end - longitudinal_start
    expected_required_length = demand_longitudinal_end - demand_longitudinal_start

    if not math.isclose(zone.width_mm, expected_width, abs_tol=GEOMETRY_TOLERANCE_MM):
        diagnostics.append(f"ERROR: зона {zone.id}: width_mm не совпадает с bbox")
    if not math.isclose(
        zone.first_bar_coordinate_mm,
        transverse_start,
        abs_tol=GEOMETRY_TOLERANCE_MM,
    ):
        diagnostics.append(f"ERROR: зона {zone.id}: первая ось не совпадает с bbox")
    if not math.isclose(
        zone.installed_length_mm,
        expected_installed_length,
        abs_tol=GEOMETRY_TOLERANCE_MM,
    ):
        diagnostics.append(f"ERROR: зона {zone.id}: installed_length_mm не совпадает с bbox")
    if not math.isclose(
        zone.required_length_mm,
        expected_required_length,
        abs_tol=GEOMETRY_TOLERANCE_MM,
    ):
        diagnostics.append(f"ERROR: зона {zone.id}: required_length_mm не совпадает с demand_bbox")

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
        diagnostics.append(f"ERROR: зона {zone.id}: bar_count должен быть width/step + 1")
    coordinates = bar_coordinates(zone)
    if len(coordinates) != zone.bar_count:
        diagnostics.append(f"ERROR: зона {zone.id}: невозможно восстановить оси стержней")
    elif not math.isclose(
        coordinates[-1],
        transverse_end,
        abs_tol=GEOMETRY_TOLERANCE_MM,
    ):
        diagnostics.append(f"ERROR: зона {zone.id}: последняя ось не совпадает с bbox")

    if typical_transverse_cell_size_mm is not None:
        minimum_width = problem.constraints.min_width_cells * typical_transverse_cell_size_mm
        if expected_width + GEOMETRY_TOLERANCE_MM < minimum_width:
            diagnostics.append(
                f"ERROR: зона {zone.id}: ширина меньше "
                f"{problem.constraints.min_width_cells} КЭ"
            )

    anchorage_policy = FixedDiameterAnchoragePolicy(
        problem.constraints.anchorage_diameters
    )
    anchorage_each_end = anchorage_policy.extension_each_end_mm(level.additional)
    expected_anchored_length = expected_required_length + 2.0 * anchorage_each_end
    if not math.isclose(
        zone.anchored_length_mm,
        expected_anchored_length,
        abs_tol=GEOMETRY_TOLERANCE_MM,
    ):
        diagnostics.append(f"ERROR: зона {zone.id}: anchored_length_mm не согласована с анкеровкой")

    try:
        selected_length = select_installed_length_mm(
            expected_anchored_length,
            problem.constraints.allowed_cut_lengths_mm,
        )
    except ValueError as error:
        diagnostics.append(f"ERROR: зона {zone.id}: невозможно выбрать длину отрезка: {error}")
        selected_length = expected_anchored_length
    if not math.isclose(
        zone.installed_length_mm,
        selected_length,
        abs_tol=GEOMETRY_TOLERANCE_MM,
    ):
        diagnostics.append(
            f"ERROR: зона {zone.id}: installed_length_mm не соответствует политике раскроя"
        )

    expected_extension = (selected_length - expected_required_length) / 2.0
    expected_longitudinal_start = demand_longitudinal_start - expected_extension
    expected_longitudinal_end = demand_longitudinal_end + expected_extension
    if not math.isclose(
        longitudinal_start,
        expected_longitudinal_start,
        abs_tol=GEOMETRY_TOLERANCE_MM,
    ) or not math.isclose(
        longitudinal_end,
        expected_longitudinal_end,
        abs_tol=GEOMETRY_TOLERANCE_MM,
    ):
        diagnostics.append(f"ERROR: зона {zone.id}: bbox не центрирован по установленной длине")

    try:
        expected_mass = rebar_mass_kg(
            diameter,
            selected_length,
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
    coverage_bboxes_by_cell: dict[int, list[tuple[float, float, float, float]]] = {}
    overcoverage_bboxes_by_cell: dict[int, list[tuple[float, float, float, float]]] = {}
    diagnostics: list[str] = []
    total_mass = 0.0

    zone_ids = [zone.id for zone in zones]
    if len(zone_ids) != len(set(zone_ids)):
        diagnostics.append("ERROR: идентификаторы зон должны быть уникальными")

    natural_minimum = 1 if demanded else 0
    natural_maximum = len(problem.demand.cells) if demanded else 0
    if not natural_minimum <= len(zones) <= natural_maximum:
        diagnostics.append(
            "ERROR: число зон должно быть в естественном диапазоне "
            f"{natural_minimum}..{natural_maximum}, получено {len(zones)}"
        )

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

        service_bbox = zone_coverage_bbox(problem.demand.direction.axis, zone)
        zone_covered: set[int] = set()
        zone_overcovered: set[int] = set()
        for cell in problem.demand.cells:
            demand_intersection_area = polygon_bbox_intersection_area(
                cell.poly,
                zone.demand_bbox,
            )
            cell_level = problem.demand.level(cell.level_index)
            if (
                demand_intersection_area > GEOMETRY_TOLERANCE_MM
                and cell_level.requires_extra is True
                and zone.level_index >= cell.level_index
            ):
                zone_covered.add(cell.id)
                coverage_bboxes_by_cell.setdefault(cell.id, []).append(zone.demand_bbox)
            service_intersection_area = polygon_bbox_intersection_area(
                cell.poly,
                service_bbox,
            )
            if service_intersection_area <= GEOMETRY_TOLERANCE_MM:
                continue
            if cell_level.requires_extra is not True or zone.level_index > cell.level_index:
                zone_overcovered.add(cell.id)
                overcoverage_bboxes_by_cell.setdefault(cell.id, []).append(service_bbox)

        if set(zone.covered_cell_ids) != zone_covered:
            diagnostics.append(f"ERROR: зона {zone.id}: covered_cell_ids не совпадает с геометрией")
        if set(zone.overcovered_cell_ids) != zone_overcovered:
            diagnostics.append(
                f"ERROR: зона {zone.id}: overcovered_cell_ids не совпадает с геометрией"
            )

    a101_profile_id = a101_profile_id_from_metadata(problem.demand.meta)
    try:
        a101_validation = validate_a101_positions(
            a101_profile_id,
            ((zone.id, zone.rebar) for zone in zones),
        )
    except KeyError as error:
        diagnostics.append(f"ERROR: не удалось проверить позиции А101: {error}")
    else:
        for check in a101_validation.checks:
            if check.outcome is A101PositionOutcome.PROHIBITED:
                diagnostics.append(
                    f"ERROR: зона {check.zone_id}: позиция "
                    f"⌀{check.rebar.diameter}/{check.rebar.step} запрещена "
                    f"профилем {a101_validation.profile_id} "
                    f"таблицы {a101_validation.table_id}"
                )

    covered = {
        cell.id
        for cell in demanded
        if polygon_bboxes_union_intersection_area(
            cell.poly,
            coverage_bboxes_by_cell.get(cell.id, ()),
        )
        + max(GEOMETRY_TOLERANCE_MM, polygon_area(cell.poly) * 1e-8)
        >= polygon_area(cell.poly)
    }
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

    overcovered = set(overcoverage_bboxes_by_cell)
    if not problem.constraints.allow_overcoverage and overcovered:
        diagnostics.append(
            f"ERROR: избыточно накрыты КЭ: {len(overcovered)} при запрете перерасхода"
        )

    conservative_mixed_gap_pairs: list[str] = []
    axis = problem.demand.direction.axis
    for index, first in enumerate(zones):
        for second in zones[index + 1 :]:
            if (
                not problem.constraints.allow_overlaps
                and partition_zones_conflict(problem, first, second)
            ):
                diagnostics.append(
                    f"ERROR: прямоугольники разбиения {first.id} и {second.id} "
                    "пересекаются"
                )

            physical_conflict = bars_conflict(
                axis,
                first,
                second,
                minimum_clear_spacing_mm=problem.constraints.minimum_clear_spacing_mm,
            )
            installed_overlap = bboxes_overlap(first.bbox, second.bbox)
            if physical_conflict:
                diagnostics.append(
                    f"WARNING: стержни зон {first.id} и {second.id} конфликтуют "
                    "после детализации"
                )
            elif installed_overlap:
                diagnostics.append(
                    f"WARNING: установленные envelope зон {first.id} и {second.id} "
                    "пересекаются после 40d, но линии стержней не конфликтуют"
                )

            if not problem.constraints.enforce_zone_gap:
                continue
            if not intervals_overlap(
                longitudinal_interval(axis, first.bbox),
                longitudinal_interval(axis, second.bbox),
            ):
                continue

            gap = transverse_axis_gap(axis, first, second)
            required_gap = max(first.rebar.step, second.rebar.step)
            if gap + GEOMETRY_TOLERANCE_MM < required_gap:
                diagnostics.append(
                    f"WARNING: расстояние между крайними стержнями зон "
                    f"{first.id} и {second.id} {gap:.3f} мм меньше "
                    f"требуемого минимума {required_gap} мм"
                )
            if first.rebar.step != second.rebar.step:
                conservative_mixed_gap_pairs.append(f"{first.id}/{second.id}")
    if not problem.constraints.enforce_zone_gap and len(zones) > 1:
        diagnostics.append("WARNING: проверка раздвижки зон отключена")
    if conservative_mixed_gap_pairs:
        diagnostics.append(
            "WARNING: для разных шагов применён консервативный больший шаг; "
            f"пары: {conservative_mixed_gap_pairs[:10]}"
        )

    cell_by_id = {cell.id: cell for cell in problem.demand.cells}
    overcovered_area_by_cell = {
        cell_id: polygon_bboxes_union_intersection_area(
            cell_by_id[cell_id].poly,
            bboxes,
        )
        for cell_id, bboxes in overcoverage_bboxes_by_cell.items()
    }
    overcovered_area = sum(
        area for area in overcovered_area_by_cell.values()
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
        physical_bar_count=sum(zone.bar_count for zone in zones),
        total_bar_length_mm=sum(
            zone.installed_length_mm * zone.bar_count for zone in zones
        ),
    )
    return LayoutEvaluation(
        valid=not any(message.startswith("ERROR:") for message in diagnostics),
        metrics=metrics,
        diagnostics=tuple(diagnostics),
    )
