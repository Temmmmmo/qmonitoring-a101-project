"""Независимый bottom-up DP по нескольким деревьям соседних объединений.

Лист — исходный КЭ, узел — прямоугольник группы. В каждом узле сохраняются
альтернативы «объединить» и «оставить раздельно» для разных чисел зон. Поэтому
одно раннее жадное слияние не фиксирует всю дальнейшую траекторию. Полный спрос,
общая детализация и независимый checker неизменны. Это конечное семейство
иерархических разбиений; optional neighbor polishing расширяет этот конечный
набор. Глобальный оптимум всех прямоугольных покрытий не доказан.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from time import perf_counter

from ..contracts.composite_search import CompositeSearchPoint, CompositeSearchProblem, CompositeSearchResult
from ..contracts.composite_coverage import MONOTONE_COMPONENT_STO_COVERAGE_POLICY
from ..services.composite_coverage import (
    MAX_CELL_ZONE_PAIRS, MAX_ZONES, check_composite_zone_coverage_geometry,
    evaluate_composite_coverage, recipe_covers, monotone_component_recipe_covers,
)
from ..services.composite_detailing import build_composite_zone, prepare_composite_detailing, validate_recipe_placement
from ..services.composite_host import evaluate_composite_host, evaluate_host_demand_feasibility
from ..services.composite_host_fit import admissible_host_window, fit_composite_zone_to_host
from ..services.composite_windows import covering_composite_window
from ..services.zone_tradeoff import recommend_zone_knee


@dataclass(frozen=True)
class _Plan:
    mass: float
    count: int
    zone: object = None
    children: tuple = ()

    def zones(self):
        if self.zone is not None:
            return (self.zone,)
        return tuple(zone for child in self.children for zone in child.zones())


def _prune(options):
    result, best = {}, math.inf
    for count, plan in sorted(options.items()):
        if plan.mass < best - 1e-6:
            result[count], best = plan, plan.mass
    return result


def solve_composite_merge(
    problem: CompositeSearchProblem, *, maximum_zones: int = 128,
    time_limit_s: float = 20, maximum_bar_length_mm: float = 11700,
    maximum_points: int = 16, progress_callback=None,
    neighbor_polish: bool = False, polish_time_limit_s: float = 5,
    polish_max_evaluations: int = 320, polish_max_steps: int = 8,
) -> CompositeSearchResult:
    """Конечный фронт трёх иерархий X/Y/longest и опциональных соседних слияний.

    Время ограничивает генерацию деревьев, а не обязательный финальный checker.
    При прерывании ветви она может дать только целый проверяемый прямоугольник:
    частичные наборы КЭ не превращаются в полное решение.
    """
    if any(isinstance(n, bool) or not isinstance(n, int) or not low <= n <= high
           for n, low, high in ((maximum_zones, 1, 128), (maximum_points, 3, 64),
                               (polish_max_evaluations, 1, 10000), (polish_max_steps, 1, 128))):
        raise ValueError("лимит зон должен быть 1..128, точек 3..64")
    if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0
           for v in (time_limit_s, maximum_bar_length_mm, polish_time_limit_s)) or time_limit_s > 300 or polish_time_limit_s > 60:
        raise ValueError("нужны положительные лимиты длины и времени (не более 300 секунд)")
    if not isinstance(neighbor_polish, bool):
        raise ValueError("neighbor_polish должен быть bool")
    if problem.boundary_mode != "strict" or problem.constraints.allow_overlaps is not True:
        raise ValueError("новый поиск сохраняет весь спрос и требует явный профиль пересечений зон")
    demand, constraints = problem.demand, problem.constraints
    empty = evaluate_composite_coverage(demand, (), policy_id=problem.policy_id, constraints=constraints)
    levels = tuple(level for level in demand.levels if level.requires_extra)
    placements = dict(problem.placements)
    if len(placements) != len(problem.placements) or set(placements) != {level.index for level in levels}:
        raise ValueError("нужны явные фазы всех дополнительных уровней без повторов")
    for level in levels:
        validate_recipe_placement(level.recipe, placements[level.index])
    cells = tuple(sorted((c for c in demand.cells if demand.level(c.level_index).requires_extra), key=lambda c: c.id))
    telemetry = {"algorithm": "composite-bottom-up-partitions/v1", "complexity_axis": "zone_count",
                 "scope": ("three_spatial_merge_hierarchies_plus_bounded_neighbor_unions_not_global_optimum"
                           if neighbor_polish else "three_spatial_merge_hierarchies_not_global_optimum"),
                 "source_demand_preserved": True,
                 "engineering_optimality_proven": False, "maximum_zones": maximum_zones,
                 "initial_cell_count": len(cells), "candidate_count": 0, "host_rejected_candidates": 0,
                 "rejected_proposals": 0, "timed_out": False, "trajectories": []}
    if not cells:
        return CompositeSearchResult((CompositeSearchPoint((), empty),), 0, telemetry)
    if problem.host_envelope is not None:
        feasibility = evaluate_host_demand_feasibility(demand, problem.host_envelope, constraints=constraints)
        telemetry["host_demand_feasibility"] = feasibility
        if feasibility["requires_engineering_decision"]:
            return CompositeSearchResult((), None, telemetry)
    started = perf_counter()
    context = prepare_composite_detailing(demand)
    boxes = {c.id: (min(x for x, _ in c.poly), min(y for _, y in c.poly),
                   max(x for x, _ in c.poly), max(y for _, y in c.poly)) for c in cells}
    admissible = {level.index: admissible_host_window(demand, level.index, problem.host_envelope, constraints)
                  if problem.host_envelope else None for level in levels}
    cache = {}
    covers = (monotone_component_recipe_covers if problem.policy_id == MONOTONE_COMPONENT_STO_COVERAGE_POLICY
              else recipe_covers)
    limit = min(maximum_zones, MAX_ZONES, MAX_CELL_ZONE_PAIRS // len(demand.cells))

    def proposal(box, required):
        key = box, required
        if key in cache:
            return cache[key]
        best = None
        for level in levels:
            if not all(covers(demand.level(i).recipe, level.recipe) for i in required):
                continue
            try:
                window = covering_composite_window(demand, box, level.index, placements[level.index],
                    constraints=constraints, admissible_bbox=admissible[level.index], context=context)
                zone = build_composite_zone(demand, window, level.index, f"MP-{len(cache)}-{level.index}",
                    placements[level.index], constraints=constraints, context=context)
                if any(c.installed_length_mm > maximum_bar_length_mm + 1e-6 for c in zone.components):
                    continue
                if problem.host_envelope is not None:
                    zone = fit_composite_zone_to_host(demand, zone, problem.host_envelope, constraints=constraints)
                    host = evaluate_composite_host(demand, (zone,), problem.host_envelope, constraints=constraints)
                    if "fail" in host["checks"].values():
                        telemetry["host_rejected_candidates"] += 1
                        continue
                check = check_composite_zone_coverage_geometry(demand, zone, constraints=constraints,
                    policy_id=problem.policy_id, context=context)
                if not check.geometry_and_pattern_valid or any(
                    b[0] > box[0] + 1e-6 or b[1] > box[1] + 1e-6
                    or b[2] < box[2] - 1e-6 or b[3] < box[3] - 1e-6 for b in check.component_service_bboxes_mm):
                    continue
                telemetry["candidate_count"] += 1
                if best is None or check.additional_mass_kg < best.mass:
                    best = _Plan(check.additional_mass_kg, 1, zone)
            except ValueError:
                telemetry["rejected_proposals"] += 1
        cache[key] = best
        return best

    def tree(group, mode, depth, deadline):
        bounds = [boxes[c.id] for c in group]
        box = (min(b[0] for b in bounds), min(b[1] for b in bounds),
               max(b[2] for b in bounds), max(b[3] for b in bounds))
        required = tuple(sorted({c.level_index for c in group}))
        joint = proposal(box, required)
        options = {1: joint} if joint else {}
        if len(group) == 1:
            return options
        if perf_counter() >= deadline:
            telemetry["timed_out"] = True
            return options
        # Spatial siblings are neighbours in a guillotine partition; labels do not
        # define borders. Alternating axes explore different possible local unions.
        axis = ((mode + depth) % 2 if mode < 2 else int(box[3] - box[1] > box[2] - box[0]))
        ordered = sorted(group, key=lambda c: (c.centroid[axis], c.centroid[1 - axis], c.id))
        # Equal centroids occur in duplicated/overlapping elements. Splitting by
        # position, with a stable ID tie-break, still permits independent leaves.
        middle = len(ordered) // 2
        first, second = tuple(ordered[:middle]), tuple(ordered[middle:])
        left, right = tree(first, mode, depth + 1, deadline), tree(second, mode, depth + 1, deadline)
        for n, a in left.items():
            for k, b in right.items():
                count, mass = n + k, a.mass + b.mass
                if count <= limit and (count not in options or mass < options[count].mass - 1e-6):
                    options[count] = _Plan(mass, count, children=(a, b))
        return _prune(options)

    archive = {}
    for mode in range(3):
        # Reserve time for distinct hierarchies instead of spending it on one path.
        deadline = started + time_limit_s * (mode + 1) / 3
        options = tree(cells, mode, 0, deadline)
        for count, plan in options.items():
            if count not in archive or plan.mass < archive[count].mass:
                archive[count] = plan
        telemetry["trajectories"].append({"mode": mode, "front": [
            {"zone_count": n, "mass_kg": p.mass} for n, p in sorted(options.items())]})
        if progress_callback:
            progress_callback(mode + 1, 3)
    front = list(_prune(archive).values())
    telemetry["generated_front_count"] = len(front)
    if len(front) > maximum_points:
        knee = recommend_zone_knee([(p.count, p.mass) for p in front])["index"]
        indexes = {round(i * (len(front) - 1) / (maximum_points - 2)) for i in range(maximum_points - 1)}
        indexes.add(knee)
        front = [front[i] for i in sorted(indexes)]
    points = []
    for plan in front:
        # A cached rectangle may be reused in two different branches. IDs belong
        # to a final solution, not to a memoized proposal, and must be unique.
        zones = tuple(replace(zone, id=f"MP-{index}") for index, zone in enumerate(plan.zones()))
        check = evaluate_composite_coverage(demand, zones, policy_id=problem.policy_id, constraints=constraints)
        if check.status != "pass":
            telemetry["rejected_proposals"] += 1
            continue
        if problem.host_envelope is not None and "fail" in evaluate_composite_host(
                demand, zones, problem.host_envelope, constraints=constraints)["checks"].values():
            telemetry["host_rejected_candidates"] += 1
            continue
        points.append(CompositeSearchPoint(zones, check))
    if neighbor_polish and points:
        # A local improvement crosses the fixed median-tree boundaries. Keep the
        # original front available as a baseline, and publish only solutions
        # that pass the independent full-demand checker below.
        from shapely.geometry import box as shapely_box
        from shapely.strtree import STRtree

        polish_started = perf_counter()
        polish_deadline = polish_started + polish_time_limit_s
        telemetry["neighbor_polish"] = {"enabled": True, "baseline_front": [
            {"zone_count": len(point.zones), "mass_kg": point.coverage.additional_mass_kg} for point in points],
            "evaluated_pairs": 0, "accepted_merges": 0, "candidate_trajectory": [],
            "time_limit_s": polish_time_limit_s, "max_evaluations": polish_max_evaluations,
            "max_steps_per_seed": polish_max_steps}
        stats = telemetry["neighbor_polish"]
        pair_cache = {}
        stats["cache_hits"] = 0
        knee_seed = recommend_zone_knee([(len(p.zones), p.coverage.additional_mass_kg)
                                        for p in points])["index"]
        seed_indexes = tuple(dict.fromkeys((len(points) - 1, knee_seed, len(points) // 2, 0)))
        polished = []
        for seed_index in seed_indexes:
            seed = points[seed_index]
            current = [(zone, check.additional_mass_kg)
                       for zone, check in zip(seed.zones, seed.coverage.zones)]
            for step in range(polish_max_steps):
                if perf_counter() >= polish_deadline:
                    break
                geometries = [shapely_box(*zone.demand_bbox) for zone, _ in current]
                tree_index = STRtree(geometries)
                best = None
                for i, (first, first_mass) in enumerate(current):
                    for j in sorted({int(index) for index in tree_index.query(geometries[i].buffer(1)) if int(index) > i}):
                        if perf_counter() >= polish_deadline:
                            break
                        second, second_mass = current[j]
                        pair_key = tuple(sorted(((first.demand_bbox, first.level_index),
                                                 (second.demand_bbox, second.level_index))))
                        if pair_key in pair_cache:
                            joint = pair_cache[pair_key]
                            stats["cache_hits"] += 1
                        else:
                            if stats["evaluated_pairs"] >= polish_max_evaluations:
                                continue
                            stats["evaluated_pairs"] += 1
                            a, b = first.demand_bbox, second.demand_bbox
                            union = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                            joint = proposal(union, tuple(sorted({first.level_index, second.level_index})))
                            pair_cache[pair_key] = joint
                        if joint is None:
                            continue
                        gain = first_mass + second_mass - joint.mass
                        if gain > 1e-6 and (best is None or gain > best[0] + 1e-6):
                            best = (gain, i, j, joint)
                    if perf_counter() >= polish_deadline:
                        break
                if best is None:
                    break
                gain, i, j, joint = best
                joined_zone = replace(joint.zone, id=f"MP-P-{seed_index}-{step}")
                current = [pair for index, pair in enumerate(current) if index not in (i, j)]
                current.append((joined_zone, joint.mass))
                stats["accepted_merges"] += 1
                stats["candidate_trajectory"].append({"seed_index": seed_index, "step": step + 1,
                    "zone_count": len(current), "estimated_mass_kg": math.fsum(mass for _, mass in current),
                    "saved_mass_kg": gain, "evaluated_pairs_at_accept": stats["evaluated_pairs"]})
                polished.append(tuple(zone for zone, _ in current))
        stats["elapsed_generation_s"] = perf_counter() - polish_started
        stats["timed_out"] = perf_counter() >= polish_deadline
        valid = []
        for zones in polished:
            check = evaluate_composite_coverage(demand, zones, policy_id=problem.policy_id, constraints=constraints)
            if check.status != "pass":
                continue
            if problem.host_envelope is not None and "fail" in evaluate_composite_host(
                    demand, zones, problem.host_envelope, constraints=constraints)["checks"].values():
                continue
            valid.append(CompositeSearchPoint(zones, check))
        stats["validated_trajectory_count"] = len(valid)
        candidates = {}
        for point in (*points, *valid):
            count = len(point.zones)
            if count not in candidates or point.coverage.additional_mass_kg < candidates[count].coverage.additional_mass_kg - 1e-6:
                candidates[count] = point
        best_mass = math.inf
        points = []
        for _, point in sorted(candidates.items()):
            if point.coverage.additional_mass_kg < best_mass - 1e-6:
                points.append(point)
                best_mass = point.coverage.additional_mass_kg
        stats["polished_front"] = [{"zone_count": len(point.zones),
            "mass_kg": point.coverage.additional_mass_kg} for point in points]
        if len(points) > maximum_points:
            knee_index = recommend_zone_knee([(len(p.zones), p.coverage.additional_mass_kg)
                                              for p in points])["index"]
            indexes = {round(i * (len(points) - 1) / (maximum_points - 2)) for i in range(maximum_points - 1)}
            indexes.add(knee_index)
            points = [points[i] for i in sorted(indexes)]
    knee = recommend_zone_knee([(len(p.zones), p.coverage.additional_mass_kg) for p in points])
    telemetry.update(runtime_s=perf_counter() - started, selection=knee, retained_front_count=len(points))
    return CompositeSearchResult(tuple(points), knee["index"], telemetry)
