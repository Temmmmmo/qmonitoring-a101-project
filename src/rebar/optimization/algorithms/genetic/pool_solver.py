"""Ограниченная целочисленная интенсификация GA внутри конечного CandidateSet.

MILP выдаёт предложения, а не инженерные решения. Фазы, коллизии и нормативные
ограничения окончательно проверяются общим materialize/evaluate; родители сохраняются.
SciPy — явная optional-зависимость профиля, базовый GA её не импортирует.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from time import perf_counter
from typing import TYPE_CHECKING

from ...contracts import ComplexityAxis
from ...services.bar_schedule import straight_bar_key

if TYPE_CHECKING:
    from ..genetic_pareto import _SearchSpace


@dataclass(frozen=True)
class PoolPolishResult:
    genomes: tuple[frozenset[int], ...]
    telemetry: dict[str, object]
    stopped_by_time_limit: bool


def polish_candidate_pool(
    space: _SearchSpace,
    *,
    complexity_axis: ComplexityAxis,
    maximum_zones: int,
    maximum_solves: int = 6,
    time_limit_s: float = 10.0,
    deadline: float | None = None,
    budget_order: str = "complexity-first",
) -> PoolPolishResult:
    """Два края и epsilon-ограничения сложности; общий лимит времени всех MILP.

    Лимит/нет integer incumbent — нормальное завершение без предложения. Solver status
    относится только к конечной модели покрытия атомов, не к произвольным раскладкам.
    Не суммирует слабое As: каждый атом требует хотя бы одного достаточного кандидата.
    """
    if not isinstance(complexity_axis, ComplexityAxis):
        raise ValueError("неподдерживаемая ось pool polish")
    if maximum_zones < 1 or maximum_solves < 3:
        raise ValueError("pool polish требует maximum_zones >= 1 и maximum_solves >= 3")
    if not math.isfinite(time_limit_s) or time_limit_s <= 0:
        raise ValueError("pool polish time_limit_s должен быть конечным положительным")
    if budget_order not in {"complexity-first", "mass-first"}:
        raise ValueError("pool polish budget_order: complexity-first или mass-first")
    started = perf_counter()
    effective_deadline = min(started + time_limit_s, deadline) if deadline is not None else started + time_limit_s
    telemetry: dict[str, object] = {
        "scope": "finite_atom_cover_model_not_engineering_optimality",
        "maximum_solves": maximum_solves, "time_limit_s": time_limit_s, "solves": [],
        "budget_order": budget_order,
    }
    if effective_deadline <= started or not space.candidates:
        return PoolPolishResult((), telemetry, effective_deadline <= started)
    try:
        import numpy as np
        import scipy
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import coo_matrix
    except ImportError as error:
        raise ValueError("pool_polish='milp' требует SciPy: pip install -e '.[solver]'") from error

    telemetry["solver"] = "scipy.optimize.milp/HiGHS"
    telemetry["scipy_version"] = scipy.__version__
    rows: list[list[int]] = [[] for _ in range(space.leaf_count)]
    for index, candidate in enumerate(space.candidates):
        if not math.isfinite(candidate.mass_kg) or candidate.mass_kg <= 0:
            raise ValueError("pool polish: стоимость кандидата должна быть положительной и конечной")
        for leaf_id in candidate.leaf_ids:
            if not 0 <= leaf_id < space.leaf_count:
                raise ValueError("pool polish: индекс атома вне карты спроса")
            rows[leaf_id].append(index)
    patterns = tuple(sorted({tuple(row) for row in rows}))
    if () in patterns:
        telemetry["uncoverable_atoms"] = sum(not row for row in rows)
        return PoolPolishResult((), telemetry, False)
    row_ids, column_ids = [], []
    for row, indexes in enumerate(patterns):
        for column in indexes:
            row_ids.append(row)
            column_ids.append(column)
    count = len(space.candidates)
    position_groups: dict[tuple, list[int]] = {}
    if complexity_axis is ComplexityAxis.POSITION_COUNT:
        for index, candidate in enumerate(space.candidates):
            zone = candidate.rectangle.zone
            key = straight_bar_key(zone.rebar.diameter, zone.installed_length_mm)
            position_groups.setdefault(key, []).append(index)
    variable_count = count + len(position_groups)
    matrix = coo_matrix((np.ones(len(row_ids)), (row_ids, column_ids)),
                        shape=(len(patterns), variable_count)).tocsc()
    mass = np.array([c.mass_kg for c in space.candidates] + [0.0] * len(position_groups))
    if complexity_axis is ComplexityAxis.POSITION_COUNT:
        complexity = np.array([0.0] * count + [1.0] * len(position_groups))
    else:
        complexity = np.array([1 if complexity_axis is ComplexityAxis.ZONE_COUNT
                               else c.rectangle.zone.bar_count for c in space.candidates], dtype=float)
    constraints = [LinearConstraint(matrix, 1, np.inf),
                   LinearConstraint(np.array([1.0] * count + [0.0] * len(position_groups)),
                                    0, maximum_zones)]
    # y_k == OR(x_i): общая позиция оплачивается один раз, даже в нескольких зонах.
    link_rows, link_columns, link_values = [], [], []
    link_count = 0
    for offset, indexes in enumerate(position_groups.values()):
        position_column = count + offset
        for index in indexes:
            link_rows.extend((link_count, link_count))
            link_columns.extend((index, position_column))
            link_values.extend((1.0, -1.0))
            link_count += 1
        link_rows.extend([link_count] * (len(indexes) + 1))
        link_columns.extend([position_column, *indexes])
        link_values.extend([1.0, *([-1.0] * len(indexes))])
        link_count += 1
    if link_count:
        links = coo_matrix((link_values, (link_rows, link_columns)),
                           shape=(link_count, variable_count)).tocsc()
        constraints.append(LinearConstraint(links, -np.inf, 0))
    telemetry.update(candidate_count=count, atom_count=space.leaf_count, coverage_rows=len(patterns))
    records = []
    genomes: list[frozenset[int]] = []
    timed_out = False

    def solve(objective, name, budget=None):
        nonlocal timed_out
        remaining = effective_deadline - perf_counter()
        if remaining <= 0:
            timed_out = True
            return None
        bounds = [] if budget is None else [LinearConstraint(complexity, 0, budget)]
        solve_started = perf_counter()
        result = milp(objective, integrality=np.ones(variable_count), bounds=Bounds(0, 1),
                      constraints=[*constraints, *bounds],
                      options={"time_limit": remaining, "mip_rel_gap": 1e-7})

        def finite_number(value):
            return float(value) if value is not None and math.isfinite(value) else None

        record = {
            "objective": name, "complexity_budget": budget, "status": int(result.status),
            "objective_value": finite_number(result.fun),
            "dual_bound": finite_number(getattr(result, "mip_dual_bound", None)),
            "relative_gap": finite_number(getattr(result, "mip_gap", None)),
            "nodes": finite_number(getattr(result, "mip_node_count", None)),
            "accepted_integer_proposal": False,
            "time_limit_s": remaining, "runtime_ms": (perf_counter() - solve_started) * 1000,
        }
        records.append(record)
        timed_out = timed_out or result.status == 1
        if result.x is None or not np.all(np.isfinite(result.x)):
            return None
        rounded = np.rint(result.x)
        genome = frozenset(np.flatnonzero(rounded[:count]).tolist())
        if position_groups:
            active_positions = np.array([
                float(any(index in genome for index in indexes))
                for indexes in position_groups.values()
            ])
            if not np.array_equal(rounded[count:], active_positions):
                return None
            actual_complexity = int(sum(active_positions))
        else:
            actual_complexity = sum(int(complexity[index]) for index in genome)
        if (np.any(np.abs(result.x - rounded) > 1e-6) or np.any(rounded < 0)
                or np.any(rounded > 1) or np.any(matrix @ rounded < 1)
                or len(genome) > maximum_zones
                or (budget is not None and actual_complexity > budget)):
            return None
        if genome not in genomes:
            genomes.append(genome)
        record.update(accepted_integer_proposal=True, mass_kg=math.fsum(float(mass[i]) for i in genome),
                      complexity=actual_complexity, zone_count=len(genome))
        return actual_complexity

    mass_edge = solve(mass, "mass_kg")
    complexity_edge = solve(complexity, complexity_axis.value)
    if complexity_edge is not None:
        solve(mass, "mass_kg", complexity_edge)
    if mass_edge is not None and complexity_edge is not None and mass_edge > complexity_edge:
        available = maximum_solves - len(records)
        budgets = sorted({int(round(complexity_edge + (mass_edge - complexity_edge) * i / (available + 1)))
                          for i in range(1, available + 1)}, reverse=budget_order == "mass-first")
        for budget in budgets:
            if complexity_edge < budget < mass_edge:
                solve(mass, "mass_kg", budget)
    telemetry.update(solves=records, runtime_ms=(perf_counter() - started) * 1000,
                     proposal_count=len(genomes))
    return PoolPolishResult(tuple(genomes), telemetry, timed_out)
