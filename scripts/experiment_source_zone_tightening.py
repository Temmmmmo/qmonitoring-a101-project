"""Read-only longitudinal tightening of saved source zones; never exports a candidate."""
import argparse
import json
from dataclasses import replace
from pathlib import Path

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from rebar.legend import parse_recipe
from rebar.models import Axis, Direction, Layer
from rebar.optimization.contracts import DemandCell, DemandLevel, DemandMap, LayoutConstraints
from rebar.optimization.contracts.composite_coverage import MONOTONE_SINGLE_STO_COVERAGE_POLICY
from rebar.optimization.contracts.placement import AxisPlacement, PeriodicAxisPattern, RecipePlacement
from rebar.optimization.services.bar_schedule import build_bar_schedule, composite_schedule_groups
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.cutting import PLATE_11700_BATCH_PROFILE, PLATE_11700_CUT_LENGTHS_MM
from rebar.optimization.services.axis_patterns import pattern_runs


SOURCE_STAGE = "original-parametric-zones-before-physical-normalization"
CONSTRAINTS = LayoutConstraints(min_width_cells=2, allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM,
                                cutting_profile=PLATE_11700_BATCH_PROFILE)


def direction(value):
    return Direction(Layer(value["layer"]), Axis(value["axis"]))


def demand_from(row):
    levels = []
    for value in row["legend"]:
        recipe = parse_recipe(value["label"])
        levels.append(DemandLevel(value["level_index"], value.get("aci"), value.get("as_min_cm2_per_m"),
            value.get("as_max_cm2_per_m"), value["label"], recipe.additions[0] if len(recipe.additions) == 1 else None,
            bool(recipe.additions), recipe))
    cells = tuple(DemandCell(value["cell_id"], tuple(map(tuple, value["polygon_mm"])),
        tuple(sum(point[i] for point in value["polygon_mm"]) / len(value["polygon_mm"]) for i in (0, 1)),
        value["aci"], value["level_index"]) for value in row["cells"])
    return DemandMap(direction(row["direction"]), tuple(levels), cells, tuple(row["source_bbox_mm"]))


def axes(value):
    pattern = value["pattern"]
    return AxisPlacement(PeriodicAxisPattern(pattern["period_mm"], tuple(pattern["offsets_mm"])),
                         value["origin_mm"], value.get("axis_depth_from_face_mm"))


def placement(zone):
    return RecipePlacement(axes(zone["background"]["placement"]),
        tuple(axes(component["placement"]) for component in zone["components"]), zone["placement_source"])


def narrow_bbox(demand, zone):
    """Keep all intersections of every actually additional FE cell; never drop a zone."""
    original = tuple(zone["demand_bbox_mm"])
    window = box(*original)
    parts = [Polygon(cell.poly).intersection(window) for cell in demand.cells if demand.level(cell.level_index).requires_extra
             and Polygon(cell.poly).intersection(window).area > 1e-6]
    if not parts:
        return original, "no_additional_intersections"
    bounds = unary_union(parts).bounds
    return ((bounds[0], original[1], bounds[2], original[3]) if demand.direction.axis is Axis.X
            else (original[0], bounds[1], original[2], bounds[3])), "tightened"


def outline(row):
    merged = unary_union([Polygon(value["polygon_mm"]) for value in row["cells"]])
    if merged.geom_type != "Polygon":
        raise ValueError("Expected one connected exterior")
    return Polygon(merged.exterior).buffer(.01)


def component_box(zone, component):
    lo, hi = component.longitudinal_interval_mm
    runs = pattern_runs(component.placement, component.axis_window_mm)
    left = min(run.first_axis_mm for run in runs)
    right = max(run.first_axis_mm + (run.bar_count - 1) * run.actual_step_mm for run in runs)
    return box(lo, left, hi, right) if zone.direction.axis is Axis.X else box(left, lo, right, hi)


def outside_ids(zones, border):
    return {f"{zone.id}:{component.component_index}" for zone in zones for component in zone.components
            if not border.covers(component_box(zone, component))}


