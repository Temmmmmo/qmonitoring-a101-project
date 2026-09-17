"""Bounded source-only transverse window rebuilding, before edge placement."""
from copy import deepcopy
from dataclasses import replace
import math
import time

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from rebar.application.composite_revit_export import build_composite_zone_revit_export
from rebar.models import Axis, Rebar
from rebar.optimization.contracts.placement import AxisPlacement, PeriodicAxisPattern, RecipePlacement
from rebar.optimization.services.anchorage import FixedDiameterAnchoragePolicy
from rebar.optimization.services.axis_patterns import pattern_coordinates
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.detailing import typical_transverse_cell_size_mm
from rebar.optimization.services.edge_envelope_placement import place_edge_envelope
from rebar.optimization.services.bar_schedule import build_bar_schedule, composite_schedule_groups
from rebar.optimization.contracts.composite_coverage import MONOTONE_SINGLE_STO_COVERAGE_POLICY
from rebar.reporting.source_graphics import SOURCE_GRAPHICS_SCHEMA, SOURCE_STAGE


def _placement(zone):
    def make(value):
        pattern = value["pattern"]
        return AxisPlacement(PeriodicAxisPattern(pattern["period_mm"], tuple(pattern["offsets_mm"])), value["origin_mm"], value.get("axis_depth_from_face_mm"))
    return RecipePlacement(make(zone["background"]["placement"]), tuple(make(c["placement"]) for c in zone["components"]), zone["placement_source"])


def _hydrate(demand, constraints, raw):
    zone = build_composite_zone(demand, tuple(raw["demand_bbox_mm"]), raw["level_index"], raw["source_zone_id"], _placement(raw), constraints=constraints,
        installed_lengths_mm=tuple(component["installed_length_mm"] for component in raw["components"]))
    parts = []
    for part, raw_part in zip(zone.components, raw["components"]):
        bounds = raw_part["bar_axis_bbox_mm"]
        interval = (bounds[0], bounds[2]) if demand.direction.axis is Axis.X else (bounds[1], bounds[3])
        parts.append(replace(part, longitudinal_interval_mm=interval))
    zone = replace(zone, components=tuple(parts))
    exported = build_composite_zone_revit_export(demand, zone, constraints=constraints)
    if exported["recipe"] != raw.get("recipe") or len(exported["components"]) != len(raw.get("components", ())):
        raise ValueError("raw zone recipe/components differ")
    for actual, saved in zip(exported["components"], raw["components"]):
        if any(actual[key] != saved.get(key) for key in ("component_index", "diameter_mm", "bar_count", "installed_length_mm", "mass_kg", "axis_coordinates_mm", "axis_window_mm")):
            raise ValueError("raw component inventory differs")
    return zone


def _signature(zones):
    return tuple(sorted((str(zone.direction), zone.id, part.component_index, part.rebar.diameter,
                         part.installed_length_mm, part.bar_count) for zone in zones for part in zone.components))


def _metrics(zones):
    schedule = build_bar_schedule(composite_schedule_groups(zones))
    return {"zone_count": len(zones), "component_count": sum(len(zone.components) for zone in zones),
            "bar_count": sum(part.bar_count for zone in zones for part in zone.components),
            "mass_kg": round(sum(part.mass_kg for zone in zones for part in zone.components), 6),
            "position_count": len(schedule)}


def _outside(zone, exterior):
    failed, policy = set(), FixedDiameterAnchoragePolicy()
    for part in zone.components:
        axes = pattern_coordinates(part.placement, part.axis_window_mm)
        lo, hi = part.longitudinal_interval_mm
        bounds = (lo, axes[0], hi, axes[-1]) if zone.direction.axis is Axis.X else (axes[0], lo, axes[-1], hi)
        core = (zone.demand_bbox[0], zone.demand_bbox[2]) if zone.direction.axis is Axis.X else (zone.demand_bbox[1], zone.demand_bbox[3])
        total = 2 * policy.extension_each_end_mm(Rebar(1, part.rebar.diameter))
        if place_edge_envelope(exterior, bounds, zone.direction.axis, core, part.installed_length_mm, total).status != "pass":
            failed.add(f"{zone.id}:{part.component_index}")
    return failed


def _windows(demand, constraints, zone):
    positive = [Polygon(cell.poly).intersection(box(*zone.demand_bbox)) for cell in demand.cells if demand.level(cell.level_index).requires_extra
                and Polygon(cell.poly).intersection(box(*zone.demand_bbox)).area > 1e-6]
    if not positive:
        return ()
    bounds, axis, part = unary_union(positive).bounds, demand.direction.axis, zone.components[0]
    lo, hi = (bounds[1], bounds[3]) if axis is Axis.X else (bounds[0], bounds[2])
    period = part.placement.pattern.period_mm
    width = math.ceil(max(hi-lo, constraints.min_width_cells * typical_transverse_cell_size_mm(demand)) / period) * period
    starts = [hi-width, lo, min(max(part.axis_window_mm[0], hi-width), lo)]
    for cell in demand.cells:
        for point in cell.poly:
            value = point[1 if axis is Axis.X else 0]
            starts.extend((value, value-width))
    valid = [start for start in dict.fromkeys(starts) if start <= lo + 1e-6 and hi <= start+width + 1e-6]
    return tuple((bounds[0], start, bounds[2], start+width) if axis is Axis.X else (start, bounds[1], start+width, bounds[3])
                 for start in valid[:16])


