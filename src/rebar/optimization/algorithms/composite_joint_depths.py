"""Конечный поиск глубин наборов; инженерные фазы, стыки и рабочая высота не утверждаются."""
from __future__ import annotations

from dataclasses import replace
import math
from time import perf_counter

from rebar.models import Layer

from ..contracts.composite_search import CompositeSearchPoint, CompositeSearchResult
from ..services.composite_coverage import evaluate_composite_coverage
from ..services.composite_detailing import build_composite_zone
from ..services.composite_host import evaluate_composite_host, TOLERANCE_MM
from ..services.composite_joint_graph import component_distance_graph


def solve_composite_joint_depths(
    demand, zones, host, *, policy_id, constraints, candidate_depths_by_component: tuple[tuple[float, ...], ...],
    hypothesis_source: str, preserve_component_order: bool = False, time_limit_s: float = 10,
    maximum_search_nodes: int = 100000,
) -> CompositeSearchResult:
    """Меняет только неизвестные глубины в КОПИИ, не выдаёт разрешение на размещение.

    Минимизирует максимальную глубину среди переданных конечных вариантов. Отдельный
    CSP с forward checking учитывает ВСЕ пары компонентов, включая разные добавки.
    Уже известные высоты не двигаются. Порядок добавок по высоте включается только явно.
    Полный независимый coverage/host повторяется после поиска без доверия графу.
    """
    if not isinstance(hypothesis_source, str) or not 1 <= len(hypothesis_source.strip()) <= 1000:
        raise ValueError("нужен явный источник гипотезы глубин")
    if not isinstance(preserve_component_order, bool):
        raise ValueError("preserve_component_order должен быть bool")
    if (isinstance(time_limit_s, bool) or not math.isfinite(time_limit_s) or not 0 < time_limit_s <= 60
            or isinstance(maximum_search_nodes, bool) or not isinstance(maximum_search_nodes, int)
            or not 1 <= maximum_search_nodes <= 1000000):
        raise ValueError("невалидный лимит поиска стыков")
    if (not isinstance(candidate_depths_by_component, tuple) or not candidate_depths_by_component
            or len(candidate_depths_by_component) > 8):
        raise ValueError("нужны варианты глубин для каждого типа добавки")
    for depths in candidate_depths_by_component:
        if (not isinstance(depths, tuple) or not 1 <= len(depths) <= 16
                or any(isinstance(d, bool) or not isinstance(d, (int, float)) or not math.isfinite(d) or d <= 0 for d in depths)
                or tuple(sorted(set(depths))) != depths):
            raise ValueError("глубины должны быть 1..16 конечными положительными возрастающими числами")
    started = perf_counter()
    baseline = evaluate_composite_coverage(demand, zones, policy_id=policy_id, constraints=constraints)
    if not baseline.geometry_and_patterns_valid:
        raise ValueError("сначала исправьте геометрию/схемы входных зон")
    graph = component_distance_graph(zones, minimum_clear_spacing_mm=constraints.minimum_clear_spacing_mm)
    nodes, edges = graph["nodes"], graph["edges"]
    if len(candidate_depths_by_component) != max(n[1] for n in nodes) + 1:
        raise ValueError("варианты глубин должны точно соответствовать индексам добавок")
    info = {"algorithm": "composite-joint-finite-depth-csp/v1", "hypothesis_source": hypothesis_source,
            "depths_engineering_approval": "not_checked", "solver_executed": False, "timed_out": False,
            "minimum_clear_spacing_mm": constraints.minimum_clear_spacing_mm,
            "candidate_depths_by_component_mm": candidate_depths_by_component,
            "preserve_component_order": preserve_component_order, "component_count": len(nodes),
            "conflicting_component_pair_count": len(edges), "search_nodes": 0, "cap_trials": [],
            "optimality_scope": "minimum_maximum_axis_depth_in_given_finite_domains_only",
            "engineering_optimality_proven": False, "placement_eligible": False,
            "remaining_checks": ["effective-depth-and-strength", "splice-design-and-staggering", "background-and-other-directions",
                                 "edge-completion", "phase-approval", "live-revit-host-and-readback"]}

    def finish(status, points=()):
        info.update(status=status, runtime_s=perf_counter() - started)
        return CompositeSearchResult(points, 0 if points else None, info)

    if graph["intrinsic_spacing_failures"]:
        info["intrinsic_spacing_failures"] = graph["intrinsic_spacing_failures"]
        return finish("intrinsic_set_spacing_cannot_be_fixed_by_depth")
    before_host = evaluate_composite_host(demand, zones, host, constraints=constraints)
    if any(before_host["checks"][k] == "fail" for k in ("planar_host_and_openings", "same_direction_zone_overlap", "top_bottom_cover")):
        return finish("non_depth_host_failure")
    cover = host.top_cover_mm if demand.direction.layer is Layer.TOP else host.bottom_cover_mm
    opposite_cover = host.bottom_cover_mm if demand.direction.layer is Layer.TOP else host.top_cover_mm
    thickness = host.top_z_mm - host.bottom_z_mm
    values = []
    for _, index, part in nodes:
        known = part.placement.axis_depth_from_face_mm
        available = tuple(d for d in candidate_depths_by_component[index]
            if cover + part.rebar.diameter / 2 - TOLERANCE_MM <= d <= thickness - opposite_cover - part.rebar.diameter / 2 + TOLERANCE_MM
            and (known is None or d == known))
        if not available:
            return finish("no_candidate_depth_respects_cover_or_fixed_depth")
        values.append(available)
    pair_requirements = {(i, j): (distance, threshold) for i, j, distance, threshold in edges}
    order_pairs = set()
    if preserve_component_order:
        for i, (zone_id, index, _) in enumerate(nodes):
            for j in range(i + 1, len(nodes)):
                if nodes[j][0] == zone_id and nodes[j][1] > index:
                    order_pairs.add((i, j))
                    pair_requirements.setdefault((i, j), (0, 0))
    adjacency = [[] for _ in nodes]
    support = {}
    for (i, j), (distance, threshold) in pair_requirements.items():
        adjacency[i].append(j)
        adjacency[j].append(i)
        allowed = [[math.hypot(distance, a - b) >= threshold and ((i, j) not in order_pairs or a < b)
                    for b in values[j]] for a in values[i]]
        support[(i, j)] = tuple(sum(1 << k for k, ok in enumerate(row) if ok) for row in allowed)
        support[(j, i)] = tuple(sum(1 << k for k in range(len(values[i])) if allowed[k][column])
                                for column in range(len(values[j])))

    class BudgetExhausted(Exception):
        pass

    def search(domains):
        info["search_nodes"] += 1
        if info["search_nodes"] > maximum_search_nodes or perf_counter() - started > time_limit_s:
            raise BudgetExhausted
        # Propagate singletons, including initially fixed known depths.
        pending = [i for i, mask in enumerate(domains) if mask.bit_count() == 1]
        while pending:
            i = pending.pop()
            selected = domains[i].bit_length() - 1
            for j in adjacency[i]:
                narrowed = domains[j] & support[(i, j)][selected]
                if not narrowed:
                    return None
                if narrowed != domains[j]:
                    domains[j] = narrowed
                    if narrowed.bit_count() == 1:
                        pending.append(j)
        free = [i for i, mask in enumerate(domains) if mask.bit_count() > 1]
        if not free:
            return domains
        i = min(free, key=lambda j: (domains[j].bit_count(), -len(adjacency[j]), j))
        remaining = domains[i]
        while remaining:
            bit = remaining & -remaining
            remaining ^= bit
            branch = domains.copy()
            branch[i] = bit
            found = search(branch)
            if found is not None:
                return found
        return None

    caps = sorted({d for ds in values for d in ds})
    best = None
    try:
        for cap in caps:
            domains = [sum(1 << k for k, d in enumerate(ds) if d <= cap) for ds in values]
            if not all(domains):
                continue
            info["solver_executed"] = True
            assignment = search(domains)
            info["cap_trials"].append({"maximum_axis_depth_mm": cap, "feasible": assignment is not None})
            if assignment is not None:
                best = tuple(ds[mask.bit_length() - 1] for ds, mask in zip(values, assignment))
                break
    except BudgetExhausted:
        info["timed_out"] = True
        return finish("search_budget_exhausted_not_infeasible")
    if best is None:
        return finish("no_assignment_in_given_depths")
    by_key = {(zone_id, index): depth for (zone_id, index, _), depth in zip(nodes, best)}
    assigned = tuple(build_composite_zone(demand, z.demand_bbox, z.level_index, z.id,
        replace(z.placement, additions=tuple(replace(a, axis_depth_from_face_mm=by_key[(z.id, i)])
                                              for i, a in enumerate(z.placement.additions))), constraints=constraints) for z in zones)
    coverage = evaluate_composite_coverage(demand, assigned, policy_id=policy_id, constraints=constraints)
    check = evaluate_composite_host(demand, assigned, host, constraints=constraints)
    if coverage != baseline or any(check["checks"][k] != "pass" for k in (
            "planar_host_and_openings", "top_bottom_cover", "same_direction_zone_overlap", "additional_bar_collisions")):
        return finish("independent_revalidation_failed")
    info.update(maximum_axis_depth_mm=max(best), minimum_axis_depth_mm=min(best),
        distinct_axis_depths_mm=sorted(set(best)),
        nominal_effective_depth_mm={"minimum": thickness - max(best), "maximum": thickness - min(best)},
        depth_assignment=[{"zone_id": zone_id, "component_index": index, "axis_depth_from_face_mm": depth}
                          for (zone_id, index, _), depth in zip(nodes, best)],
        host_preflight=check, unchanged_coverage_and_mass=True)
    return finish("geometric_depth_assignment_found_not_engineering_approval", (CompositeSearchPoint(assigned, coverage),))
