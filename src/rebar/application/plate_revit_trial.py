"""Lossless four-direction transfer to a mandatory-rollback Revit experiment.

This exports a chosen analysis point, not an engineering placement authorization.
No reinforcement coordinates, phases, lengths, quantities or diameters are repaired.
"""
from __future__ import annotations

import math

from rebar.optimization.services.detailing import rebar_mass_kg

DIRECTIONS = ("bottom-X", "bottom-Y", "top-X", "top-Y")


def _same(actual: float, expected: float, field: str) -> None:
    if not math.isfinite(actual) or not math.isfinite(expected) or abs(actual - expected) > 1e-5:
        raise ValueError(f"Source {field} does not match exported bars")


def build_full_plate_trial(report: dict, candidate_index: int, source_sha256: str) -> dict:
    """Flatten ALL chosen zones/components into exact regular Rebar runs."""
    if (report.get("schema_version") != "composite-plate-analysis/v1"
            or report.get("units") != "mm" or report.get("placement_eligible") is not False
            or report.get("source_demand_preserved") is not True):
        raise ValueError("Expected a complete, demand-preserving composite plate report in mm")
    if report.get("demo"):
        raise ValueError("Use a real input report, not the public UI demo")
    if type(candidate_index) is not int or not 0 <= candidate_index < len(report["front"]):
        raise ValueError("Explicit valid front index required")
    if len(report["directions"]) != 4:
        raise ValueError("All four directions are required")
    point = report["front"][candidate_index]
    if len(point["direction_candidate_indexes"]) != 4:
        raise ValueError("Incomplete front point")
    exported, total_count, zone_count, mass, blockers = [], 0, 0, [], set(report["blocking_check_ids"])
    for source, direction, index in zip(report["directions"], DIRECTIONS, point["direction_candidate_indexes"]):
        if direction != "{layer}-{axis}".format(**source["direction"]):
            raise ValueError("Direction ordering differs from the full-plate contract")
        candidates = [c for c in source["candidates"] if c["candidate_index"] == index]
        if len(candidates) != 1:
            raise ValueError("Selected direction candidate is missing or duplicated")
        candidate = candidates[0]
        coverage = candidate["coverage"]
        if coverage["coverage_passed"] is not True or coverage["uncovered_cell_count"] != 0:
            raise ValueError("No partial/under-covered plate can be transferred as complete")
        runs, direction_mass, direction_count, seen_zones = [], [], 0, set()
        axis = 0 if direction.endswith("X") else 1
        for zone in candidate["zone_drafts"]:
            if (zone["schema_version"] != "reinforcement-zone-revit/v2" or zone["units"] != "mm"
                    or zone["direction"] != source["direction"]
                    or zone["checks"]["geometry_and_schedule"] != "pass"):
                raise ValueError("Unexpected or invalid zone export")
            zone_id = zone["source_zone_id"]
            if zone_id in seen_zones or not zone["components"]:
                raise ValueError("Repeated or empty zone")
            seen_zones.add(zone_id)
            blockers.update(zone["checks"]["blocking_check_ids"])
            for component in zone["components"]:
                box = component["bar_axis_bbox_mm"]
                length = box[axis + 2] - box[axis]
                _same(length, component["installed_length_mm"], "installed length")
                coordinates = []
                for run_index, run in enumerate(component["uniform_runs"]):
                    count = run["bar_count"]
                    coordinates.extend(run["first_axis_mm"] + k * run["actual_step_mm"] for k in range(count))
                    a, b = [0.0, 0.0], [0.0, 0.0]
                    a[axis], b[axis] = box[axis], box[axis + 2]
                    a[1-axis] = b[1-axis] = run["first_axis_mm"]
                    runs.append({"id": f"{direction}:{zone_id}:{component['component_index']}:{run_index}",
                        "zone_id": zone_id, "component_index": component["component_index"],
                        "steel_class": source["settings"]["steel_class"], "diameter_mm": component["diameter_mm"],
                        "start_xy_mm": a, "end_xy_mm": b, "bar_count": count, "spacing_mm": run["actual_step_mm"]})
                coordinates.sort()
                expected_axes = sorted(component["axis_coordinates_mm"])
                if len(coordinates) != len(expected_axes) or len(coordinates) != component["bar_count"]:
                    raise ValueError("Uniform runs do not preserve component quantity")
                for actual, expected in zip(coordinates, expected_axes):
                    _same(actual, expected, "bar axis")
                value = rebar_mass_kg(component["diameter_mm"], length, len(coordinates))
                _same(value, component["mass_kg"], "component mass")
                direction_mass.append(value)
                direction_count += len(coordinates)
        _same(math.fsum(direction_mass), candidate["metrics"]["additional_mass_kg"], "direction mass")
        _same(direction_count, candidate["metrics"]["physical_bar_count"], "direction count")
        _same(len(seen_zones), candidate["metrics"]["zone_count"], "direction zones")
        exported.append({"direction": direction, "source": source["source"]["filenames"]["dxf"], "runs": runs})
        mass.extend(direction_mass)
        total_count += direction_count
        zone_count += len(seen_zones)
    _same(math.fsum(mass), point["additional_mass_kg"], "plate mass")
    _same(total_count, point["physical_bar_count"], "plate count")
    _same(zone_count, point["zone_count"], "plate zones")
    return {"schema_version": "qmonitoring-full-plate-trial/v1", "mode": "commit-readback-rollback",
        "units": "mm", "placement_eligible": False, "case_id": report["case_id"] or "Full plate",
        "source_report_sha256": source_sha256, "source_blockers": sorted(blockers), "directions": exported,
        "expected": {"zone_count": zone_count, "run_count": sum(len(d["runs"]) for d in exported),
                     "physical_bar_count": total_count, "additional_mass_kg": math.fsum(mass)}}
