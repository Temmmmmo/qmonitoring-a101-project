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
from .detailing import demanded_cells
from .geometry import GEOMETRY_TOLERANCE_MM, bboxes_overlap, point_in_bbox, polygon_area


def evaluate_layout(
    problem: LayoutProblem,
    zones: Sequence[LayoutZone],
    request: AlgorithmRequest | None = None,
) -> LayoutEvaluation:
    """Независимо проверить решение и заново вычислить сравнимые метрики."""

    request = request or AlgorithmRequest()
    demanded = demanded_cells(problem)
    covered: set[int] = set()
    overcovered: set[int] = set()
    diagnostics: list[str] = []

    for zone in zones:
        try:
            level = problem.demand.level(zone.level_index)
        except KeyError:
            diagnostics.append(f"ERROR: зона {zone.id}: неизвестный уровень {zone.level_index}")
            continue
        if level.additional != zone.rebar:
            diagnostics.append(f"ERROR: зона {zone.id}: арматура не соответствует уровню")
        xmin, ymin, xmax, ymax = zone.bbox
        if xmax <= xmin or ymax <= ymin:
            diagnostics.append(f"ERROR: зона {zone.id}: вырожденный bbox")
            continue

        expected_width = ymax - ymin if problem.demand.direction.axis is Axis.X else xmax - xmin
        expected_length = xmax - xmin if problem.demand.direction.axis is Axis.X else ymax - ymin
        if not math.isclose(zone.width_mm, expected_width, abs_tol=GEOMETRY_TOLERANCE_MM):
            diagnostics.append(f"ERROR: зона {zone.id}: width_mm не совпадает с bbox")
        if not math.isclose(
            zone.installed_length_mm, expected_length, abs_tol=GEOMETRY_TOLERANCE_MM
        ):
            diagnostics.append(f"ERROR: зона {zone.id}: installed_length_mm не совпадает с bbox")

        for cell in problem.demand.cells:
            if not point_in_bbox(cell.centroid, zone.bbox):
                continue
            cell_level = problem.demand.level(cell.level_index)
            if cell_level.requires_extra is True and zone.level_index >= cell.level_index:
                covered.add(cell.id)
            if cell_level.requires_extra is not True or zone.level_index > cell.level_index:
                overcovered.add(cell.id)

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

    if not problem.constraints.allow_overcoverage and overcovered:
        diagnostics.append(
            f"ERROR: избыточно накрыты КЭ: {len(overcovered)} при запрете перерасхода"
        )

    if not problem.constraints.allow_overlaps:
        for index, first in enumerate(zones):
            for second in zones[index + 1 :]:
                if bboxes_overlap(first.bbox, second.bbox):
                    diagnostics.append(f"ERROR: зоны {first.id} и {second.id} пересекаются")

    cell_by_id = {cell.id: cell for cell in problem.demand.cells}
    overcovered_area = sum(polygon_area(cell_by_id[cell_id].poly) for cell_id in overcovered)
    total_mass = sum(zone.mass_kg for zone in zones)
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
