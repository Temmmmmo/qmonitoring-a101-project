"""DXF/Mosaic → составной пул → независимое review выбранного результата."""
from __future__ import annotations

from copy import deepcopy

from rebar.models import Mosaic
from rebar.optimization.algorithms.composite_pool import solve_composite_pool
from rebar.optimization.algorithms.composite_partition import solve_composite_partition
from rebar.optimization.contracts.composite_search import CompositeSearchProblem

from .composite_layout_review import prepare_composite_review, review_composite_layout
from .composite_host_review import HOST_COORDINATE_POLICY, rectangular_host_from_reference, review_composite_host


def optimize_composite_layout(
    mosaic: Mosaic, configuration: dict, *, host_reference: dict | None = None,
    coordinate_policy: str | None = None, host_policy: str = "report-only",
    algorithm: str = "whole-cell-pool", **search_options,
) -> dict:
    """Конфиг совпадает с review-input, но zones обязаны быть пустыми: их ищет ядро."""
    demand, constraints, placements = prepare_composite_review(mosaic, configuration)
    if configuration["zones"]:
        raise ValueError("для поиска zones должны быть пустыми; заданные зоны проверяйте через review")
    if (host_reference is None) != (coordinate_policy is None):
        raise ValueError("host reference и явная coordinate_policy передаются вместе")
    if (host_policy not in ("report-only", "require-planar-containment", "interior-exceptions")
            or (host_policy != "report-only" and host_reference is None)):
        raise ValueError("неподдерживаемая host_policy или отсутствует host")
    if host_reference is not None and coordinate_policy != HOST_COORDINATE_POLICY:
        raise ValueError("нужна явная поддержанная политика XY-привязки")
    host = rectangular_host_from_reference(host_reference) if host_reference is not None else None
    problem = CompositeSearchProblem(demand, tuple(sorted(placements.items())), constraints, configuration["policy_id"],
                                     host if host_policy != "report-only" else None,
                                     "interior-exceptions" if host_policy == "interior-exceptions" else "strict")
    if algorithm not in ("whole-cell-pool", "fragment-grid"):
        raise ValueError("неизвестный составной алгоритм")
    solver = solve_composite_pool if algorithm == "whole-cell-pool" else solve_composite_partition
    result = solver(problem, **search_options)
    front = []
    for point in result.points:
        layout = deepcopy(configuration)
        layout["zones"] = [{"id": zone.id, "level_index": zone.level_index,
                            "demand_bbox_mm": list(zone.demand_bbox)} for zone in point.zones]
        front.append({"zone_count": len(point.zones), "physical_bar_count": point.coverage.physical_bar_count,
                      "additional_mass_kg": point.coverage.additional_mass_kg,
                      "maximum_installed_bar_length_mm": max((c.installed_length_mm for z in point.zones for c in z.components), default=0),
                      "cutting_profile": constraints.cutting_profile,
                      "stock_length_and_splices_approval": "not_checked",
                      "specification_position_count": None,
                      "uncovered_cell_count": point.coverage.uncovered_cell_count, "review_input": layout})
        if host_reference is not None:
            front[-1]["host_preflight"] = review_composite_host(mosaic, layout, host_reference,
                                                                coordinate_policy=coordinate_policy)
    selected = None if result.selected_index is None else review_composite_layout(
        mosaic, front[result.selected_index]["review_input"])
    status = "research_front_found" if selected else "no_research_solution_found"
    partition = result.telemetry.get("interior_partition")
    if partition is not None:
        status = "partial_research_front_found" if selected else "no_interior_solution_found"
        for point, validated in zip(front, result.points):
            ids = set(partition["target_cell_ids"])
            # Whole original coverage remains in each point; never claim zero missing
            # demand merely because the optimizer was given a restricted target.
            point["coverage_scope"] = "original_direction_including_boundary_exceptions"
            point["mass_scope"] = "selected_zones_only_boundary_completion_not_included"
            point["target_cell_count"] = len(ids)
            point["target_uncovered_cell_count"] = sum(not cell.covered for cell in validated.coverage.cells
                                                        if cell.cell_id in ids)
            point["original_uncovered_area_mm2"] = sum(cell.uncovered_area_mm2 for cell in validated.coverage.cells)
            point["full_solution_mass_kg"] = None
            point["engineer_comparison"] = {"status": "not_checked", "mass_gate_passed": None,
                "reason": "Partial scope; a matching recipe/direction engineer reference and boundary completion are required."}
        if not partition["target_cell_count"]:
            status = "no_interior_demand"
    elif result.telemetry.get("host_demand_feasibility", {}).get("requires_engineering_decision"):
        status = "engineering_decision_required"
    return {"schema_version": "composite-layout-search/v1", "mode": "research-only", "units": "mm",
            "status": status,
            "optimizer_executed": result.telemetry.get("solver_executed", False),
            "placement_eligible": False, "source_cell_count": len(demand.cells),
            "host_policy": host_policy,
            "interior_partition": partition,
            "phase_approval": "not_checked", "phase_source": configuration["phase_source"],
            "front": front, "selected_index": result.selected_index, "selected_review": selected,
            "telemetry": result.telemetry,
            "warning": ("Частичная задача: краевая полоса не решена; массу нельзя сравнивать с полной инженерной выдачей. "
                        if partition is not None else "") + "Конечный research-фронт, не глобальный оптимум и не разрешение Revit. "
                       "Покрытие не заменяет проверки host, фаз, укладки и нормативов. "
                       "Число стержней дано до проектного решения о товарных длинах/стыках; позиции спецификации не посчитаны."}
