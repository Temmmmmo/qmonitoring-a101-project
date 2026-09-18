"""Составной поиск в конечном пуле прямоугольников, с общей физической моделью.

Не адаптирует составную схему к последней добавке старого GA. Геометрии — узлы
многоуровневого пространственного разбиения, все совместимые рецепты сохраняются.
Масса/оси/покрытие — только общие сервисы. Не production-оптимум и не Revit apply.
"""
from __future__ import annotations

from collections import deque
import math
from time import perf_counter

from ..contracts.composite_search import CompositeSearchPoint, CompositeSearchProblem, CompositeSearchResult
from ..contracts.front import ComplexityAxis
from ..services.bar_schedule import straight_bar_key
from ..services.composite_coverage import evaluate_composite_coverage, recipe_covers
from ..services.composite_detailing import build_composite_zone, validate_recipe_placement
from ..services.composite_host import evaluate_composite_host, evaluate_host_demand_feasibility
from ..services.composite_host_fit import admissible_host_window, fit_composite_zone_to_host
from ..services.composite_interior import partition_interior_demand
from ..services.composite_mesh_domain import composite_mesh_domain, zone_inside_mesh
from ..services.composite_windows import covering_composite_window
from ..services.finite_cover import solve_finite_cover_front
from ..services.geometry import bboxes_overlap, polygon_in_bbox


def _bbox(cells):
    points = [point for cell in cells for point in cell.poly]
    return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)