def rebuild_source_graphics(problem, source_graphics, *, seconds=10):
    """Fail closed: only same-ID/length/count non-worsening source candidates pass."""
    packet, started = deepcopy(source_graphics), time.monotonic()
    if (packet.get("schema_version") != SOURCE_GRAPHICS_SCHEMA or packet.get("source_stage") != SOURCE_STAGE
            or packet.get("units") != "mm" or packet.get("case_id") != problem.case_id
            or len(packet.get("directions", ())) != len(problem.direction_problems) or len(packet["directions"]) != 4):
        return packet, {"status": "not_checked", "reason": "direction_contract"}
    before_total = after_total = 0
    before_zones, after_zones, final_checks = [], [], []
    records = []
    for raw_row, direction_problem in zip(packet["directions"], problem.direction_problems):
        demand, constraints = direction_problem.demand, direction_problem.constraints
        if raw_row.get("direction") != {"layer": demand.direction.layer.value, "axis": demand.direction.axis.value}:
            return source_graphics, {"status": "not_checked", "reason": "direction_mismatch"}
        cells = raw_row.get("cells", ())
        try:
            matches_cells = len(cells) == len(demand.cells) and not any(tuple(map(tuple, raw["polygon_mm"])) != cell.poly or raw["cell_id"] != cell.id or raw["aci"] != cell.aci or raw["level_index"] != cell.level_index
                                                                        for raw, cell in zip(cells, demand.cells))
        except (KeyError, TypeError):
            matches_cells = False
        if not matches_cells:
            return source_graphics, {"status": "not_checked", "reason": "source_cells_mismatch"}
        mesh = unary_union([Polygon(cell.poly) for cell in demand.cells])
        if mesh.geom_type != "Polygon":
            return source_graphics, {"status": "not_checked", "reason": "multipart_exterior"}
        exterior = Polygon(mesh.exterior).buffer(.01, join_style=2)
        try:
            active = [_hydrate(demand, constraints, raw) for raw in raw_row["zone_drafts"]]
        except (KeyError, ValueError):
            return source_graphics, {"status": "not_checked", "reason": "source_lengths_not_rehydratable"}
        before_zones.extend(active)
        policy = MONOTONE_SINGLE_STO_COVERAGE_POLICY
        baseline = evaluate_composite_coverage(demand, tuple(active), policy_id=policy, constraints=constraints)
        if not baseline.geometry_and_patterns_valid or not baseline.coverage_passed:
            return source_graphics, {"status": "not_checked", "reason": "baseline_coverage"}
        outside = set().union(*(_outside(zone, exterior) for zone in active))
        before_total += len(outside)
        accepted, changes, unvisited = [], [], []
        for index, parent in enumerate(tuple(active)):
            if time.monotonic() - started >= seconds:
                unvisited = [zone.id for zone in active[index:]]
                break
            if not _outside(parent, exterior):
                continue
            lengths = tuple(part.installed_length_mm for part in parent.components)
            inventory = tuple((part.rebar.diameter, part.installed_length_mm, part.bar_count) for part in parent.components)
            for window in _windows(demand, constraints, parent)[:16]:
                try:
                    proposal = build_composite_zone(demand, window, parent.level_index, parent.id, parent.placement,
                        constraints=constraints, installed_lengths_mm=lengths)
                except ValueError:
                    continue
                if tuple((part.rebar.diameter, part.installed_length_mm, part.bar_count) for part in proposal.components) != inventory:
                    continue
                candidate = [*active[:index], proposal, *active[index+1:]]
                check = evaluate_composite_coverage(demand, tuple(candidate), policy_id=policy, constraints=constraints)
                candidate_outside = set().union(*(_outside(zone, exterior) for zone in candidate))
                if check.geometry_and_patterns_valid and check.coverage_passed and not candidate_outside-outside and len(candidate_outside) < len(outside):
                    active, outside = candidate, candidate_outside
                    accepted.append(parent.id)
                    changes.append({"source_zone_id": parent.id, "before_demand_bbox_mm": list(parent.demand_bbox),
                                    "after_demand_bbox_mm": list(proposal.demand_bbox)})
                    break
        accepted_set = set(accepted)
        raw_row["zone_drafts"] = [build_composite_zone_revit_export(demand, zone, constraints=constraints) if zone.id in accepted_set else raw
                                    for raw, zone in zip(raw_row["zone_drafts"], active)]
        final = evaluate_composite_coverage(demand, tuple(active), policy_id=policy, constraints=constraints)
        if not final.geometry_and_patterns_valid or not final.coverage_passed:
            return source_graphics, {"status": "not_checked", "reason": "final_coverage"}
        after_zones.extend(active)
        final_checks.append(final)
        after_total += len(outside)
        records.append({"direction": raw_row["direction"], "accepted_zone_ids": accepted, "changes": changes,
                        "remaining_component_ids": sorted(outside), "unvisited_zone_ids": unvisited})
    if _signature(before_zones) != _signature(after_zones):
        return source_graphics, {"status": "not_checked", "reason": "inventory_changed"}
    if not all(check.uncovered_cell_count == 0 and check.coverage_passed for check in final_checks):
        return source_graphics, {"status": "not_checked", "reason": "final_uncovered"}
    checks = {"status": "pass", "before_outside_count": before_total, "after_outside_count": after_total,
                    "directions": records, "timeout": time.monotonic()-started >= seconds, "engineering_approval": False,
                    "coverage_passed": True, "uncovered_cell_count": 0, "inventory_unchanged": True,
                    "before_metrics": _metrics(before_zones), "after_metrics": _metrics(after_zones),
                    "note": "Source-only relocation preserves selected lengths/count; stock is not recomputed here."}
    packet["zone_rebuild"] = deepcopy(checks)
    return packet, checks
