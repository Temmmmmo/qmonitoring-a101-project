"""Гильотинные разрезы по атомам КЭ с неизменным полным demand и общим checker.

Конечная сетка, скалярные прогоны mass + lambda*zones + mu*bars, не глобальный
Парето-фронт. Стоимость каждого прямоугольника после 40d/раскроя всех добавок.
"""
from __future__ import annotations

import math
from time import perf_counter

from ..contracts.composite_search import CompositeSearchPoint, CompositeSearchProblem, CompositeSearchResult
from ..services.composite_coverage import check_composite_zone_coverage_geometry, evaluate_composite_coverage, recipe_covers
from ..services.composite_detailing import build_composite_zone, prepare_composite_detailing, validate_recipe_placement
from ..services.composite_grid import grid_bbox, make_fragment_grid
from ..services.composite_host import evaluate_composite_host
from ..services.composite_interior import partition_interior_demand
from ..services.geometry import GEOMETRY_TOLERANCE_MM


def solve_composite_partition(
    problem: CompositeSearchProblem, *, along_step_mm: float = 1800, across_step_mm: float = 900,
    across_origin_mm: float | None = None,
    maximum_zones: int = 64, maximum_physical_bars: int | None = None,
    maximum_mass_kg: float | None = None, maximum_bar_length_mm: float | None = None,
    zone_penalties: tuple[float, ...] = (0, 25, 75, 150, 300, 600, 1200),
    bar_penalties: tuple[float, ...] = (0, 10, 30), time_limit_s: float = 120,
) -> CompositeSearchResult:
    if problem.boundary_mode != "interior-exceptions" or problem.host_envelope is None:
        raise ValueError("фрагментный эксперимент требует явный interior-exceptions и host")
    if maximum_zones is None or time_limit_s is None:
        raise ValueError("лимиты зон и времени обязательны")
    for value, name in ((maximum_zones, "maximum_zones"), (maximum_physical_bars, "maximum_physical_bars")):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise ValueError(name + " должен быть положительным целым")
    if maximum_zones > 128:
        raise ValueError("не более 128 зон")
    for value in (maximum_mass_kg, maximum_bar_length_mm, time_limit_s):
        if value is not None and (isinstance(value, bool) or not math.isfinite(value) or value <= 0):
            raise ValueError("невалидный лимит массы/длины/времени")
    if time_limit_s > 300 or not zone_penalties or not bar_penalties or len(zone_penalties) * len(bar_penalties) > 32:
        raise ValueError("не более 300 секунд и 32 скалярных прогонов")
    if any(isinstance(v, bool) or not math.isfinite(v) or v < 0 for v in (*zone_penalties, *bar_penalties)):
        raise ValueError("штрафы должны быть конечными неотрицательными")
    started = perf_counter()
    demand, constraints, placements = problem.demand, problem.constraints, dict(problem.placements)
    empty = evaluate_composite_coverage(demand, (), policy_id=problem.policy_id, constraints=constraints)
    needed = tuple(level for level in demand.levels if level.requires_extra)
    if len(placements) != len(problem.placements) or set(placements) != {level.index for level in needed}:
        raise ValueError("нужны явные фазы всех разрешённых рецептов без повторов")
    for level in needed:
        validate_recipe_placement(level.recipe, placements[level.index])
        if any(isinstance(spec.step, bool) or not isinstance(spec.step, int) for spec in level.recipe.additions):
            raise ValueError("сетка требует целые шаги")
    partition = partition_interior_demand(problem)
    telemetry = {"algorithm": "composite-fragment-guillotine/v1", "interior_partition": partition,
        "solver_executed": False, "engineering_optimality_proven": False, "timed_out": False,
        "maximum_zones": maximum_zones, "maximum_physical_bars": maximum_physical_bars,
        "maximum_mass_kg": maximum_mass_kg, "maximum_bar_length_mm": maximum_bar_length_mm,
        "rejected_proposals": 0, "candidate_count": 0, "scalar_runs": [],
        "selection": "minimum_equal_weight_normalized_distance_to_ideal",
        "scope": "finite_guillotine_grid_scalar_sweep_not_global_pareto"}
    if not partition["target_cell_ids"]:
        return CompositeSearchResult((), None, telemetry)
    grid = make_fragment_grid(problem, partition, along_step_mm=along_step_mm, across_step_mm=across_step_mm,
                              across_origin_mm=across_origin_mm)
    context = prepare_composite_detailing(demand)
    telemetry["fragment_grid"] = {k: v for k, v in grid.items() if k != "required_masks"}
    xs, ys = grid["along_coordinates_mm"], grid["across_coordinates_mm"]
    nx, ny = len(xs) - 1, len(ys) - 1
    masks, candidates, states = {}, {}, []
    for height in range(1, ny + 1):
        for width in range(1, nx + 1):
            for y in range(ny - height + 1):
                for x in range(nx - width + 1):
                    if perf_counter() - started > time_limit_s:
                        telemetry.update(timed_out=True, runtime_s=perf_counter() - started)
                        return CompositeSearchResult((), None, telemetry)
                    state = (x, x + width, y, y + height)
                    states.append(state)
                    mask = (masks[(x, x + width, y, y + height - 1)] | masks[(x, x + width, y + height - 1, y + height)]
                            if height > 1 else masks[(x, x + width - 1, y, y + 1)] | grid["required_masks"][y][x + width - 1]
                            if width > 1 else grid["required_masks"][y][x])
                    masks[state] = mask
                    if not mask:
                        candidates[state] = ()
                        continue
                    box = grid_bbox(demand.direction.axis, xs, ys, state)
                    compatible = [level for level in needed if all(not (mask & (1 << required.index))
                        or recipe_covers(required.recipe, level.recipe) for required in needed)]
                    options = []
                    for level in compatible:
                        quantum = math.lcm(*(spec.step for spec in level.recipe.additions))
                        span = ys[y + height] - ys[y]
                        if abs(span / quantum - round(span / quantum)) > 1e-6:
                            continue
                        try:
                            zone = build_composite_zone(demand, box, level.index, "FG-" + "-".join(map(str, (*state, level.index))),
                                                        placements[level.index], constraints=constraints, context=context)
                            if maximum_bar_length_mm is not None and any(c.installed_length_mm > maximum_bar_length_mm + 1e-6 for c in zone.components):
                                continue
                            check = check_composite_zone_coverage_geometry(demand, zone, constraints=constraints, context=context)
                            if not check.geometry_and_pattern_valid:
                                continue
                            # Require every mandatory component to service the whole tile rectangle.
                            # Original FE union coverage is independently checked after partitioning.
                            if any(b[0] > box[0] + GEOMETRY_TOLERANCE_MM or b[1] > box[1] + GEOMETRY_TOLERANCE_MM
                                   or b[2] < box[2] - GEOMETRY_TOLERANCE_MM or b[3] < box[3] - GEOMETRY_TOLERANCE_MM
                                   for b in check.component_service_bboxes_mm):
                                continue
                            # Full host checks are applied to selected layouts below. The common
                            # rectangle is only a necessary condition, not a bypass for holes/cutting.
                            options.append((zone, check.additional_mass_kg, check.physical_bar_count))
                        except ValueError:
                            continue
                    candidates[state] = tuple(options)
                    telemetry["candidate_count"] += len(options)
    telemetry["candidate_build_s"] = perf_counter() - started
    points, signatures = [], set()
    root = (0, nx, 0, ny)
    target_ids = set(partition["target_cell_ids"])
    for bar_penalty in bar_penalties:
        for zone_penalty in zone_penalties:
            costs, choices = {}, {}
            complete = True
            for state in states:
                if perf_counter() - started > time_limit_s:
                    complete = False
                    telemetry["timed_out"] = True
                    break
                if not masks[state]:
                    costs[state], choices[state] = 0.0, ()
                    continue
                options = candidates[state]
                best, decision = math.inf, None
                for option in options:
                    _, mass, bars = option
                    score = mass + zone_penalty + bar_penalty * bars
                    if score < best:
                        best, decision = score, (option[0],)
                a, b, c, d = state
                splits = [((a, k, c, d), (k, b, c, d)) for k in range(a + 1, b)]
                splits.extend(((a, b, c, k), (a, b, k, d)) for k in range(c + 1, d))
                for first, second in splits:
                    score = costs[first] + costs[second]
                    if score < best - 1e-8:
                        best, decision = score, (first, second)
                costs[state], choices[state] = best, decision
            if not complete:
                break
            telemetry["solver_executed"] = True
            record = {"zone_penalty_kg": zone_penalty, "bar_penalty_kg": bar_penalty, "accepted": False}
            telemetry["scalar_runs"].append(record)
            if not math.isfinite(costs[root]):
                record["rejection"] = "no_partition_in_this_grid"
                continue
            chosen, pending = [], [root]
            while pending:
                decision = choices[pending.pop()]
                if len(decision) == 1:
                    chosen.append(decision[0])
                elif len(decision) == 2:
                    pending.extend(decision)
            chosen = tuple(sorted(chosen, key=lambda z: z.id))
            mass = math.fsum(c.mass_kg for z in chosen for c in z.components)
            bars = sum(c.bar_count for z in chosen for c in z.components)
            record.update(zone_count=len(chosen), physical_bar_count=bars, mass_kg=mass)
            if (len(chosen) > maximum_zones or (maximum_physical_bars is not None and bars > maximum_physical_bars)
                    or (maximum_mass_kg is not None and mass > maximum_mass_kg + 1e-6)):
                record["rejection"] = "hard_budget_not_relaxed"
                continue
            check = evaluate_composite_coverage(demand, chosen, policy_id=problem.policy_id, constraints=constraints)
            host_check = evaluate_composite_host(demand, chosen, problem.host_envelope, constraints=constraints)
            if (not check.geometry_and_patterns_valid or any(not c.covered for c in check.cells if c.cell_id in target_ids)
                    or "fail" in host_check["checks"].values()):
                telemetry["rejected_proposals"] += 1
                record["rejection"] = "independent_coverage_or_host_failed"
                continue
            record["accepted"] = True
            signature = tuple(z.id for z in chosen)
            if signature not in signatures:
                signatures.add(signature)
                points.append(CompositeSearchPoint(chosen, check))
        if telemetry["timed_out"]:
            break
    # Two-dimensional mass/zones front, physical counts are separately constrained/reported.
    points.sort(key=lambda p: (len(p.zones), p.coverage.additional_mass_kg))
    front, best_mass = [], math.inf
    for point in points:
        if point.coverage.additional_mass_kg < best_mass - 1e-6:
            front.append(point)
            best_mass = point.coverage.additional_mass_kg
    selected = None
    if front:
        lo, hi = front[-1].coverage.additional_mass_kg, front[0].coverage.additional_mass_kg
        nlo, nhi = len(front[0].zones), len(front[-1].zones)
        selected = min(range(len(front)), key=lambda i: (
            ((front[i].coverage.additional_mass_kg - lo) / max(hi - lo, 1e-6)) ** 2
            + ((len(front[i].zones) - nlo) / max(nhi - nlo, 1)) ** 2, front[i].coverage.additional_mass_kg))
    telemetry.update(runtime_s=perf_counter() - started, original_uncovered_before_search=empty.uncovered_cell_count)
    return CompositeSearchResult(tuple(front), selected, telemetry)
