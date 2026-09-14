"""Компактный черновой контракт выбранного решения для Revit-интеграции."""

from __future__ import annotations

from typing import Any

from rebar.optimization import PlateProblem, PlateSolution
from rebar.optimization.services.bar_geometry import axis_envelope_to_body_bbox
from rebar.optimization.services.bar_schedule import build_bar_schedule, layout_schedule_groups
from rebar.reporting.serialization import to_jsonable
from rebar.reporting.zone_schedule import build_zone_schedule

from .gate_assessment import assess_plate_gates

REVIT_EXPORT_SCHEMA = "plate-solution-revit/v1"

_EXPORT_SAFETY_GATES = frozenset(
    {
        "plate-directions",
        "source-demand-preserved",
        "demand-coverage",
        "minimum-zone-width",
        "anchorage",
        "step-multiple",
        "postprocessing-conflicts",
        "a101-allowed-positions",
    }
)


def build_plate_solution_revit_export(
    problem: PlateProblem,
    solution: PlateSolution,
    *,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """Собрать выбранный кандидат без SVG, фронта и остальных web-данных.

    Доступность проверяется по существующим safety-гейтам модели зон. Контракт
    остаётся draft: габарит тел стержней не является контуром AreaReinforcement
    и не подтверждает проверку границ/проёмов/защитного слоя реального Revit-host.
    """

    assessment = assess_plate_gates(problem, solution)
    safety_items = tuple(
        item for item in assessment.items if item.id in _EXPORT_SAFETY_GATES
    )
    blocking_items = tuple(item for item in safety_items if item.status != "pass")
    blocking_check_ids = [item.id for item in blocking_items]
    if not solution.valid:
        blocking_check_ids.append("plate-solution-validity")
    export_eligible = solution.valid and not blocking_items

    schedule = build_bar_schedule(
        group for item in solution.direction_solutions
        for group in layout_schedule_groups(
            item.solution.zones, prefix=f"{item.direction}:",
            steel_class=item.solution.meta.get("steel_class", ""),
        )
    )
    position_by_source = {
        source_id: position.mark for position in schedule for source_id in position.source_ids
    }
    directions = []
    for direction_problem in problem.direction_problems:
        direction = direction_problem.demand.direction
        direction_solution = solution.solution(direction)
        rows_by_zone_id = {
            row.zone_id: row for row in build_zone_schedule(direction, direction_solution)
        }
        zones = []
        for zone in direction_solution.zones:
            row = rows_by_zone_id[zone.id]
            zones.append(
                {
                    "mark": row.mark,
                    "callout": row.callout,
                    "source_zone_id": zone.id,
                    "position_mark": position_by_source[f"{direction}:{zone.id}"],
                    "bbox_mm": list(zone.bbox),
                    "bbox_semantics": "bar_axis_envelope",
                    "straight_bar_body_bbox_mm": list(axis_envelope_to_body_bbox(
                        direction.axis, zone.bbox, zone.rebar.diameter,
                    )),
                    "demand_bbox_mm": list(zone.demand_bbox),
                    "level_index": zone.level_index,
                    "diameter_mm": zone.rebar.diameter,
                    "step_mm": zone.rebar.step,
                    "nominal_step_mm": zone.rebar.step,
                    "axis_pattern": {
                        "period_mm": zone.rebar.step, "offsets_mm": [0.0],
                        "origin_mm": zone.first_bar_coordinate_mm,
                        "source": "legacy_uniform_layout_zone",
                    },
                    "bar_count": zone.bar_count,
                    "first_bar_coordinate_mm": zone.first_bar_coordinate_mm,
                    "required_length_mm": zone.required_length_mm,
                    "anchored_length_mm": zone.anchored_length_mm,
                    "installed_length_mm": zone.installed_length_mm,
                    "width_mm": zone.width_mm,
                    "mass_kg": zone.mass_kg,
                }
            )
        directions.append(
            {
                "layer": direction.layer.value,
                "axis": direction.axis.value,
                "algorithm": direction_solution.algorithm,
                "status": direction_solution.status.value,
                "zones": zones,
            }
        )

    return {
        "schema_version": REVIT_EXPORT_SCHEMA,
        "contract_status": "draft",
        "units": "mm",
        "case_id": problem.case_id,
        "candidate": {
            "id": candidate_id,
            "status": solution.status.value,
            "algorithm_by_direction": to_jsonable(
                solution.meta.get("algorithm_by_direction", {})
            ),
        },
        "checks": {
            "direction_count": solution.metrics.direction_count,
            "under_reinforced_cell_count": (
                solution.metrics.under_reinforced_cell_count
            ),
            "export_eligible": export_eligible,
            "blocking_check_ids": blocking_check_ids,
            "diagnostics": list(solution.diagnostics),
        },
        "metrics": to_jsonable(solution.metrics),
        "bar_schedule": to_jsonable(schedule),
        "position_count": len(schedule),
        "position_count_scope": "straight_bars_by_diameter_length_and_declared_class",
        "steel_class_declared": bool(schedule) and all(row.steel_class for row in schedule),
        "directions": directions,
    }
