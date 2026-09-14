"""Граф расстояний между параллельными наборами, не расчёт прочности стыков."""
from __future__ import annotations

from itertools import combinations
import math

from ..contracts.placement import CompositeLayoutZone
from .axis_patterns import pattern_coordinates
from .composite_host import TOLERANCE_MM


def component_distance_graph(zones: tuple[CompositeLayoutZone, ...], *, minimum_clear_spacing_mm: float) -> dict:
    """Геометрия уже проверенных наборов; минимум расстояния всех пар осей в XY.

    Минимум по отсортированным поперечным координатам вычисляется за O(n+m).
    Он точен для параллельных наборов с одинаковыми длинами стержней внутри набора.
    Сближение торцов проверяется той же консервативной моделью, что в host checker.
    """
    if (isinstance(minimum_clear_spacing_mm, bool) or not math.isfinite(minimum_clear_spacing_mm)
            or minimum_clear_spacing_mm < 0):
        raise ValueError("нужен конечный неотрицательный зазор")
    nodes = tuple((zone.id, part.component_index, part) for zone in zones for part in zone.components)
    if not nodes or len(nodes) > 256 or sum(n[2].bar_count for n in nodes) > 2000:
        raise ValueError("граф стыков требует 1..256 наборов и не более 2000 стержней")
    coordinates = [pattern_coordinates(n[2].placement, n[2].axis_window_mm) for n in nodes]
    intrinsic = [i for i, (_, _, part) in enumerate(nodes) if any(
        b - a < part.rebar.diameter + minimum_clear_spacing_mm - TOLERANCE_MM
        for a, b in zip(coordinates[i], coordinates[i][1:]))]
    edges = []
    for i, j in combinations(range(len(nodes)), 2):
        a, b = nodes[i][2], nodes[j][2]
        along = max(a.longitudinal_interval_mm[0] - b.longitudinal_interval_mm[1],
                    b.longitudinal_interval_mm[0] - a.longitudinal_interval_mm[1], 0)
        threshold = (a.rebar.diameter + b.rebar.diameter) / 2 + minimum_clear_spacing_mm - TOLERANCE_MM
        if along >= threshold:
            continue
        p = q = 0
        transverse = math.inf
        first, second = coordinates[i], coordinates[j]
        while p < len(first) and q < len(second):
            transverse = min(transverse, abs(first[p] - second[q]))
            if first[p] < second[q]:
                p += 1
            else:
                q += 1
        distance = math.hypot(along, transverse)
        if distance < threshold:
            edges.append((i, j, distance, threshold))
    return {"nodes": nodes, "edges": tuple(edges), "intrinsic_spacing_failures": tuple(intrinsic)}