def metrics(checks, zones):
    return {"zones": len(zones), "components": sum(len(zone.components) for zone in zones),
            "bars": sum(check.physical_bar_count or 0 for check in checks),
            "mass_kg": round(sum(check.additional_mass_kg or 0 for check in checks), 6),
            "positions": len(build_bar_schedule(composite_schedule_groups(zones))),
            "uncovered_cells": sum(check.uncovered_cell_count for check in checks),
            "uncovered_area_mm2": sum(check.uncovered_area_mm2 for check in checks),
            "geometry_and_patterns_valid": all(check.geometry_and_patterns_valid for check in checks),
            "coverage_passed": all(check.coverage_passed for check in checks)}


def raw_zone(demand, zone):
    built = build_composite_zone(demand, tuple(zone["demand_bbox_mm"]), zone["level_index"], zone["source_zone_id"],
        placement(zone), constraints=CONSTRAINTS, installed_lengths_mm=tuple(c["installed_length_mm"] for c in zone["components"]))
    components = []
    for component, raw in zip(built.components, zone["components"]):
        bbox = raw["bar_axis_bbox_mm"]
        interval = (bbox[0], bbox[2]) if demand.direction.axis is Axis.X else (bbox[1], bbox[3])
        components.append(replace(component, longitudinal_interval_mm=interval))
    return replace(built, components=tuple(components))


def inspect_packet(name, packet):
    if packet.get("source_stage") != SOURCE_STAGE or packet.get("units") != "mm":
        raise ValueError("Expected original source packet in mm")
    before, after, old_checks, new_checks, raw_bars, raw_mass = [], [], [], [], 0, 0.0
    old_outside, new_outside = set(), set()
    status = {"tightened": 0, "unchanged": 0, "no_additional_intersections": 0}
    for row in packet["directions"]:
        demand = demand_from(row)
        old = tuple(raw_zone(demand, zone) for zone in row["zone_drafts"])
        raw_bars += sum(component["bar_count"] for zone in row["zone_drafts"] for component in zone["components"])
        raw_mass += sum(component["mass_kg"] for zone in row["zone_drafts"] for component in zone["components"])
        new = []
        for zone in row["zone_drafts"]:
            bbox, reason = narrow_bbox(demand, zone)
            if reason == "tightened" and bbox == tuple(zone["demand_bbox_mm"]):
                reason = "unchanged"
            status[reason] += 1
            new.append(build_composite_zone(demand, bbox, zone["level_index"], zone["source_zone_id"], placement(zone), constraints=CONSTRAINTS))
        old_check = evaluate_composite_coverage(demand, old, policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY, constraints=CONSTRAINTS)
        new_check = evaluate_composite_coverage(demand, tuple(new), policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY, constraints=CONSTRAINTS)
        before.extend(old)
        after.extend(new)
        old_checks.append(old_check)
        new_checks.append(new_check)
        boundary = outline(row)
        old_outside |= {f"{demand.direction}:{value}" for value in outside_ids(old, boundary)}
        new_outside |= {f"{demand.direction}:{value}" for value in outside_ids(new, boundary)}
    before_metrics, after_metrics = metrics(old_checks, before), metrics(new_checks, after)
    if before_metrics["bars"] != raw_bars or abs(before_metrics["mass_kg"] - raw_mass) > 1e-6:
        raise ValueError("Rehydrated baseline differs from raw source components")
    return {"variant": name, "status": status, "raw_packet_matches_rehydrated": True,
            "before": before_metrics, "after": after_metrics,
            "new_component_frames_outside_exterior": len(new_outside), "outside_removed_count": len(old_outside-new_outside),
            "outside_introduced_count": len(new_outside-old_outside),
            "accepted_source_proxy": not new_outside-old_outside and after_metrics["geometry_and_patterns_valid"] and after_metrics["coverage_passed"]}


def packets(report):
    yield "variant-0", report["source_graphics"]
    for index, variant in enumerate(report.get("layout_variants", ())[1:], 1):
        if (nested := variant.get("report") or {}).get("source_graphics"):
            yield f"variant-{index}", nested["source_graphics"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = {"scope": "source logical-zone proxy; no physical/host/3D/stock-batch approval",
              "variants": [inspect_packet(name, packet) for name, packet in packets(json.loads(args.report.read_text()))]}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n")


if __name__ == "__main__":
    main()
