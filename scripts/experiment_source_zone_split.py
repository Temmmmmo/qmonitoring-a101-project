"""Read-only two-strip split probes for five saved v0 base-outside source zones."""
import argparse
import json
import runpy
from pathlib import Path

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from rebar.models import Axis, Rebar
from rebar.optimization.contracts.composite_coverage import MONOTONE_SINGLE_STO_COVERAGE_POLICY
from rebar.optimization.services.anchorage import FixedDiameterAnchoragePolicy
from rebar.optimization.services.axis_patterns import pattern_runs
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.services.composite_detailing import build_composite_zone


TIGHTENING = runpy.run_path(str(Path(__file__).with_name("experiment_source_zone_tightening.py")))
CONSTRAINTS = TIGHTENING["CONSTRAINTS"]
demand_from = TIGHTENING["demand_from"]
narrow_bbox = TIGHTENING["narrow_bbox"]
outline = TIGHTENING["outline"]
placement = TIGHTENING["placement"]
component_box = TIGHTENING["component_box"]


def frame(component, demand_bbox, axis, extension):
    x1, y1, x2, y2 = component["bar_axis_bbox_mm"]
    dx1, dy1, dx2, dy2 = demand_bbox
    return box(dx1-extension, y1, dx2+extension, y2) if axis is Axis.X else box(x1, dy1-extension, x2, dy2+extension)


def base_outside(row, zone, border):
    axis, policy = Axis(row["direction"]["axis"]), FixedDiameterAnchoragePolicy()
    return any(not border.covers(box(*component["bar_axis_bbox_mm"]))
        and not border.covers(frame(component, zone["demand_bbox_mm"], axis, policy.extension_each_end_mm(Rebar(1, component["diameter_mm"]))))
        and not border.covers(frame(component, zone["demand_bbox_mm"], axis, 0)) for component in zone["components"])


def axes_of(zone):
    return tuple(sorted(coordinate for component in zone.components for run in pattern_runs(component.placement, component.axis_window_mm)
                        for coordinate in (run.first_axis_mm + index*run.actual_step_mm for index in range(run.bar_count))))


def child_bbox(demand, parent, low, high):
    bounds = parent.demand_bbox
    strip = box(bounds[0], low, bounds[2], high) if demand.direction.axis is Axis.X else box(low, bounds[1], high, bounds[3])
    parts = [Polygon(cell.poly).intersection(strip) for cell in demand.cells if demand.level(cell.level_index).requires_extra
             and Polygon(cell.poly).intersection(strip).area > 1e-6]
    if not parts:
        return None
    x1, y1, x2, y2 = unary_union(parts).bounds
    # A strip owns its full transverse window; only its longitudinal core may tighten.
    return (x1, low, x2, high) if demand.direction.axis is Axis.X else (low, y1, high, y2)


def cuts(demand, parent):
    low, high = parent.components[0].axis_window_mm
    vertices = {point[1 if demand.direction.axis is Axis.X else 0] for cell in demand.cells for point in cell.poly if low < point[1 if demand.direction.axis is Axis.X else 0] < high}
    axes = axes_of(parent)
    return sorted(vertices | {(a+b)/2 for a, b in zip(axes, axes[1:]) if low < (a+b)/2 < high})


def split_probe(demand, zones, index, border, *, preserve_axes=True):
    parent = zones[index]
    initial_axes = axes_of(parent)
    result = {"zone_id": parent.id, "tested_cuts": 0, "local_fit_candidates": 0,
              "rejected": {"empty_children": 0, "detailing_or_minwidth": 0, "changed_or_duplicate_axes": 0,
                           "children_outside": 0, "coverage_rejected": 0}, "accepted": []}
    for cut in cuts(demand, parent):
        result["tested_cuts"] += 1
        low, high = parent.components[0].axis_window_mm
        first, second = child_bbox(demand, parent, low, cut), child_bbox(demand, parent, cut, high)
        if first is None or second is None:
            result["rejected"]["empty_children"] += 1
            continue
        try:
            children = tuple(build_composite_zone(demand, bbox, parent.level_index, f"{parent.id}#split-{n}", parent.placement,
                constraints=CONSTRAINTS) for n, bbox in enumerate((first, second), 1))
        except ValueError:
            result["rejected"]["detailing_or_minwidth"] += 1
            continue
        child_axes = tuple(sorted((*axes_of(children[0]), *axes_of(children[1]))))
        if len(set(child_axes)) != len(child_axes) or (preserve_axes and child_axes != initial_axes):
            result["rejected"]["changed_or_duplicate_axes"] += 1
            continue
        if any(not border.covers(component_box(zone, component)) for zone in children for component in zone.components):
            result["rejected"]["children_outside"] += 1
            continue
        result["local_fit_candidates"] += 1
        candidate = (*zones[:index], *children, *zones[index+1:])
        coverage = evaluate_composite_coverage(demand, candidate, policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY, constraints=CONSTRAINTS)
        if coverage.geometry_and_patterns_valid and coverage.coverage_passed:
            result["accepted"].append({"cut_mm": cut, "children": 2, "bars": coverage.physical_bar_count,
                "mass_kg": round(coverage.additional_mass_kg or 0, 6), "axis_count": len(child_axes), "positions": len({(c.rebar.diameter, c.installed_length_mm)
                for zone in candidate for c in zone.components})})
        else:
            result["rejected"]["coverage_rejected"] += 1
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-fewer-axes", action="store_true")
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    packet = report["source_graphics"]
    probes = []
    for row in packet["directions"]:
        demand = demand_from(row)
        border = outline(row)
        zones = tuple(build_composite_zone(demand, narrow_bbox(demand, zone)[0], zone["level_index"], zone["source_zone_id"],
            placement(zone), constraints=CONSTRAINTS) for zone in row["zone_drafts"])
        targets = [index for index, zone in enumerate(row["zone_drafts"]) if base_outside(row, zone, border)]
        for index in targets:
            if len(probes) == 5:
                break
            probes.append({"direction": row["direction"], **split_probe(demand, zones, index, border,
                preserve_axes=not args.allow_fewer_axes)})
        if len(probes) == 5:
            break
    result = {"scope": "v0 first five base-outside zones; two strips; source-only",
              "preserve_axes": not args.allow_fewer_axes, "probes": probes}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n")


if __name__ == "__main__":
    main()
