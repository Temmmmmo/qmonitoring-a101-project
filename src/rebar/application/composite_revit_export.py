"""Отдельный draft v2 для составной зоны, не подмена plate-solution-revit/v1."""

from __future__ import annotations

from typing import Any

from rebar.models import Axis
from rebar.optimization.contracts import DemandMap, LayoutConstraints
from rebar.optimization.contracts.placement import CompositeLayoutZone
from rebar.optimization.services.axis_patterns import pattern_coordinates, pattern_runs
from rebar.optimization.services.bar_geometry import axis_envelope_to_body_bbox
from rebar.optimization.services.composite_detailing import evaluate_composite_zone
from rebar.reporting.serialization import to_jsonable

COMPOSITE_REVIT_EXPORT_SCHEMA = "reinforcement-zone-revit/v2"


def build_composite_zone_revit_export(
    demand: DemandMap,
    zone: CompositeLayoutZone,
    *,
    constraints: LayoutConstraints | None = None,
) -> dict[str, Any]:
    """Воспроизводимые параметры всех добавок; готовность к размещению НЕ заявляется."""
    check = evaluate_composite_zone(demand, zone, constraints=constraints)
    if not check.geometry_valid:
        raise ValueError("составная зона не прошла проверку: " + "; ".join(check.diagnostics))
    components = []
    for component in zone.components:
        coordinates = pattern_coordinates(component.placement, component.axis_window_mm)
        runs = pattern_runs(component.placement, component.axis_window_mm)
        start, end = component.longitudinal_interval_mm
        bbox = ((start, coordinates[0], end, coordinates[-1]) if zone.direction.axis is Axis.X
                else (coordinates[0], start, coordinates[-1], end))
        components.append({
            "component_index": component.component_index,
            "diameter_mm": component.rebar.diameter,
            "nominal_step_mm": component.rebar.step,
            "placement": to_jsonable(component.placement),
            "axis_window_mm": list(component.axis_window_mm),
            "axis_coordinates_mm": list(coordinates),
            "uniform_runs": to_jsonable(runs),
            "bar_count": component.bar_count,
            "bar_axis_bbox_mm": list(bbox),
            "straight_bar_body_bbox_mm": list(axis_envelope_to_body_bbox(
                zone.direction.axis, bbox, component.rebar.diameter,
            )),
            "required_length_mm": component.required_length_mm,
            "anchored_length_mm": component.anchored_length_mm,
            "installed_length_mm": component.installed_length_mm,
            "mass_kg": component.mass_kg,
        })
    return {
        "schema_version": COMPOSITE_REVIT_EXPORT_SCHEMA, "contract_status": "draft",
        "units": "mm", "source_zone_id": zone.id, "direction": to_jsonable(zone.direction),
        "demand_bbox_mm": list(zone.demand_bbox), "level_index": zone.level_index,
        "recipe": to_jsonable(zone.recipe), "placement_source": zone.placement.source,
        "background": {"specification": to_jsonable(zone.recipe.background),
                       "placement": to_jsonable(zone.placement.background),
                       "included_in_additional_mass": False},
        "components": components,
        "metrics": {"zone_count": check.zone_count, "component_count": check.component_count,
                    "uniform_run_count": check.uniform_run_count,
                    "physical_bar_count": check.physical_bar_count,
                    "additional_mass_kg": check.total_mass_kg,
                    "additional_bar_length_mm": check.total_bar_length_mm},
        "checks": {
            "geometry_and_schedule": "pass", "export_eligible": False,
            "blocking_check_ids": ["composite-demand-coverage", "composite-minimum-width", "a101-composite-positions",
                                   "host-boundary-cover-openings", "xy-layer-order",
                                   "background-and-additions-3d-collisions", "revit-readback"],
            "diagnostics": list(check.diagnostics),
        },
        "placement_boundary_policy": "body_bbox_is_not_an_area_boundary_or_a_host_clearance_check",
    }
