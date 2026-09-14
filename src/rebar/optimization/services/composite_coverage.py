"""Независимое покрытие составных зон в ЯВНО выбранном research-профиле.

Не складывает слабые зоны или разные добавки из разных зон. Не подтверждает
несущую способность, нормативные сочетания, host, 3D или разрешение размещения.
Одиночный LayoutProblem/GA и существующий v2 export не меняются.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

from rebar.models import Axis, ReinforcementRecipe

from ..contracts.composite_coverage import (
    COMPOSITE_COVERAGE_POLICY, MONOTONE_SINGLE_STO_COVERAGE_POLICY, STO_279_COVERAGE_POLICY,
    CompositeCellCoverage, CompositeCoverageEvaluation, CompositeCoverageZoneCheck,
)
from ..contracts.placement import AxisPlacement, CompositeLayoutZone
from ..contracts.problem import BBox, DemandMap, LayoutConstraints
from .axis_patterns import pattern_runs
from .bar_geometry import longitudinal_interval
from .composite_detailing import CompositeDetailingContext, evaluate_composite_zone
from .geometry import (
    GEOMETRY_TOLERANCE_MM, bboxes_overlap, polygon_area, polygon_bbox_intersection_area,
    polygon_bboxes_union_intersection_area,
)

MAX_CELLS = 10000
MAX_ZONES = 512
MAX_CELL_ZONE_PAIRS = 1000000
REMAINING_CHECKS = (
    "composite-coverage-engineering-acceptance", "composite-minimum-width",
    "a101-composite-positions", "host-boundary-cover-openings", "xy-layer-order",
    "background-and-additions-3d-collisions", "postprocessing-conflicts",
    "project-axis-origins-approval", "revit-readback",
)


def recipe_covers(required: ReinforcementRecipe, supplied: ReinforcementRecipe) -> bool:
    """Упорядоченные компоненты, тот же фон/диаметр, не более редкий условный шаг.

    Не заменяет две добавки одной эквивалентной по суммарному As; не переставляет
    компоненты. Это консервативный профиль, а не разрешение замены арматуры по нормам.
    """
    return (required.background == supplied.background
            and len(supplied.additions) >= len(required.additions)
            and all(a.diameter == b.diameter and b.step <= a.step
                    for a, b in zip(required.additions, supplied.additions)))


def monotone_single_recipe_covers(
    required: ReinforcementRecipe, supplied: ReinforcementRecipe,
) -> bool:
    """Research-only: same background, ONE addition, no thinner AND no sparser.

    No ordering of level indices, summed As, diameter/spacing trade-off or multiple
    add-ons can establish this relation. A true result is not engineering approval
    for diameter substitution or a particular layer depth/anchorage arrangement.
    """
    return (required.background == supplied.background
            and len(required.additions) == len(supplied.additions) == 1
            and supplied.additions[0].diameter >= required.additions[0].diameter
            and supplied.additions[0].step <= required.additions[0].step)


def _same_origin(first: AxisPlacement, second: AxisPlacement) -> bool:
    if first.origin_mm is None or second.origin_mm is None or first.pattern != second.pattern:
        return False
    period = first.pattern.period_mm
    return abs(math.remainder(first.origin_mm - second.origin_mm, period)) <= GEOMETRY_TOLERANCE_MM


def _check_patterns(zone: CompositeLayoutZone, policy_id: str) -> None:
    background = zone.placement.background
    if (zone.recipe.background.step != 300 or background.pattern.period_mm != 300
            or background.pattern.offsets_mm != (0,)):
        raise ValueError("профиль покрытия требует равномерный фон @300")
    for i, (spec, axes) in enumerate(zip(zone.recipe.additions, zone.placement.additions)):
        if spec.step == 100 and policy_id in {
            STO_279_COVERAGE_POLICY, MONOTONE_SINGLE_STO_COVERAGE_POLICY,
        }:
            delta = (spec.diameter + zone.recipe.background.diameter) / 2
            expected_contact = ((100.0, 200.0, 300 - delta), (delta, 100.0, 200.0))
            if (len(zone.recipe.additions) != 1 or axes.pattern.period_mm != 300
                    or axes.pattern.offsets_mm not in expected_contact
                    or abs(math.remainder(axes.origin_mm - background.origin_mm, 300)) > GEOMETRY_TOLERANCE_MM):
                raise ValueError("@100: нужна схема СТО с отдельной осью вплотную к фону, не равномерная сетка")
            continue
        expected = {300: (0,), 150: (100, 200)}.get(spec.step)
        if expected is None or axes.pattern.period_mm != 300 or axes.pattern.offsets_mm != expected:
            raise ValueError(f"добавка {i + 1}: профиль поддерживает @300 или условный @150 с осями 100/200")
        if i == 0 and spec.step == 150:
            # Для первой добавки известен не только средний шаг, но и положение между фоном.
            delta = axes.origin_mm - background.origin_mm
            if abs(math.remainder(delta, 300)) > GEOMETRY_TOLERANCE_MM:
                raise ValueError("первая добавка @150 не согласована с фазой фоновой сетки")


def _service_interval(axes: AxisPlacement, window: tuple[float, float]) -> tuple[float, float]:
    """Граница обслуживания — середина до соседней оси бесконечного шаблона.

    Разворачивать 100000 отдельных координат не нужно. Для 100/200 крайнее полуполе
    бывает 50 или 100 мм, а не всегда 75 мм от условного @150. Интервал НЕ равен cover.
    """
    runs = pattern_runs(axes, window)
    if not runs:
        raise ValueError("пустое окно осей")
    first = min(r.first_axis_mm for r in runs)
    last = max(r.first_axis_mm + (r.bar_count - 1) * r.actual_step_mm for r in runs)
    period = axes.pattern.period_mm

    def neighbour_gap(coordinate: float, before: bool) -> float:
        gaps = []
        for offset in axes.pattern.offsets_mm:
            delta = ((coordinate - axes.origin_mm - offset) if before
                     else (axes.origin_mm + offset - coordinate)) % period
            gaps.append(period if min(delta, period - delta) <= GEOMETRY_TOLERANCE_MM else delta)
        return min(gaps)

    return first - neighbour_gap(first, True) / 2, last + neighbour_gap(last, False) / 2


def _component_bboxes(zone: CompositeLayoutZone) -> tuple[BBox, ...]:
    start, end = longitudinal_interval(zone.direction.axis, zone.demand_bbox)
    result = []
    for component in zone.components:
        lo, hi = _service_interval(component.placement, component.axis_window_mm)
        # Анкеровка и запас раскроя не дают права закрыть спрос за заданной demand_bbox.
        window_lo, window_hi = component.axis_window_mm
        lo, hi = max(lo, window_lo), min(hi, window_hi)
        result.append((start, lo, end, hi) if zone.direction.axis is Axis.X else (lo, start, hi, end))
    return tuple(result)


def _intersection(boxes: Sequence[BBox]) -> BBox:
    return (max(b[0] for b in boxes), max(b[1] for b in boxes),
            min(b[2] for b in boxes), min(b[3] for b in boxes))


def validate_composite_demand(demand: DemandMap) -> None:
    if not demand.levels or len(demand.cells) > MAX_CELLS:
        raise ValueError("пустая шкала или превышен лимит КЭ")
    if [level.index for level in demand.levels] != list(range(len(demand.levels))):
        raise ValueError("непоследовательные индексы шкалы")
    if any(level.recipe is None or level.requires_extra is None for level in demand.levels):
        raise ValueError("покрытие требует явные составы всех уровней, не только интервалы As")
    if len({level.recipe.background for level in demand.levels}) != 1:
        raise ValueError("разные фоновые спецификации требуют отдельного профиля")
    if len({cell.id for cell in demand.cells}) != len(demand.cells):
        raise ValueError("повторяющиеся ID КЭ")
    for cell in demand.cells:
        demand.level(cell.level_index)
        p = cell.poly
        if len(p) not in (3, 4) or any(len(v) != 2 or not all(
                not isinstance(x, bool) and math.isfinite(x) for x in v) for v in p):
            raise ValueError(f"КЭ {cell.id}: нужны конечные вершины треугольника/квада")
        cross = [(b[0]-a[0])*(c[1]-b[1]) - (b[1]-a[1])*(c[0]-b[0])
                 for a, b, c in zip(p, (*p[1:], p[0]), (*p[2:], *p[:2]))]
        if (not math.isfinite(polygon_area(p)) or not all(math.isfinite(x) for x in cross)
                or polygon_area(p) <= GEOMETRY_TOLERANCE_MM or (min(cross) < -1e-6 and max(cross) > 1e-6)):
            raise ValueError(f"КЭ {cell.id}: вырожденная/невыпуклая геометрия не поддержана")


def check_composite_zone_coverage_geometry(
    demand: DemandMap, zone: CompositeLayoutZone, *, constraints: LayoutConstraints | None = None,
    context: CompositeDetailingContext | None = None,
    policy_id: str = COMPOSITE_COVERAGE_POLICY,
) -> CompositeCoverageZoneCheck:
    """Общая проверка одного кандидата без пересчёта всех КЭ; не полный coverage."""
    check = evaluate_composite_zone(demand, zone, constraints=constraints, context=context)
    errors = [message for message in check.diagnostics if message.startswith("ERROR:")]
    boxes = ()
    if not errors:
        try:
            _check_patterns(zone, policy_id)
            boxes = _component_bboxes(zone)
        except (ValueError, TypeError, OverflowError) as error:
            errors.append(f"ERROR: {error}")
    return CompositeCoverageZoneCheck(zone.id, not errors, boxes,
        check.physical_bar_count if not errors else None, check.total_mass_kg if not errors else None, tuple(errors))


def evaluate_composite_coverage(
    demand: DemandMap,
    zones: Sequence[CompositeLayoutZone],
    *,
    policy_id: str,
    constraints: LayoutConstraints | None = None,
) -> CompositeCoverageEvaluation:
    """Проверить геометрию и покрытие всех КЭ одним направлением; ничего не исправлять.

    Один фрагмент КЭ должен обслуживаться ВСЕМИ необходимыми добавками одной зоны.
    Фрагменты достаточных зон объединяются геометрически, не суммированием площадей.
    Пользователь обязан явно выбрать research-профиль; placement_eligible всегда false.
    """
    if policy_id not in (COMPOSITE_COVERAGE_POLICY, STO_279_COVERAGE_POLICY,
                         MONOTONE_SINGLE_STO_COVERAGE_POLICY):
        raise ValueError("нужен явно выбранный поддерживаемый research-профиль покрытия")
    if len(zones) > MAX_ZONES or len(zones) * len(demand.cells) > MAX_CELL_ZONE_PAIRS:
        raise ValueError("превышен лимит зон/пар КЭ-зона; неполная проверка не разрешена")
    validate_composite_demand(demand)
    monotone_single = policy_id == MONOTONE_SINGLE_STO_COVERAGE_POLICY
    if monotone_single and (
        any(len(level.recipe.additions) > 1 for level in demand.levels)
        or any(len(zone.recipe.additions) != 1 for zone in zones)
    ):
        raise ValueError("monotone-single research policy supports exactly one additional set, not composite recipes")
    constraints = constraints or LayoutConstraints()
    if constraints.allow_overcoverage is not True:
        raise ValueError("профиль без избыточного покрытия пока не поддержан")
    if not isinstance(constraints.allow_overlaps, bool):
        raise ValueError("allow_overlaps должен быть bool")
    if (isinstance(constraints.min_width_cells, bool) or not isinstance(constraints.min_width_cells, int)
            or constraints.min_width_cells < 1):
        raise ValueError("min_width_cells должен быть положительным целым")
    checks, diagnostics = [], []
    duplicate_ids = {zone.id for zone in zones if sum(other.id == zone.id for other in zones) > 1}
    for zone in zones:
        check = check_composite_zone_coverage_geometry(demand, zone, constraints=constraints, policy_id=policy_id)
        if zone.id in duplicate_ids:
            check = CompositeCoverageZoneCheck(zone.id, False, (), None, None,
                                              (*check.diagnostics, "ERROR: повторяющийся ID зоны"))
        checks.append(check)
    usable = [(zone, check) for zone, check in zip(zones, checks) if check.geometry_and_pattern_valid]
    if usable and any(not _same_origin(usable[0][0].placement.background, z.placement.background) for z, _ in usable):
        diagnostics.append("ERROR: зоны используют разные фоновые сетки; их нельзя объединить в одно покрытие")
        usable = []
    if not constraints.allow_overlaps:
        for i, (first, _) in enumerate(usable):
            if any(bboxes_overlap(first.demand_bbox, second.demand_bbox) for second, _ in usable[i + 1:]):
                diagnostics.append("ERROR: пересечение зон запрещено выбранными constraints")
                break

    cells = []
    diameter_substitutions: dict[tuple[str, int, int], set[int]] = {}
    covers = monotone_single_recipe_covers if monotone_single else recipe_covers
    for cell in demand.cells:
        required = demand.level(cell.level_index).recipe
        if not required.additions:
            continue  # Фон не пересчитывается как добавочная арматура.
        boxes, ids = [], []
        for zone, check in usable:
            if not covers(required, zone.recipe):
                continue
            box = _intersection(check.component_service_bboxes_mm[:len(required.additions)])
            if box[2] > box[0] and box[3] > box[1] and polygon_bbox_intersection_area(cell.poly, box) > 0:
                boxes.append(box)
                ids.append(zone.id)
                if monotone_single and zone.recipe.additions[0].diameter != required.additions[0].diameter:
                    key = (zone.id, required.additions[0].diameter, zone.recipe.additions[0].diameter)
                    diameter_substitutions.setdefault(key, set()).add(cell.id)
        area = polygon_area(cell.poly)
        covered = polygon_bboxes_union_intersection_area(cell.poly, boxes)
        missing = max(0.0, area - covered)
        cells.append(CompositeCellCoverage(cell.id, cell.level_index, area, covered, missing,
                                          missing <= max(1e-6, area * 1e-9), tuple(ids)))
    remaining_checks = REMAINING_CHECKS
    if monotone_single:
        remaining_checks = (*remaining_checks, "monotone-diameter-substitution-engineering-approval")
        substitutions = tuple((zone_id, required_d, supplied_d, len(cell_ids))
                              for (zone_id, required_d, supplied_d), cell_ids
                              in sorted(diameter_substitutions.items()))
        diagnostics.append(
            "WARNING: explicit research-only single-addition substitution: same background, "
            "diameter no smaller AND nominal spacing no larger; engineering approval is absent. "
            f"Diameter-substitution zone/diameter pairs: {len(substitutions)}; "
            f"examples (zone, required diameter, supplied diameter, intersected cells): {substitutions[:10]}"
        )
    geometry_valid = not any(d.startswith("ERROR:") for d in diagnostics) and all(
        c.geometry_and_pattern_valid for c in checks)
    coverage_passed = all(c.covered for c in cells)
    return CompositeCoverageEvaluation(
        policy_id, "pass" if geometry_valid and coverage_passed else "fail", geometry_valid, coverage_passed,
        len(cells), sum(c.covered for c in cells), sum(not c.covered for c in cells),
        math.fsum(c.uncovered_area_mm2 for c in cells), len(zones), sum(len(z.components) for z in zones),
        sum(c.physical_bar_count for c in checks) if geometry_valid else None,
        math.fsum(c.additional_mass_kg for c in checks) if geometry_valid else None,
        tuple(cells), tuple(checks), tuple(diagnostics), remaining_checks,
    )
