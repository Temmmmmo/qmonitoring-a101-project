"""Honest repaired graphic transport: new bars are never old trim originals."""
from copy import deepcopy
import hashlib
import math

from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from rebar.models import Axis
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS
from rebar.optimization.services.opening_relocation import lane_map
from rebar.optimization.services.shaped_geometry import shaped_cutting_schedule
from rebar.optimization.services.tz_boundary_trim import geometry_presence_offers
from rebar.reporting.serialization import to_jsonable
from .boundary_trim_web import _raw_bar
from .physical_layout_recovery import _bytes

SCHEMA = "graphic-bar-plan-repaired/v1"


def _fe_ids(bar, sources, index):
    offers = geometry_presence_offers((bar,), sources).get(bar.direction, ())
    tree, cells, polygons, demand = index[bar.direction]
    result, sufficient = [], {}
    all_offers = unary_union([p for _, _, p in offers])
    for position in tree.query(all_offers):
        cell = cells[int(position)]
        additions = demand.level(cell.level_index).recipe.additions
        if not additions:
            continue
        spec = additions[0]
        key = spec.diameter, spec.step
        if key not in sufficient:
            sufficient[key] = unary_union([p for d, s, p in offers if d >= spec.diameter and s <= spec.step])
        if polygons[int(position)].intersection(sufficient[key]).area > 0:
            result.append(cell.id)
    return sorted(result)


def build_repaired_graphic_packet(source_packet, before, after, checks, lanes, problem):
    """Bind immutable trim bytes, explicit operations, source manifest and fresh proof."""
    if (source_packet.get("schema_version") != "graphic-bar-plan-draft/v1"
            or source_packet.get("case_id") != problem.case_id
            or source_packet.get("placement_eligible") is not False
            or source_packet.get("engineering_approval") is not False
            or checks.get("accepted_nonregression") is not True):
        raise ValueError("Repaired export requires bound trim history and independent nonregression")
    sources = lane_map(lanes)
    index = {}
    for original in problem.direction_problems:
        polygons = [Polygon(cell.poly) for cell in original.demand.cells]
        index[original.demand.direction] = STRtree(polygons), original.demand.cells, polygons, original.demand
    source_rows = {(direction, raw["id"]): raw for row in source_packet["directions"]
                   for direction in [f'{row["direction"]["layer"]}-{row["direction"]["axis"]}'] for raw in row["bars"]}
    old = {(str(bar.direction), bar.id): bar for bar in before}
    final = {(str(bar.direction), bar.id): bar for bar in after}
    if set(source_rows) != set(old) or not set(old) <= set(final) or any(
            any(source_rows[key].get(k) != v for k, v in _raw_bar(bar).items()) for key, bar in old.items()):
        raise ValueError("Exact trimmed physical inventory differs from immutable history")
    metrics = checks["physical_metrics"]
    schedule = shaped_cutting_schedule(after)
    if len(schedule) != metrics["position_count"] or abs(math.fsum(p.total_mass_kg for p in schedule)-metrics["mass_kg"]) > 1e-6:
        raise ValueError("Fresh repaired schedule and checker metrics disagree")
    operations, additions, directions, lane_fe = [], [], [], {}
    for direction in PLATE_DIRECTIONS:
        rows = []
        for bar in after:
            if bar.direction != direction:
                continue
            key = str(direction), bar.id
            previous = old.get(key)
            kind = "added" if previous is None else "retained" if previous == bar else "modified"
            reason = {"added": "finite-source-lane-positive-deficit-gain", "retained": "unchanged-trimmed-bar",
                      "modified": "exact-deficit-FE-edge-minus-original-service-half-width"}[kind]
            fe_ids = _fe_ids(bar, sources, index)
            for owner in bar.source_bar_ids:
                lane_fe.setdefault((str(direction), owner), set()).update(fe_ids)
            if kind != "retained" and not fe_ids:
                raise ValueError("New repair operation must reference positive-area original FE service")
            provenance = {"kind": kind, "policy_id": checks["policy_id"], "source_fe_ids": fe_ids,
                          "lane_ids": list(bar.source_bar_ids), "reason": reason}
            rows.append({**_raw_bar(bar), "provenance": provenance})
            record = {"direction": str(direction), "reason": reason, "source_fe_ids": fe_ids,
                      "lane_ids": list(bar.source_bar_ids)}
            if kind == "added":
                additions.append({**record, "bar_id": bar.id, "policy_id": checks["policy_id"]})
            else:
                record.update(source_bar_id=bar.id, output_bar_ids=[bar.id], kind=kind)
                if kind == "modified":
                    across = 1 if direction.axis is Axis.X else 0
                    record.update(original_coordinate_mm=previous.segments[0].start_mm[across],
                                  coordinate_mm=bar.segments[0].start_mm[across])
                operations.append(record)
        directions.append({"direction": to_jsonable(direction), "bars": rows})
    manifest = {str(p.demand.direction): [cell.id for cell in p.demand.cells
        if p.demand.level(cell.level_index).recipe.additions] for p in problem.direction_problems}
    checker_json = _bytes(checks).decode("utf-8")
    lane_rows = to_jsonable(lanes)
    for lane, row in zip(lanes, lane_rows):
        row["source_fe_ids"] = sorted(lane_fe.get((str(lane.source.direction), lane.source.id), ()))
    repair = {"schema_version": "flat-trim-repair-certificate/v1", "policy_id": checks["policy_id"],
        "accepted_nonregression": True, "source_demand_removed": False,
        "original_FE_geometry_changed": False, "weak_As_summation": False,
        "source_fe_ids_by_direction": manifest, "source_lanes": lane_rows,
        "checks": deepcopy(checks), "checker_json": checker_json,
        "checker_sha256": hashlib.sha256(checker_json.encode()).hexdigest(),
        "placement_eligible": False, "engineering_approval": False}
    conditional = checks["collisions"]
    return {"schema_version": SCHEMA, "units": "mm", "case_id": problem.case_id,
        "geometry_kind": "straight-bars-only", "source_stage": "flat-trim-deficit-repaired",
        "source_trim_packet": deepcopy(source_packet), "source_trim_packet_json": _bytes(source_packet).decode("utf-8"),
        "source_trim_packet_sha256": hashlib.sha256(_bytes(source_packet)).hexdigest(),
        "directions": directions, "operations": operations, "additions": additions, "repair": repair,
        "expected": {"physical_bar_count": metrics["physical_bar_count"], "additional_mass_kg": metrics["mass_kg"],
                     "position_count": metrics["position_count"], "source_zone_count": source_packet["after"]["source_zone_count"]},
        "checks": {"coverage": checks["geometric_presence"]["status"],
            "anchorage_40d": checks["coverage_with_control_40d"]["status"], "stock_cutting": checks["stock_cutting"]["status"],
            "outer_boundary": {"status": "fail" if checks["material_boundary_failures_after"] else "pass",
                               "failure_count": checks["material_boundary_failures_after"]},
            "collisions_3d": {"status": "not_checked", "proven_pair_count": None, "uncertain_pair_count": None},
            "conditional_collisions_3d": {"status": conditional["status"],
                "proven_pair_count": conditional["proven_collision_pair_count"], "uncertain_pair_count": conditional["uncertain_pair_count"]}},
        "placement_eligible": False, "engineering_approval": False}
