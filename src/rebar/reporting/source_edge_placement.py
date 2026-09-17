"""External source-packet postprocess for the user-approved total-80d edge proxy."""
from copy import deepcopy

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from rebar.models import Axis, Rebar
from rebar.optimization.services.anchorage import FixedDiameterAnchoragePolicy
from rebar.optimization.services.edge_envelope_placement import place_edge_envelope
from rebar.reporting.source_graphics import SOURCE_GRAPHICS_SCHEMA, SOURCE_STAGE


EDGE_POLICY_ID = "source-edge-total-80d/research-v1"


def apply_source_edge_placement(source_graphics):
    """Return a new packet; raw source graphics and v2 zone fields stay untouched."""
    packet = deepcopy(source_graphics)
    checks, policy, before_outside, after_outside, component_count, unknown = [], FixedDiameterAnchoragePolicy(), 0, 0, 0, 0
    total_invalid, core_invalid = 0, 0
    expected = {("bottom", "X"), ("bottom", "Y"), ("top", "X"), ("top", "Y")}
    directions = packet.get("directions", ())
    keys = {(row.get("direction", {}).get("layer"), row.get("direction", {}).get("axis")) for row in directions}
    if (packet.get("schema_version") != SOURCE_GRAPHICS_SCHEMA or packet.get("units") != "mm"
            or packet.get("source_stage") != SOURCE_STAGE or keys != expected or len(directions) != 4):
        return packet, {"status": "not_checked", "reason": "not_original_mm_source"}
    for row in packet.get("directions", ()):
        if not row.get("cells"):
            checks.append({"direction": row.get("direction"), "status": "not_checked", "reason": "empty_cells"})
            unknown += 1
            continue
        mesh = unary_union([Polygon(cell["polygon_mm"]) for cell in row.get("cells", ())])
        if mesh.geom_type != "Polygon":
            checks.append({"direction": row.get("direction"), "status": "not_checked", "reason": "multipart_exterior"})
            unknown += 1
            continue
        exterior, axis = Polygon(mesh.exterior), Axis(row["direction"]["axis"])
        buffered_exterior = exterior.buffer(.01, join_style=2)
        for zone in row.get("zone_drafts", ()):
            core = (zone["demand_bbox_mm"][0], zone["demand_bbox_mm"][2]) if axis is Axis.X else (zone["demand_bbox_mm"][1], zone["demand_bbox_mm"][3])
            for component in zone.get("components", ()):
                component_count += 1
                total = 2*policy.extension_each_end_mm(Rebar(1, component["diameter_mm"]))
                before = tuple(component["bar_axis_bbox_mm"])
                before_outside += not buffered_exterior.covers(box(*before))
                placed = place_edge_envelope(exterior, before, axis, core, component["installed_length_mm"], total)
                entry = {"direction": row["direction"], "source_zone_id": zone["source_zone_id"], "component_index": component["component_index"],
                         "status": placed.status, "reason": placed.reason, "before_bounds_mm": before, "after_bounds_mm": placed.bounds_mm,
                         "shift_mm": placed.shift_mm, "total_extension_mm": total}
                checks.append(entry)
                if placed.status == "pass":
                    component["bar_axis_bbox_mm"] = list(placed.bounds_mm)
                    shift = placed.shift_mm
                    if "straight_bar_body_bbox_mm" in component:
                        body = component["straight_bar_body_bbox_mm"]
                        component["straight_bar_body_bbox_mm"] = ([body[0]+shift, body[1], body[2]+shift, body[3]]
                            if axis is Axis.X else [body[0], body[1]+shift, body[2], body[3]+shift])
                    if abs(shift) > 1e-6 and isinstance(zone.get("checks"), dict):
                        zone["checks"]["geometry_and_schedule"] = "not_checked"
                after = component["bar_axis_bbox_mm"]
                after_outside += not buffered_exterior.covers(box(*after))
                after_lo, after_hi = (after[0], after[2]) if axis is Axis.X else (after[1], after[3])
                left, right = core[0] - after_lo, after_hi - core[1]
                total_invalid += left + right < total - 1e-6
                core_invalid += left < -1e-6 or right < -1e-6
    passed = sum(row["status"] == "pass" for row in checks)
    failed = sum(row["status"] == "fail" for row in checks)
    outer_status = "fail" if after_outside else "not_checked" if unknown or not component_count else "pass"
    def status(invalid):
        return "not_checked" if unknown or not component_count else "fail" if invalid else "pass"
    packet["edge_placement"] = {"policy_id": EDGE_POLICY_ID, "status": "research_only", "component_count": component_count, "placed_count": passed,
                                "remaining_count": failed, "before_outside_count": before_outside,
                                "after_outside_count": after_outside, "fixed_count": before_outside-after_outside, "unknown_count": unknown,
                                "outer_envelope_status": outer_status,
                                "total_extension_status": status(total_invalid),
                                "longitudinal_core_containment_status": status(core_invalid), "checks": checks,
                                "native_host_boundary": "not_checked", "engineering_approval": False}
    note = "Краевой перенос суммарного 80d: нет инженерного подтверждения."
    if note not in packet.get("note", ""):
        packet["note"] = (packet.get("note", "") + " " + note).strip()
    return packet, packet["edge_placement"]
