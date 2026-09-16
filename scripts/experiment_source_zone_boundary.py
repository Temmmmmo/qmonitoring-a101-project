"""Read-only boundary experiment for original source component envelopes.

It does not alter zones, bars, stock choices, or the input report.
"""
import argparse
import json
import math
from pathlib import Path

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from rebar.models import Rebar
from rebar.optimization.services.anchorage import FixedDiameterAnchoragePolicy
from rebar.optimization.services.cutting import PLATE_11700_CUT_LENGTHS_MM


SOURCE_STAGE = "original-parametric-zones-before-physical-normalization"


def source_packets(report):
    yield "variant-0", report["source_graphics"]
    for index, variant in enumerate(report.get("layout_variants", ())[1:], 1):
        nested = variant.get("report") or {}
        if nested.get("source_graphics"):
            yield f"variant-{index}", nested["source_graphics"]


def exterior(direction, tolerance):
    merged = unary_union([Polygon(cell["polygon_mm"]) for cell in direction["cells"]])
    if merged.geom_type != "Polygon":
        raise ValueError("Expected one connected FE exterior")
    # Deliberately ignores holes: this is not a native host/openings check.
    return Polygon(merged.exterior).buffer(tolerance)


def rectangle(values):
    if (not isinstance(values, list) or len(values) != 4
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) for value in values)):
        raise ValueError("Expected finite bbox")
    return box(*values)


def frame(component, demand, axis, extension):
    x1, y1, x2, y2 = component["bar_axis_bbox_mm"]
    dx1, dy1, dx2, dy2 = demand
    return (box(dx1 - extension, y1, dx2 + extension, y2)
            if axis == "X" else box(x1, dy1 - extension, x2, dy2 + extension))


def base_frame(component, demand, axis):
    return frame(component, demand, axis, 0)


def example(zone, component, direction, demand, tight, outline):
    return {"zone_id": zone.get("source_zone_id"), "direction": direction,
            "diameter_mm": component["diameter_mm"], "demand_bbox_mm": demand,
            "tight_axis_bbox_mm": list(tight.bounds),
            "outside_area_mm2": round(tight.difference(outline).area, 3)}


def inspect_packet(name, packet, tolerance):
    if packet.get("source_stage") != SOURCE_STAGE or packet.get("units") != "mm":
        raise ValueError("Expected original source packet in mm")
    policy = FixedDiameterAnchoragePolicy()
    result = {"variant": name, "component_count": 0, "existing_outside": 0,
              "outside_tight_inside": 0, "outside_tight_outside_base_inside": 0,
              "outside_tight_outside_base_outside": 0, "demand_bbox_outside": 0,
              "has_shorter_catalogue_candidate": 0, "has_arbitrarily_shorter_length": 0,
              "tight_outside_by_outer_bbox": {"along": 0, "transverse": 0, "both": 0, "exterior_notch": 0},
              "examples": {"edge_expansion": [], "nonrectangular_fit": []}}
    for direction in packet["directions"]:
        outline = exterior(direction, tolerance)
        axis = direction["direction"]["axis"]
        for zone in direction["zone_drafts"]:
            demand = zone["demand_bbox_mm"]
            demand_shape = rectangle(demand)
            for component in zone["components"]:
                actual = rectangle(component["bar_axis_bbox_mm"])
                diameter = component["diameter_mm"]
                extension = policy.extension_each_end_mm(Rebar(1, diameter))
                tight = frame(component, demand, axis, extension)
                base = base_frame(component, demand, axis)
                actual_length = component["installed_length_mm"]
                tight_length = tight.bounds[2] - tight.bounds[0] if axis == "X" else tight.bounds[3] - tight.bounds[1]
                result["component_count"] += 1
                result["demand_bbox_outside"] += not outline.covers(demand_shape)
                if outline.covers(actual):
                    continue
                result["existing_outside"] += 1
                if outline.covers(tight):
                    result["outside_tight_inside"] += 1
                    result["has_arbitrarily_shorter_length"] += tight_length < actual_length - tolerance
                    result["has_shorter_catalogue_candidate"] += any(tight_length <= length < actual_length - tolerance
                                                                         for length in PLATE_11700_CUT_LENGTHS_MM)
                    continue
                group = "outside_tight_outside_base_inside" if outline.covers(base) else "outside_tight_outside_base_outside"
                result[group] += 1
                xmin, ymin, xmax, ymax = outline.bounds
                txmin, tymin, txmax, tymax = tight.bounds
                along = txmin < xmin or txmax > xmax if axis == "X" else tymin < ymin or tymax > ymax
                other = tymin < ymin or tymax > ymax if axis == "X" else txmin < xmin or txmax > xmax
                result["tight_outside_by_outer_bbox"]["both" if along and other else "along" if along else "transverse" if other else "exterior_notch"] += 1
                key = "edge_expansion" if group.endswith("inside") else "nonrectangular_fit"
                if len(result["examples"][key]) < 2:
                    result["examples"][key].append(example(zone, component, direction["direction"], demand, tight, outline))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, help="Write compact summary JSON, not source FE data")
    parser.add_argument("--tolerance-mm", type=float, default=.01)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    summary = {"method": "component axis frame; source FE exterior only; holes ignored",
               "tolerance_mm": args.tolerance_mm,
               "variants": [inspect_packet(name, packet, args.tolerance_mm) for name, packet in source_packets(report)]}
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
