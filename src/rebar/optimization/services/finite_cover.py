"""Ограниченный MILP целых КЭ: предложения, а не инженерный сертификат."""
from __future__ import annotations

import math
from time import perf_counter


def solve_finite_cover_front(
    coverage: tuple[frozenset[int], ...], masses: tuple[float, ...], cell_count: int,
    *, maximum_zones: int, time_limit_s: float, budgets: tuple[int, ...],
    incompatible_pairs: tuple[tuple[int, int], ...] = (),
    bar_counts: tuple[int, ...] | None = None, maximum_physical_bars: int | None = None,
    maximum_mass_kg: float | None = None,
    position_keys: tuple[frozenset[tuple], ...] | None = None,
    maximum_positions: int | None = None,
    position_budgets: tuple[int, ...] = (),
) -> tuple[tuple[tuple[int, ...], ...], dict]:
    """Минимум массы при заданных лимитах зон; каждый КЭ покрывает один достаточный кандидат.

    Не суммирует компоненты/As. Прерыванием решателя не оправдывается дробный или
    неполный ответ. Оптимальность относится только к переданной конечной матрице.
    """
    if (len(coverage) != len(masses) or not isinstance(cell_count, int) or isinstance(cell_count, bool)
            or cell_count < 0 or not isinstance(maximum_zones, int) or isinstance(maximum_zones, bool)
            or maximum_zones < 1 or not math.isfinite(time_limit_s) or time_limit_s <= 0
            or any(not math.isfinite(m) or m <= 0 for m in masses)
            or any(not isinstance(b, int) or isinstance(b, bool) or not 1 <= b <= maximum_zones for b in budgets)
            or any(not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < cell_count
                   for ids in coverage for i in ids)):
        raise ValueError("невалидная конечная модель покрытия или бюджет")
    if (any(len(pair) != 2 or any(isinstance(i, bool) or not isinstance(i, int)
                                 or not 0 <= i < len(masses) for i in pair) or pair[0] == pair[1]
            for pair in incompatible_pairs)
            or (bar_counts is not None and (len(bar_counts) != len(masses) or any(
                isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in bar_counts)))
            or (maximum_physical_bars is not None and (bar_counts is None
                or isinstance(maximum_physical_bars, bool) or not isinstance(maximum_physical_bars, int)
                or maximum_physical_bars < 1))
            or (maximum_mass_kg is not None and (isinstance(maximum_mass_kg, bool)
                or not math.isfinite(maximum_mass_kg) or maximum_mass_kg <= 0))):
        raise ValueError("невалидные конфликты или лимиты массы/физических стержней")
    if (position_keys is not None and (len(position_keys) != len(masses)
            or any(not isinstance(keys, frozenset) or not keys for keys in position_keys))):
        raise ValueError("каждому кандидату нужно непустое множество позиций")
    if ((maximum_positions is not None or position_budgets) and position_keys is None
            or maximum_positions is not None and (isinstance(maximum_positions, bool)
                or not isinstance(maximum_positions, int) or maximum_positions < 1)
            or any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in position_budgets)):
        raise ValueError("невалидный лимит позиций")
    rows = [[] for _ in range(cell_count)]
    for j, cells in enumerate(coverage):
        for i in cells:
            rows[i].append(j)
    telemetry = {"scope": "finite_whole_cell_cover_not_engineering_optimality", "solves": [],
                 "uncoverable_cells": [i for i, row in enumerate(rows) if not row],
                 "incompatible_pair_count": len(incompatible_pairs),
                 "maximum_physical_bars": maximum_physical_bars, "maximum_mass_kg": maximum_mass_kg}
    if not cell_count:
        return ((),), telemetry
    if telemetry["uncoverable_cells"]:
        return (), telemetry
    try:
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import coo_matrix
    except ImportError as error:
        raise ValueError("составной поиск требует SciPy: pip install -e '.[solver]'") from error
    keys = sorted(set().union(*position_keys)) if position_keys else []
    key_indexes = {key: len(masses) + i for i, key in enumerate(keys)}
    nvars = len(masses) + len(keys)
    objective = np.array((*masses, *([0.0] * len(keys))))
    zone_row = np.array((*([1.0] * len(masses)), *([0.0] * len(keys))))
    position_row = 1 - zone_row
    patterns = sorted({tuple(row) for row in rows})
    coordinates = [(i, j) for i, row in enumerate(patterns) for j in row]
    matrix = coo_matrix((np.ones(len(coordinates)), tuple(zip(*coordinates))),
                        shape=(len(patterns), nvars)).tocsc()
    extra_constraints = []
    conflicts = None
    if incompatible_pairs:
        coords = [(i, j) for i, pair in enumerate(incompatible_pairs) for j in pair]
        conflicts = coo_matrix((np.ones(len(coords)), tuple(zip(*coords))),
                               shape=(len(incompatible_pairs), nvars)).tocsc()
        extra_constraints.append(LinearConstraint(conflicts, 0, 1))
    if maximum_physical_bars is not None:
        extra_constraints.append(LinearConstraint(np.array((*bar_counts, *([0] * len(keys)))), 0, maximum_physical_bars))
    if maximum_mass_kg is not None:
        extra_constraints.append(LinearConstraint(objective, 0, maximum_mass_kg))
    if position_keys:
        # x_zone <= y_position for EVERY component, and y <= sum(x_using_it).
        # A zone with two additions activates two types, not one technical run.
        rows_i, cols_j, coefficients, bounds = [], [], [], []
        for j, zone_keys in enumerate(position_keys):
            for key in sorted(zone_keys):
                i = len(bounds)
                rows_i.extend((i, i))
                cols_j.extend((j, key_indexes[key]))
                coefficients.extend((1, -1))
                bounds.append(0)
        for key in keys:
            i = len(bounds)
            rows_i.append(i)
            cols_j.append(key_indexes[key])
            coefficients.append(1)
            for j, zone_keys in enumerate(position_keys):
                if key in zone_keys:
                    rows_i.append(i)
                    cols_j.append(j)
                    coefficients.append(-1)
            bounds.append(0)
        activation = coo_matrix((coefficients, (rows_i, cols_j)), shape=(len(bounds), nvars)).tocsc()
        extra_constraints.append(LinearConstraint(activation, -np.inf, np.array(bounds)))
    started = perf_counter()
    proposals = []
    telemetry.update(solver="scipy.optimize.milp/HiGHS", coverage_rows=len(patterns))
    runs = tuple(dict.fromkeys(
        [(b, maximum_positions) for b in budgets]
        + [(maximum_zones, min(b, maximum_positions) if maximum_positions else b) for b in position_budgets]
    ))
    for budget, position_budget in runs:
        remaining = time_limit_s - (perf_counter() - started)
        if remaining <= 0:
            break
        cap = [LinearConstraint(position_row, 0, position_budget)] if position_budget is not None else []
        result = milp(objective, integrality=np.ones(nvars), bounds=Bounds(0, 1),
                      constraints=[LinearConstraint(matrix, 1, np.inf),
                                   LinearConstraint(zone_row, 0, budget), *cap, *extra_constraints],
                      options={"time_limit": remaining, "mip_rel_gap": 1e-7})
        record = {"maximum_zones": budget, "maximum_positions": position_budget,
                  "status": int(result.status), "accepted": False}
        telemetry["solves"].append(record)
        if result.x is None or np.shape(result.x) != (nvars,) or not np.all(np.isfinite(result.x)):
            continue
        rounded = np.rint(result.x)
        if (np.any(np.abs(result.x - rounded) > 1e-6) or np.any(rounded < 0) or np.any(rounded > 1)
                or np.any(matrix @ rounded < 1) or np.dot(zone_row, rounded) > budget
                or (conflicts is not None and np.any(conflicts @ rounded > 1))
                or (maximum_physical_bars is not None and np.dot(bar_counts, rounded[:len(masses)]) > maximum_physical_bars)
                or (maximum_mass_kg is not None and np.dot(objective, rounded) > maximum_mass_kg + 1e-6)):
            continue
        indexes = tuple(np.flatnonzero(rounded[:len(masses)]).tolist())
        actual_keys = set().union(*(position_keys[j] for j in indexes)) if position_keys else set()
        if position_keys and (any(rounded[key_indexes[key]] != (key in actual_keys) for key in keys)
                or position_budget is not None and len(actual_keys) > position_budget):
            continue
        record.update(accepted=True, mass_kg=math.fsum(masses[j] for j in indexes), zone_count=len(indexes))
        if position_keys is not None:
            record["position_count"] = len(actual_keys)
        if bar_counts is not None:
            record["physical_bar_count"] = sum(bar_counts[j] for j in indexes)
        if indexes not in proposals:
            proposals.append(indexes)
    telemetry["runtime_s"] = perf_counter() - started
    return tuple(proposals), telemetry
