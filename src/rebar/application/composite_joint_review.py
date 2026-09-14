"""Офлайн-эксперимент высот составных наборов. Не меняет экспорт и команды Revit."""
from __future__ import annotations

from dataclasses import replace

from rebar.optimization.algorithms.composite_joint_depths import solve_composite_joint_depths
from rebar.optimization.contracts.composite_search import CompositeSearchProblem
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.services.composite_interior import partition_interior_demand
from rebar.reporting.serialization import to_jsonable

from .composite_host_review import HOST_COORDINATE_POLICY, rectangular_host_from_reference
from .composite_layout_review import build_review_zones, prepare_composite_review


def research_composite_joint_depths(mosaic, configuration, reference, *, coordinate_policy,
        candidate_depths_by_component, minimum_clear_spacing_mm, hypothesis_source,
        preserve_component_order=False, time_limit_s=10, maximum_search_nodes=100000):
    if coordinate_policy != HOST_COORDINATE_POLICY:
        raise ValueError("нужна явная исследовательская XY-привязка")
    demand, constraints, zones = build_review_zones(mosaic, configuration)
    # Finite-depth experiment explicitly records a clearance assumption instead of
    # quietly treating the old geometric default of zero as a construction rule.
    constraints = replace(constraints, minimum_clear_spacing_mm=minimum_clear_spacing_mm)
    _, _, placements = prepare_composite_review(mosaic, configuration)
    host = rectangular_host_from_reference(reference)
    problem = CompositeSearchProblem(demand, tuple(sorted(placements.items())), constraints,
                                     configuration["policy_id"], host, "interior-exceptions")
    partition = partition_interior_demand(problem)
    baseline = evaluate_composite_coverage(demand, zones, policy_id=configuration["policy_id"], constraints=constraints)
    ids = set(partition["target_cell_ids"])
    if not ids or any(not c.covered for c in baseline.cells if c.cell_id in ids):
        raise ValueError("для поиска стыков нужно полное покрытие непустого внутреннего target")
    result = solve_composite_joint_depths(demand, zones, host, policy_id=configuration["policy_id"], constraints=constraints,
        candidate_depths_by_component=candidate_depths_by_component, hypothesis_source=hypothesis_source,
        preserve_component_order=preserve_component_order, time_limit_s=time_limit_s, maximum_search_nodes=maximum_search_nodes)
    return {"schema_version": "composite-joint-depth-research/v1", "mode": "research-only", "units": "mm",
        "placement_eligible": False, "status": result.telemetry["status"],
        "coordinate_policy": coordinate_policy, "input": configuration,
        "interior_partition": partition, "target_uncovered_cell_count": 0,
        "original_uncovered_cell_count": baseline.uncovered_cell_count,
        "original_uncovered_area_mm2": baseline.uncovered_area_mm2,
        "zone_count": len(zones), "physical_bar_count": baseline.physical_bar_count,
        "additional_mass_kg": baseline.additional_mass_kg,
        "maximum_installed_bar_length_mm": max(c.installed_length_mm for z in zones for c in z.components),
        "full_solution_mass_kg": None, "engineer_comparison": "not_checked",
        "mass_scope": "selected_zones_only_boundary_completion_not_included",
        "clearance_engineering_approval": "not_checked", "phase_approval": "not_checked",
        "depth_search": result.telemetry,
        "proposed_zones": to_jsonable(result.points[0].zones) if result.points else [],
        "warning": "Только геометрическая гипотеза одного направления. Разнесение по глубине меняет рабочую высоту; "
                   "прочность, узлы нахлёста/разбежка, фон, X/Y и край не подтверждены. Это НЕ пакет размещения в Revit."}
