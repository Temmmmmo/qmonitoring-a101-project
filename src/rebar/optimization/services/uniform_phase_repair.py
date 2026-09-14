"""Opt-in сдвиг uniform-наборов без изменения спроса и количества материала.

Это локальное улучшение существующей плоской модели LayoutZone, не проверка
периодических схем А101, фоновой сетки, рабочего host или высот слоёв.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from copy import deepcopy
import math

from ..contracts import AlgorithmRequest, LayoutEvaluation, LayoutProblem, LayoutZone
from .bar_geometry import (
    bars_conflict,
    intervals_overlap,
    longitudinal_interval,
    transverse_axis_gap,
)
from .detailing import DetailingContext, build_zone_from_bbox, prepare_detailing
from .evaluation import evaluate_layout
from .geometry import GEOMETRY_TOLERANCE_MM
from .phase import feasible_first_bar_coordinates

ZonePair = tuple[str, str]


@dataclass(frozen=True)
class UniformPhaseRepairResult:
    """Пары зон означают наличие хотя бы одной конфликтующей пары стержней."""

    zones: tuple[LayoutZone, ...]
    conflicting_zone_pairs_before: tuple[ZonePair, ...]
    conflicting_zone_pairs_after: tuple[ZonePair, ...]
    zone_gap_pairs_before: tuple[ZonePair, ...]
    zone_gap_pairs_after: tuple[ZonePair, ...]
    evaluation: LayoutEvaluation
    moves: int
    passes: int
    pair_checks: int
    budget_exhausted: bool
    limitations: tuple[str, ...]
    placement_eligible: bool = False


def _pair_audit(
    problem: LayoutProblem, zones: tuple[LayoutZone, ...],
) -> tuple[tuple[ZonePair, ...], tuple[ZonePair, ...]]:
    conflicts, gaps = [], []
    axis = problem.demand.direction.axis
    for index, first in enumerate(zones):
        for second in zones[index + 1:]:
            pair = tuple(sorted((first.id, second.id)))
            if bars_conflict(axis, first, second,
                             minimum_clear_spacing_mm=problem.constraints.minimum_clear_spacing_mm):
                conflicts.append(pair)
            if (problem.constraints.enforce_zone_gap
                    and intervals_overlap(longitudinal_interval(axis, first.bbox),
                                          longitudinal_interval(axis, second.bbox))
                    and transverse_axis_gap(axis, first, second) + GEOMETRY_TOLERANCE_MM
                    < max(first.rebar.step, second.rebar.step)):
                gaps.append(pair)
    return tuple(sorted(conflicts)), tuple(sorted(gaps))


def _rebuild(
    problem: LayoutProblem, original: LayoutZone, coordinate: float, context: DetailingContext | None,
) -> LayoutZone:
    seed_ids = original.meta.get("seed_cell_ids")
    if (not isinstance(seed_ids, (tuple, list))
            or any(isinstance(item, bool) or not isinstance(item, int) for item in seed_ids)):
        raise ValueError(f"зона {original.id} не хранит целые seed_cell_ids для phase repair")
    if context is None or any(item not in context.cells_by_id for item in seed_ids):
        raise ValueError(f"зона {original.id} содержит неизвестные исходные seed_cell_ids")
    # Provenance старой раскладки может включать КЭ, чей исходный уровень выше.
    # Они обязаны быть покрыты другой достаточной зоной (проверено общим evaluator),
    # но не меняют уровень и геометрию этой зоны при поперечном перемещении.
    candidate = build_zone_from_bbox(
        problem, original.demand_bbox, original.level_index, original.id,
        context=context, first_bar_coordinate_mm=coordinate,
    )
    if (candidate.rebar != original.rebar or candidate.bar_count != original.bar_count
            or candidate.demand_bbox != original.demand_bbox
            or set(candidate.covered_cell_ids) != set(original.covered_cell_ids)):
        raise ValueError(f"phase repair изменил состав или покрытие зоны {original.id}")
    for name in ("width_mm", "required_length_mm", "anchored_length_mm", "installed_length_mm", "mass_kg"):
        if not math.isclose(getattr(candidate, name), getattr(original, name), rel_tol=1e-9, abs_tol=1e-6):
            raise ValueError(f"phase repair изменил {name} зоны {original.id}")
    if longitudinal_interval(problem.demand.direction.axis, candidate.bbox) != longitudinal_interval(
        problem.demand.direction.axis, original.bbox,
    ):
        raise ValueError(f"phase repair изменил продольную геометрию зоны {original.id}")
    return replace(candidate, meta=deepcopy(original.meta))


def _phases(
    problem: LayoutProblem, zone: LayoutZone, neighbors: tuple[LayoutZone, ...],
    *, maximum_candidates: int,
) -> tuple[float, ...]:
    """Общие 9 фаз и ограниченные события касания ближайших физических осей."""
    common = feasible_first_bar_coordinates(problem, zone)
    lower, upper = min(common), max(common)
    current = zone.first_bar_coordinate_mm
    values = {current, *common}
    events: set[float] = set()
    event_axis_checks = 0
    maximum_event_axes = max(64, maximum_candidates * 16)
    for other in sorted(neighbors, key=lambda item: item.id):
        distance = (zone.rebar.diameter + other.rebar.diameter) / 2 + problem.constraints.minimum_clear_spacing_mm
        first_index = max(0, math.ceil((lower - distance - other.first_bar_coordinate_mm) / other.rebar.step))
        last_index = min(other.bar_count, 1 + math.floor(
            (upper + zone.width_mm + distance - other.first_bar_coordinate_mm) / other.rebar.step,
        ))
        for other_index in range(first_index, last_index):
            if event_axis_checks >= maximum_event_axes:
                break
            event_axis_checks += 1
            coordinate = other.first_bar_coordinate_mm + other_index * other.rebar.step
            nearest = math.floor((coordinate - current) / zone.rebar.step)
            for index in (nearest, nearest + 1):
                if not 0 <= index < zone.bar_count:
                    continue
                for sign in (-1, 1):
                    phase = coordinate + sign * distance - index * zone.rebar.step
                    if lower <= phase <= upper:
                        events.add(phase)
        if event_axis_checks >= maximum_event_axes:
            break
    for phase in sorted(events - values, key=lambda value: (abs(value - current), value)):
        if len(values) >= maximum_candidates:
            break
        values.add(phase)
    return tuple(sorted(values, key=lambda value: (abs(value - current), value)))


def repair_uniform_zone_phases(
    problem: LayoutProblem,
    zones: tuple[LayoutZone, ...] | list[LayoutZone],
    *,
    request: AlgorithmRequest | None = None,
    maximum_passes: int = 8,
    maximum_pair_checks: int = 100_000,
    maximum_phase_candidates: int = 33,
) -> UniformPhaseRepairResult:
    """Детерминированный coordinate descent по реальным ``bars_conflict``.

    Каждый принятый сдвиг строго уменьшает число конфликтующих пар uniform-зон.
    Длина/диаметр/шаг/количество/масса/40d, demand_bbox и исходный спрос неизменны.
    Общий конструктор проверяет допустимый диапазон фазы; независимый валидатор
    проверяет всю выдачу до и после. Zone-gap не выдаётся за физическую коллизию
    и остаётся отдельным результатом. Бюджет ограничивает search pair checks;
    две независимые финальные/начальные проверки в него не включены.

    Это явный opt-in: функция нигде не вызывается автоматически и не допускает
    выдачу в Revit. В частности, uniform @150 не подтверждает схему 100/200.
    """
    for name, value, minimum in (("maximum_passes", maximum_passes, 0),
                                  ("maximum_pair_checks", maximum_pair_checks, 0),
                                  ("maximum_phase_candidates", maximum_phase_candidates, 10)):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} должен быть целым не меньше {minimum}")
    if (not math.isfinite(problem.constraints.minimum_clear_spacing_mm)
            or problem.constraints.minimum_clear_spacing_mm < 0):
        raise ValueError("minimum_clear_spacing_mm должен быть конечным и неотрицательным")
    original = tuple(zones)
    before = evaluate_layout(problem, original, request)
    if not before.valid:
        raise ValueError("phase repair требует исходной hard-valid раскладки: " + "; ".join(
            message for message in before.diagnostics if message.startswith("ERROR:")
        ))
    conflicts_before, gaps_before = _pair_audit(problem, original)
    context = prepare_detailing(problem) if original else None
    current = list(original)
    axis = problem.demand.direction.axis
    # Продольный интервал неизменен, поэтому исключённая здесь пара не может
    # стать конфликтующей при любом поперечном сдвиге.
    neighbors = [tuple(j for j, other in enumerate(original)
                       if i != j and intervals_overlap(longitudinal_interval(axis, zone.bbox),
                                                       longitudinal_interval(axis, other.bbox)))
                 for i, zone in enumerate(original)]
    edge_conflicts = {
        (i, j): bars_conflict(axis, original[i], original[j],
                             minimum_clear_spacing_mm=problem.constraints.minimum_clear_spacing_mm)
        for i in range(len(original)) for j in neighbors[i] if i < j
    }
    moves = passes = pair_checks = 0
    exhausted = False
    for _pass in range(maximum_passes):
        changed = False
        order = sorted(range(len(original)), key=lambda i: (
            -sum(edge_conflicts[tuple(sorted((i, j)))] for j in neighbors[i]), original[i].id,
        ))
        for i in order:
            adjacent = neighbors[i]
            previous_count = sum(edge_conflicts[tuple(sorted((i, j)))] for j in adjacent)
            if not previous_count:
                continue
            best, best_count, best_edges = current[i], previous_count, None
            conflicting_neighbors = tuple(current[j] for j in adjacent
                                          if edge_conflicts[tuple(sorted((i, j)))])
            for coordinate in _phases(problem, current[i], conflicting_neighbors,
                                      maximum_candidates=maximum_phase_candidates):
                if abs(coordinate - current[i].first_bar_coordinate_mm) <= GEOMETRY_TOLERANCE_MM:
                    continue
                if pair_checks + len(adjacent) > maximum_pair_checks:
                    exhausted = True
                    break
                candidate = _rebuild(problem, original[i], coordinate, context)
                if not problem.constraints.allow_overcoverage and candidate.overcovered_cell_ids:
                    continue
                checked = tuple(bars_conflict(
                    axis, candidate, current[j],
                    minimum_clear_spacing_mm=problem.constraints.minimum_clear_spacing_mm,
                ) for j in adjacent)
                pair_checks += len(adjacent)
                if sum(checked) < best_count:
                    best, best_count, best_edges = candidate, sum(checked), checked
                    if not best_count:
                        break
            if best_edges is not None:
                current[i] = best
                for j, conflict in zip(adjacent, best_edges, strict=True):
                    edge_conflicts[tuple(sorted((i, j)))] = conflict
                moves += 1
                changed = True
            if exhausted:
                break
        passes += 1
        if exhausted or not changed:
            break
    result = tuple(current)
    after = evaluate_layout(problem, result, request)
    conflicts_after, gaps_after = _pair_audit(problem, result)
    if (not after.valid or len(conflicts_after) > len(conflicts_before)
            or after.metrics.under_reinforced_cell_count != before.metrics.under_reinforced_cell_count
            or after.metrics.physical_bar_count != before.metrics.physical_bar_count
            or not math.isclose(after.metrics.total_mass_kg, before.metrics.total_mass_kg,
                                rel_tol=1e-9, abs_tol=1e-6)):
        raise ValueError("независимая проверка отклонила результат uniform phase repair")
    limitations = (
        "Проверены только параллельные uniform-оси одного направления; не профиль 3D-укладки.",
        "Host, проёмы, существующий фон, X/Y и нормативные периодические/контактные схемы не проверены.",
        "Локальный ограниченный поиск не доказывает отсутствие допустимого решения при остаточных конфликтах.",
    )
    if any(zone.rebar.step == 150 for zone in original):
        limitations += ("Uniform @150 не воспроизводит подтверждённую схему осей 100/200 мм.",)
    return UniformPhaseRepairResult(
        result, conflicts_before, conflicts_after, gaps_before, gaps_after, after,
        moves, passes, pair_checks, exhausted, limitations,
    )