def _groups(cells, depth):
    """Два детерминированных дерева медианных разрезов; не границы цвета."""
    queue = deque([(tuple(cells), 0)])
    known = set()
    while queue:
        group, level = queue.popleft()
        signature = tuple(cell.id for cell in group)
        if signature in known:
            continue
        known.add(signature)
        yield group
        if level >= depth or len(group) < 2:
            continue
        for axis in (0, 1):
            ordered = sorted(group, key=lambda c: (c.centroid[axis], c.id))
            coordinates = sorted({c.centroid[axis] for c in group})
            if len(coordinates) < 2:
                continue
            pivot = coordinates[len(coordinates) // 2]
            left = tuple(sorted((c for c in ordered if c.centroid[axis] < pivot), key=lambda c: c.id))
            right = tuple(sorted((c for c in ordered if c.centroid[axis] >= pivot), key=lambda c: c.id))
            queue.extend(((left, level + 1), (right, level + 1)))


def solve_composite_pool(
    problem: CompositeSearchProblem, *, maximum_zones: int = 64, maximum_candidates: int = 384,
    partition_depth: int = 6, solver_time_limit_s: float = 30.0,
    maximum_physical_bars: int | None = None, maximum_mass_kg: float | None = None,
    maximum_bar_length_mm: float | None = None,
    complexity_axis: ComplexityAxis = ComplexityAxis.ZONE_COUNT,
    maximum_positions: int | None = None, steel_class: str = "",
    retain_position_alternatives: bool = False,
    forbid_unmeshed_zones: bool = False,
) -> CompositeSearchResult:
    """Построить mass/zone-count фронт и выбрать ближайшую к идеалу точку (равные веса).

    Весь пул и выбранные раскладки проходят независимый research-валидатор. КЭ
    учитывается в матрице только при полном покрытии одним кандидатом; объединение
    нескольких достаточных фрагментов пока не расширяет пространство поиска.
    Опциональный запрет белых областей проверяет прямоугольники по union исходных
    КЭ до построения зоны и повторно после выбора MILP.
    """
    for value, low, high, name in ((maximum_zones, 1, 128, "maximum_zones"),
                                  (maximum_candidates, 1, 1024, "maximum_candidates"),
                                  (partition_depth, 0, 8, "partition_depth")):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{name} должен быть целым {low}..{high}")
    if (isinstance(solver_time_limit_s, bool) or not math.isfinite(solver_time_limit_s)
            or not 0 < solver_time_limit_s <= 300):
        raise ValueError("solver_time_limit_s должен быть в (0, 300]")
    if maximum_physical_bars is not None and (isinstance(maximum_physical_bars, bool)
            or not isinstance(maximum_physical_bars, int) or maximum_physical_bars < 1):
        raise ValueError("maximum_physical_bars должен быть положительным целым")
    if maximum_mass_kg is not None and (isinstance(maximum_mass_kg, bool)
            or not math.isfinite(maximum_mass_kg) or maximum_mass_kg <= 0):
        raise ValueError("maximum_mass_kg должна быть положительной конечной")
    if maximum_bar_length_mm is not None and (isinstance(maximum_bar_length_mm, bool)
            or not math.isfinite(maximum_bar_length_mm) or maximum_bar_length_mm <= 0):
        raise ValueError("maximum_bar_length_mm должна быть положительной конечной")
    complexity_axis = ComplexityAxis(complexity_axis)
    if complexity_axis not in (ComplexityAxis.ZONE_COUNT, ComplexityAxis.POSITION_COUNT):
        raise ValueError("составной пул поддерживает выбор по зонам или позициям")
    if maximum_positions is not None and (isinstance(maximum_positions, bool)
            or not isinstance(maximum_positions, int) or maximum_positions < 1):
        raise ValueError("maximum_positions должен быть положительным целым")
    if not isinstance(steel_class, str) or not isinstance(retain_position_alternatives, bool):
        raise ValueError("невалидный класс стали или режим сохранения кандидатов")
    demand, constraints = problem.demand, problem.constraints
    if not isinstance(forbid_unmeshed_zones, bool):
        raise ValueError("forbid_unmeshed_zones должен быть bool")
    mesh_domain = composite_mesh_domain(demand) if forbid_unmeshed_zones else None
    if problem.boundary_mode not in ("strict", "interior-exceptions") or (
            problem.boundary_mode == "interior-exceptions" and problem.host_envelope is None):
        raise ValueError("неизвестный boundary_mode или отсутствует host для внутренних исключений")
    partial = problem.boundary_mode == "interior-exceptions"
    if constraints.allow_overlaps is not True:
        raise ValueError("составной пул пока требует явный research-профиль с пересечениями")
    started = perf_counter()
    empty = evaluate_composite_coverage(demand, (), policy_id=problem.policy_id, constraints=constraints)
    placements = dict(problem.placements)
    if len(placements) != len(problem.placements):
        raise ValueError("повторяющиеся уровни размещения")
    needed = [level for level in demand.levels if level.requires_extra]
    if set(placements) != {level.index for level in needed}:
        raise ValueError("нужны явные фазы ВСЕХ дополнительных уровней шкалы")
    for level in needed:
        validate_recipe_placement(level.recipe, placements[level.index])
    cells = tuple(sorted((c for c in demand.cells if demand.level(c.level_index).requires_extra), key=lambda c: c.id))
    telemetry = {"algorithm": "composite-pool-milp/v1", "scope": "research-only",
                 "maximum_candidates": maximum_candidates, "maximum_zones": maximum_zones,
                 "partition_depth": partition_depth, "pool_truncated": False, "rejected_proposals": 0,
                 "candidate_errors": [], "selection": "minimum_equal_weight_normalized_distance_to_ideal",
                 "engineering_optimality_proven": False, "host_rejected_candidates": 0,
                 "solver_executed": False, "maximum_physical_bars": maximum_physical_bars,
                 "maximum_mass_kg": maximum_mass_kg, "maximum_bar_length_mm": maximum_bar_length_mm,
                 "bar_length_rejected_candidates": 0}
    telemetry["forbid_unmeshed_zones"] = forbid_unmeshed_zones
    telemetry["unmeshed_zone_rejected_candidates"] = 0
    telemetry["host_shifted_components"] = 0
    telemetry.update(complexity_axis=complexity_axis.value, maximum_positions=maximum_positions,
                     retain_position_alternatives=retain_position_alternatives)
    if problem.host_envelope is not None:
        feasibility = evaluate_host_demand_feasibility(demand, problem.host_envelope, constraints=constraints)
        telemetry["host_demand_feasibility"] = feasibility
        if feasibility["requires_engineering_decision"] and not partial:
            telemetry.update(candidate_count=0, solver_executed=False, runtime_s=perf_counter() - started)
            return CompositeSearchResult((), None, telemetry)
    limits = {}
    if partial:
        partition = partition_interior_demand(problem)
        telemetry["interior_partition"] = partition
        limits = partition["admissible_bboxes_by_level_mm"]
        target_ids = set(partition["target_cell_ids"])
        cells = tuple(cell for cell in cells if cell.id in target_ids)
    if not cells:
        if partial:
            return CompositeSearchResult((), None, telemetry)
        return CompositeSearchResult((CompositeSearchPoint((), empty),), 0, telemetry)
    indexes = {cell.id: i for i, cell in enumerate(cells)}
    zones, masks, masses, bars, known = [], [], [], [], set()
    for group in _groups(cells, partition_depth):
        for level in needed:
            served = [cell for cell in group if recipe_covers(demand.level(cell.level_index).recipe, level.recipe)
                      and (not partial or (limits[level.index] is not None and polygon_in_bbox(cell.poly, limits[level.index])))]
            if not served:
                continue
            try:
                admissible = (limits.get(level.index) if partial else admissible_host_window(
                    demand, level.index, problem.host_envelope, constraints) if problem.host_envelope else None)
                box = covering_composite_window(demand, _bbox(served), level.index, placements[level.index], constraints=constraints,
                                                admissible_bbox=admissible)
            except ValueError as error:
                telemetry["candidate_errors"].append({"level_index": level.index, "bbox_mm": _bbox(served), "error": str(error)})
                continue
            signature = box, level.index
            if mesh_domain is not None and not zone_inside_mesh(mesh_domain, box):
                telemetry["unmeshed_zone_rejected_candidates"] += 1
                continue
            if signature in known:
                continue
            known.add(signature)
            if len(known) > maximum_candidates:
                telemetry["pool_truncated"] = True
                break
            try:
                zone = build_composite_zone(demand, box, level.index, f"CP-{len(zones) + 1:04d}",
                                            placements[level.index], constraints=constraints)
                if problem.host_envelope is not None:
                    try:
                        fitted = fit_composite_zone_to_host(demand, zone, problem.host_envelope, constraints=constraints)
                    except ValueError:
                        telemetry["host_rejected_candidates"] += 1
                        raise
                    telemetry["host_shifted_components"] += sum(a.longitudinal_interval_mm != b.longitudinal_interval_mm
                        for a, b in zip(zone.components, fitted.components))
                    zone = fitted
                if maximum_bar_length_mm is not None and any(
                        component.installed_length_mm > maximum_bar_length_mm + 1e-6 for component in zone.components):
                    telemetry["bar_length_rejected_candidates"] += 1
                    continue
                check = evaluate_composite_coverage(demand, (zone,), policy_id=problem.policy_id, constraints=constraints)
                if not check.geometry_and_patterns_valid:
                    raise ValueError(str(check.zones[0].diagnostics))
                if problem.host_envelope is not None:
                    host_check = evaluate_composite_host(demand, (zone,), problem.host_envelope, constraints=constraints)
                    if "fail" in host_check["checks"].values():
                        telemetry["host_rejected_candidates"] += 1
                        continue
            except ValueError as error:
                telemetry["candidate_errors"].append({"level_index": level.index, "bbox_mm": box, "error": str(error)})
                continue
            mask = frozenset(indexes[cell.cell_id] for cell in check.cells if cell.covered and cell.cell_id in indexes)
            if mask:
                zones.append(zone)
                masks.append(mask)
                masses.append(check.additional_mass_kg)
                bars.append(check.physical_bar_count)
        if telemetry["pool_truncated"]:
            break
    telemetry.update(candidate_count=len(zones), candidate_build_s=perf_counter() - started)
    # First seek a feasible mass edge, then a small-zone edge and intermediate caps.
    budgets = tuple(dict.fromkeys((maximum_zones, 1, *[min(maximum_zones, b) for b in (2, 4, 8, 16, 32, 64, 128)])))
    conflicts = tuple((i, j) for i, zone in enumerate(zones) for j in range(i)
                      if bboxes_overlap(zone.demand_bbox, zones[j].demand_bbox)) if problem.host_envelope else ()
    positions = tuple(frozenset(straight_bar_key(c.rebar.diameter, c.installed_length_mm, steel_class)
                               for c in zone.components) for zone in zones)
    position_mode = complexity_axis is ComplexityAxis.POSITION_COUNT or maximum_positions is not None
    proposals, solver = solve_finite_cover_front(tuple(masks), tuple(masses), len(cells),
                                               maximum_zones=maximum_zones, time_limit_s=solver_time_limit_s, budgets=budgets,
                                               incompatible_pairs=conflicts, bar_counts=tuple(bars),
                                               maximum_physical_bars=maximum_physical_bars, maximum_mass_kg=maximum_mass_kg,
                                               position_keys=positions if position_mode else None,
                                               maximum_positions=maximum_positions,
                                               position_budgets=(1, 2, 4, 8, 16, 32, 64) if position_mode else ())
    telemetry["solver"] = solver
    telemetry["solver_executed"] = bool(solver["solves"])
    points = []
    for proposal in proposals:
        chosen = tuple(zones[i] for i in proposal)
        if mesh_domain is not None and any(not zone_inside_mesh(mesh_domain, zone.demand_bbox) for zone in chosen):
            telemetry["rejected_proposals"] += 1
            continue
        check = evaluate_composite_coverage(demand, chosen, policy_id=problem.policy_id, constraints=constraints)
        host_failed = problem.host_envelope is not None and "fail" in evaluate_composite_host(
            demand, chosen, problem.host_envelope, constraints=constraints)["checks"].values()
        target_covered = all(cell.covered for cell in check.cells if cell.cell_id in indexes)
        over_budget = ((maximum_physical_bars is not None and check.physical_bar_count is not None
                        and check.physical_bar_count > maximum_physical_bars)
                       or (maximum_mass_kg is not None and check.additional_mass_kg is not None
                           and check.additional_mass_kg > maximum_mass_kg + 1e-6)
                       or (maximum_bar_length_mm is not None and any(c.installed_length_mm > maximum_bar_length_mm + 1e-6
                                                                 for z in chosen for c in z.components)))
        if not check.geometry_and_patterns_valid or not target_covered or (not partial and check.status != "pass") or host_failed or over_budget:
            telemetry["rejected_proposals"] += 1
            continue
        points.append(CompositeSearchPoint(chosen, check))

    def count(point):
        if complexity_axis is ComplexityAxis.ZONE_COUNT:
            return len(point.zones)
        return len({straight_bar_key(c.rebar.diameter, c.installed_length_mm, steel_class)
                    for zone in point.zones for c in zone.components})

    points.sort(key=lambda p: (count(p), p.coverage.additional_mass_kg, tuple(z.id for z in p.zones)))
    front, best_mass = [], math.inf
    for point in points:
        if point.coverage.additional_mass_kg < best_mass - 1e-6:
            front.append(point)
            best_mass = point.coverage.additional_mass_kg
    if retain_position_alternatives:
        # Different type sets can become equivalent only AFTER all four directions.
        front = points
    selected = None
    if front:
        min_mass, max_mass = min(p.coverage.additional_mass_kg for p in front), max(p.coverage.additional_mass_kg for p in front)
        min_count, max_count = min(map(count, front)), max(map(count, front))
        selected = min(range(len(front)), key=lambda i: (
            ((front[i].coverage.additional_mass_kg - min_mass) / max(max_mass - min_mass, 1e-6)) ** 2
            + ((count(front[i]) - min_count) / max(max_count - min_count, 1)) ** 2,
            front[i].coverage.additional_mass_kg, len(front[i].zones)))
    telemetry["runtime_s"] = perf_counter() - started
    return CompositeSearchResult(tuple(front), selected, telemetry)
