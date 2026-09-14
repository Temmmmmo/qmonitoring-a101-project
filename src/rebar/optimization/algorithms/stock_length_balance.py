"""Совместный выбор длин однородных наборов и точных схем раскроя партии.

Только удлинение, неизменные диаметры/количества. Геометрию host проверяет вызывающий
общий детализатор. Это конечный MILP, не разрешение на размещение или стыковку.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from time import perf_counter

from ..services.bar_schedule import BarScheduleGroup, build_bar_schedule
from ..services.cutting import PLATE_11700_CUT_LENGTHS_MM
from ..services.detailing import rebar_mass_kg
from ..services.stock_cutting import check_stock_cutting


@dataclass(frozen=True)
class StockLengthBalanceResult:
    status: str
    installed_lengths_mm: tuple[tuple[str, float], ...]
    original_mass_kg: float
    balanced_mass_kg: float | None
    telemetry: dict


def balance_stock_lengths(
    groups: tuple[BarScheduleGroup, ...], *, maximum_mass_increase_pct: float = 5,
    maximum_positions: int | None = None, time_limit_s: float = 10,
    maximum_patterns: int = 10000,
) -> StockLengthBalanceResult:
    """Минимум массы при точном расходе каждой заготовки и неизменном количестве.

    Один выбор длины на весь компонент зоны; технические Revit-ряды не разделяются.
    Ограничение прироста массы жёсткое: никакого fallback на удвоенную массу.
    """
    if (not 0 < len(groups) <= 512 or any(isinstance(v, bool) or not math.isfinite(v)
            for v in (maximum_mass_increase_pct, time_limit_s))
            or not 0 <= maximum_mass_increase_pct <= 100 or not 0 < time_limit_s <= 60
            or isinstance(maximum_patterns, bool) or not isinstance(maximum_patterns, int) or maximum_patterns < 1
            or maximum_positions is not None and (isinstance(maximum_positions, bool)
                or not isinstance(maximum_positions, int) or not 1 <= maximum_positions <= 512)):
        raise ValueError("невалидные размеры задачи или лимиты балансировки раскроя")
    original = build_bar_schedule(groups)
    mass_before = math.fsum(row.total_mass_kg for row in original)
    telemetry = {"policy_id": "11700-zero-kerf-mixed-straight-length-balance/v1", "solver_executed": False,
                 "maximum_mass_increase_pct": maximum_mass_increase_pct, "maximum_positions": maximum_positions,
                 "physical_bar_count": sum(g.physical_bar_count for g in groups), "geometry_checked": False}

    def reject(reason, *, status="not_checked"):
        return StockLengthBalanceResult(status, (), mass_before, None, {**telemetry, "reason": reason})

    if any(not group.steel_class.strip() for group in groups):
        return reject("steel_class_not_declared")
    if telemetry["physical_bar_count"] > 100000:
        raise ValueError("не более 100000 стержней в партии; количество не обрезано")
    started = perf_counter()
    variants, by_group, by_material = [], [], {}
    for i, group in enumerate(groups):
        ids = []
        material = (group.steel_class.strip(), group.diameter_mm)
        for length in PLATE_11700_CUT_LENGTHS_MM:
            if length >= group.installed_length_mm - 1e-6:
                ids.append(len(variants))
                variants.append((i, material, int(length), rebar_mass_kg(group.diameter_mm, length, group.physical_bar_count)))
                by_material.setdefault(material, set()).add(int(length))
        if not ids:
            return reject("installed_bar_exceeds_stock", status="infeasible")
        by_group.append(tuple(ids))
    patterns = []
    for material, length_set in sorted(by_material.items()):
        lengths = tuple(sorted(length_set, reverse=True))

        def enumerate_patterns(index, remaining, cuts):
            if len(patterns) >= maximum_patterns or perf_counter() - started > time_limit_s:
                raise TimeoutError
            if remaining == 0:
                patterns.append((material, tuple(cuts)))
                return
            if index == len(lengths):
                return
            length = lengths[index]
            for quantity in range(remaining // length, -1, -1):
                enumerate_patterns(index + 1, remaining - quantity * length,
                                   (*cuts, (length, quantity)) if quantity else cuts)
        try:
            enumerate_patterns(0, 11700, ())
        except TimeoutError:
            return reject("pattern_search_limit")
    telemetry.update(variant_count=len(variants), pattern_count=len(patterns))
    try:
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import coo_matrix
    except ImportError:
        return reject("solver_unavailable")
    position_keys = sorted({(material, length) for _, material, length, _ in variants})
    position_index = {key: len(variants) + len(patterns) + i for i, key in enumerate(position_keys)}
    variable_count = len(variants) + len(patterns) + len(position_keys)
    ri, cj, values, lower, upper = [], [], [], [], []

    def row(coefficients, lo, hi):
        index = len(lower)
        for j, value in coefficients:
            ri.append(index)
            cj.append(j)
            values.append(value)
        lower.append(lo)
        upper.append(hi)

    for ids in by_group:
        row(((j, 1) for j in ids), 1, 1)
    # Equality is per material AND length. No discarded cut, unassigned bar or spare.
    for material, length in position_keys:
        coefficients = [(j, groups[i].physical_bar_count) for j, (i, m, le, _) in enumerate(variants)
                        if m == material and le == length]
        coefficients.extend((len(variants) + j, -count) for j, (m, cuts) in enumerate(patterns)
                            if m == material for le, count in cuts if le == length)
        row(coefficients, 0, 0)
        users = [j for j, (_, m, le, _) in enumerate(variants) if (m, le) == (material, length)]
        activated = position_index[(material, length)]
        for j in users:
            row(((j, 1), (activated, -1)), -np.inf, 0)
        row(((activated, 1), *((j, -1) for j in users)), -np.inf, 0)
    if maximum_positions is not None:
        row(((j, 1) for j in position_index.values()), 0, maximum_positions)
    max_mass = mass_before * (1 + maximum_mass_increase_pct / 100)
    row(((j, variant[3]) for j, variant in enumerate(variants)), 0, max_mass)
    matrix = coo_matrix((values, (ri, cj)), shape=(len(lower), variable_count)).tocsc()
    objective = np.zeros(variable_count)
    objective[:len(variants)] = [variant[3] for variant in variants]
    bounds = np.ones(variable_count)
    bounds[len(variants):len(variants) + len(patterns)] = telemetry["physical_bar_count"]
    remaining = time_limit_s - (perf_counter() - started)
    if remaining <= 0:
        return reject("time_limit_before_solver")
    result = milp(objective, integrality=np.ones(variable_count), bounds=Bounds(0, bounds),
                  constraints=LinearConstraint(matrix, lower, upper),
                  options={"time_limit": remaining, "mip_rel_gap": 1e-7})
    telemetry.update(solver_executed=True, solver_status=int(result.status), runtime_s=perf_counter() - started,
                     optimal_within_length_variants=result.status == 0)
    if result.x is None:
        return reject("no_balanced_lengths_within_limits", status="infeasible" if result.status == 2 else "not_checked")
    if np.shape(result.x) != (variable_count,) or not np.all(np.isfinite(result.x)):
        return reject("invalid_solver_vector")
    rounded = np.rint(result.x)
    if (np.any(np.abs(result.x - rounded) > 1e-6) or np.any(rounded < 0) or np.any(rounded > bounds)
            or np.any(matrix @ rounded < np.array(lower) - 1e-6)
            or np.any(matrix @ rounded > np.array(upper) + 1e-6)):
        return reject("independent_solver_constraints_failed")
    chosen = [j for j in range(len(variants)) if rounded[j] == 1]
    if any(sum(variants[j][0] == i for j in chosen) != 1 for i in range(len(groups))):
        return reject("component_assignment_failed")
    lengths_by_group = {variants[j][0]: variants[j][2] for j in chosen}
    balanced_groups = tuple(BarScheduleGroup(g.source_id, g.diameter_mm, lengths_by_group[i],
                            g.physical_bar_count, g.steel_class) for i, g in enumerate(groups))
    schedule = build_bar_schedule(balanced_groups)
    balanced_mass = math.fsum(row.total_mass_kg for row in schedule)
    if (balanced_mass > max_mass + 1e-6 or balanced_mass < mass_before - 1e-6
            or maximum_positions is not None and len(schedule) > maximum_positions
            or any(lengths_by_group[i] < g.installed_length_mm - 1e-6 for i, g in enumerate(groups))):
        return reject("independent_mass_or_lengths_failed")
    cutting = check_stock_cutting(schedule, time_limit_s=max(0.1, time_limit_s - (perf_counter() - started)))
    telemetry["stock_cutting"] = cutting
    if cutting["status"] != "pass":
        return reject("independent_cutting_not_passed")
    return StockLengthBalanceResult("balanced", tuple((g.source_id, lengths_by_group[i]) for i, g in enumerate(groups)),
                                    mass_before, balanced_mass, telemetry)
