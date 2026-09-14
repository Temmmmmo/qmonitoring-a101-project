"""Проверка точного раскроя ФАКТИЧЕСКОЙ ведомости без изменения установленной арматуры.

Явная модель: пруток 11700 мм, нулевой пропил, смешанные прямые отрезки одного
диаметра/класса. Не проектирует стыки, не добавляет фиктивные стержни или «запас».
"""
from __future__ import annotations

import math
from time import perf_counter

from .bar_schedule import BarSchedulePosition, straight_bar_key


def check_stock_cutting(
    schedule: tuple[BarSchedulePosition, ...], *, time_limit_s: float = 5,
    maximum_patterns: int = 10000, maximum_nodes: int = 100000,
) -> dict:
    """Найти целочисленные схемы без остатка; лимит поиска означает not_checked."""
    if (isinstance(time_limit_s, bool) or not math.isfinite(time_limit_s) or not 0 < time_limit_s <= 60
            or any(isinstance(n, bool) or not isinstance(n, int) or n < 1
                   for n in (maximum_patterns, maximum_nodes))):
        raise ValueError("невалидный бюджет проверки раскроя")
    if len(schedule) > 512:
        raise ValueError("не более 512 позиций раскроя")
    grouped, marks, type_keys = {}, set(), set()
    for position in schedule:
        key = straight_bar_key(position.diameter_mm, position.length_mm, position.steel_class)
        if (position.shape != "straight" or not position.mark or position.mark in marks or key in type_keys
                or isinstance(position.physical_bar_count, bool)
                or not isinstance(position.physical_bar_count, int) or not 1 <= position.physical_bar_count <= 100000):
            raise ValueError("раскрой требует уникальную ведомость прямых стержней с целым количеством")
        marks.add(position.mark)
        type_keys.add(key)
        grouped.setdefault((key[1], key[2]), []).append(position)
    started = perf_counter()
    groups = []
    for (steel_class, diameter), members in sorted(grouped.items()):
        rows = sorted(members, key=lambda p: (-p.length_mm, p.mark))
        # Schedule lengths are already grouped at 1e-6 mm, not rounded to manufacturing lengths.
        lengths = tuple(round(p.length_mm * 1_000_000) for p in rows)
        quantities = tuple(p.physical_bar_count for p in rows)
        stock = 11_700_000_000
        total = sum(length * n for length, n in zip(lengths, quantities))
        record = {"steel_class": steel_class, "diameter_mm": diameter, "status": "not_checked",
                  "position_marks": [p.mark for p in rows], "physical_bar_count": sum(quantities),
                  "installed_length_mm": total / 1_000_000, "stock_bar_count": None,
                  "waste_mm": None, "patterns": []}
        groups.append(record)
        if not steel_class:
            record["reason"] = "steel_class_not_declared"
            continue
        if any(n <= 0 or n > stock for n in lengths):
            record.update(status="fail", reason="installed_bar_exceeds_stock_or_length_resolution")
            continue
        if total % stock:
            record.update(status="fail", reason="batch_total_not_multiple_of_stock",
                          minimum_possible_waste_mm=(stock - total % stock) / 1_000_000)
            continue
        patterns, nodes, truncated = [], 0, False

        def visit(index, remaining, partial):
            nonlocal nodes, truncated
            nodes += 1
            if (nodes > maximum_nodes or len(patterns) >= maximum_patterns
                    or perf_counter() - started >= time_limit_s):
                truncated = True
                return
            if remaining == 0:
                patterns.append((*partial, *([0] * (len(rows) - index))))
                return
            if index == len(rows):
                return
            if index == len(rows) - 1:
                count, remainder = divmod(remaining, lengths[index])
                if remainder == 0 and count <= quantities[index]:
                    patterns.append((*partial, count))
                return
            for count in range(min(quantities[index], remaining // lengths[index]), -1, -1):
                visit(index + 1, remaining - count * lengths[index], (*partial, count))
                if truncated:
                    return

        visit(0, stock, ())
        record.update(pattern_count=len(patterns), enumeration_nodes=nodes)
        if truncated:
            record["reason"] = "pattern_search_limit"
            continue
        if not patterns:
            record.update(status="fail", reason="no_exact_cut_pattern")
            continue
        try:
            import numpy as np
            from scipy.optimize import Bounds, LinearConstraint, milp
            from scipy.sparse import csc_matrix
        except ImportError:
            record["reason"] = "solver_unavailable"
            continue
        remaining = time_limit_s - (perf_counter() - started)
        if remaining <= 0:
            record["reason"] = "solver_time_limit"
            continue
        matrix = csc_matrix(np.array(patterns, dtype=float).T)
        upper = [min(n // count for n, count in zip(quantities, pattern) if count) for pattern in patterns]
        answer = milp(np.ones(len(patterns)), integrality=np.ones(len(patterns)),
                      bounds=Bounds(0, upper), constraints=LinearConstraint(matrix, quantities, quantities),
                      options={"time_limit": remaining, "mip_rel_gap": 0})
        record["solver_status"] = int(answer.status)
        if answer.x is None:
            record.update(status="fail" if answer.status == 2 else "not_checked",
                          reason="batch_pattern_counts_infeasible" if answer.status == 2 else "solver_no_solution")
            continue
        values = answer.x
        if (np.shape(values) != (len(patterns),) or not np.all(np.isfinite(values))
                or np.any(np.abs(values - np.rint(values)) > 1e-6)):
            record["reason"] = "solver_invalid_integer_solution"
            continue
        used = tuple(int(round(v)) for v in values)
        if (any(n < 0 or n > cap for n, cap in zip(used, upper))
                or any(sum(pattern[i] * n for pattern, n in zip(patterns, used)) != quantity
                       for i, quantity in enumerate(quantities))
                or sum(used) * stock != total):
            record["reason"] = "independent_count_check_failed"
            continue
        record.update(status="pass", reason="every_installed_bar_assigned_once", stock_bar_count=sum(used), waste_mm=0,
                      patterns=[{"stock_bar_count": n, "cuts": [{"mark": p.mark, "length_mm": p.length_mm,
                                  "pieces_per_stock_bar": count} for p, count in zip(rows, pattern) if count],
                                 "length_per_stock_bar_mm": 11700, "waste_per_stock_bar_mm": 0}
                                for pattern, n in zip(patterns, used) if n])
    statuses = {group["status"] for group in groups}
    status = "fail" if "fail" in statuses else "not_checked" if "not_checked" in statuses else "pass"
    return {"schema_version": "stock-cutting-check/v1", "status": status, "units": "mm",
            "policy_id": "11700-zero-kerf-mixed-straight-cuts/v1", "stock_length_mm": 11700,
            "kerf_mm": 0, "mixed_lengths_per_stock_allowed": True, "manufacturing_approval": "not_checked",
            "geometry_changed": False, "extra_uninstalled_pieces": 0, "groups": groups,
            "physical_bar_count": sum(p.physical_bar_count for p in schedule),
            "stock_bar_count": sum(g["stock_bar_count"] for g in groups) if status == "pass" else None,
            "waste_mm": 0 if status == "pass" else None,
            "warning": "Проверка заданной партии; нулевой пропил и смешанные отрезки — явные допущения модели. "
                       "Не подтверждает технологию стыков, геометрию host или разрешение Revit."}
