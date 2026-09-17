"""Read-only boundary-aware source-zone rebuild experiment; never exports a packet."""
import argparse
import json
import math
import runpy
import time
from pathlib import Path

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from rebar.models import Axis, Rebar
from rebar.optimization.contracts.composite_coverage import MONOTONE_SINGLE_STO_COVERAGE_POLICY
from rebar.optimization.services.anchorage import FixedDiameterAnchoragePolicy
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.detailing import typical_transverse_cell_size_mm
from rebar.optimization.services.edge_envelope_placement import place_edge_envelope


BASE = runpy.run_path(str(Path(__file__).with_name("experiment_source_zone_tightening.py")))
CONSTRAINTS = BASE["CONSTRAINTS"]
demand_from = BASE["demand_from"]
outline = BASE["outline"]
placement = BASE["placement"]
raw_zone = BASE["raw_zone"]
component_box = BASE["component_box"]
metrics = BASE["metrics"]


def _outside(zone, exterior):
    policy, result = FixedDiameterAnchoragePolicy(), []
    for component in zone.components:
        bounds = component_box(zone, component).bounds
        core = (zone.demand_bbox[0], zone.demand_bbox[2]) if zone.direction.axis is Axis.X else (zone.demand_bbox[1], zone.demand_bbox[3])
        total = 2 * policy.extension_each_end_mm(Rebar(1, component.rebar.diameter))
        placed = place_edge_envelope(exterior, bounds, zone.direction.axis, core, component.installed_length_mm, total)
        if placed.status != "pass":
            result.append(f"{zone.id}:{component.component_index}")
    return result


def _positive_bounds(demand, window):
    parts = [Polygon(cell.poly).intersection(window) for cell in demand.cells
             if demand.level(cell.level_index).requires_extra and Polygon(cell.poly).intersection(window).area > 1e-6]
    return unary_union(parts).bounds if parts else None


def _child_bbox(demand, parent, axis, cut, lower):
    x1, y1, x2, y2 = parent.demand_bbox
    if axis is Axis.X:
        strip = box(x1, y1, cut, y2) if lower else box(cut, y1, x2, y2)
    else:
        strip = box(x1, y1, x2, cut) if lower else box(x1, cut, x2, y2)
    bounds = _positive_bounds(demand, strip)
    if bounds is None:
        return None
    # A strip owns its transverse minimum-width window; only its longitudinal core
    # tightens to positive FE. This retains the agreed 2-KE constraint and phase.
    if demand.direction.axis is Axis.X:
        return bounds[0], strip.bounds[1], bounds[2], strip.bounds[3]
    return strip.bounds[0], bounds[1], strip.bounds[2], bounds[3]


def _cuts(demand, bbox):
    x1, y1, x2, y2 = bbox
    values = {Axis.X: {x1, x2}, Axis.Y: {y1, y2}}
    for cell in demand.cells:
        for x, y in cell.poly:
            if x1 < x < x2:
                values[Axis.X].add(x)
            if y1 < y < y2:
                values[Axis.Y].add(y)
    result = {}
    for axis, points in values.items():
        ordered = sorted(value for value in points if (x1 < value < x2 if axis is Axis.X else y1 < value < y2))
        # Deterministic bounded probe: endpoints and evenly spaced interior events.
        result[axis] = ordered if len(ordered) <= 8 else [ordered[0], ordered[-1], *ordered[::max(1, len(ordered)//6)][:6]]
    return result


def _rebuild(demand, parent, bboxes):
    return tuple(build_composite_zone(demand, bbox, parent.level_index, f"{parent.id}#r{index}", parent.placement,
                                      constraints=CONSTRAINTS)
                 for index, bbox in enumerate(bboxes, 1))


def transverse_relocations(demand, parent, *, cap=16):
    """Move, never shrink below 2-KE, the same-phase transverse selection window."""
    bounds = _positive_bounds(demand, box(*parent.demand_bbox))
    if bounds is None:
        return ()
    axis = demand.direction.axis
    lo, hi = (bounds[1], bounds[3]) if axis is Axis.X else (bounds[0], bounds[2])
    old_lo, old_hi = parent.components[0].axis_window_mm
    quantum = parent.components[0].placement.pattern.period_mm
    required = max(hi - lo, CONSTRAINTS.min_width_cells * typical_transverse_cell_size_mm(demand))
    width = max(quantum, math.ceil(required / quantum) * quantum)
    starts = {hi - width, lo, old_lo}
    for cell in demand.cells:
        for point in cell.poly:
            value = point[1 if axis is Axis.X else 0]
            if hi - width <= value <= lo:
                starts.add(value)
    proposals = []
    ordered = [hi - width, lo, old_lo, *sorted(starts - {hi - width, lo, old_lo})]
    for start in ordered[:cap]:
        end = start + width
        if not start <= lo + 1e-6 or hi > end + 1e-6:
            continue
        if axis is Axis.X:
            candidate = (bounds[0], start, bounds[2], end)
        else:
            candidate = (start, bounds[1], end, bounds[3])
        if candidate != parent.demand_bbox:
            proposals.append(candidate)
    return tuple(proposals)


def find_partitions(demand, parent, exterior, *, cap=64):
    """Deterministic 2..4 rectangle candidates, deriving every child from positive FE."""
    candidates, seen = [], set()
    pending = [(parent.demand_bbox,)]
    while pending and len(seen) < cap:
        boxes = pending.pop(0)
        key = tuple(sorted(boxes))
        if key in seen:
            continue
        seen.add(key)
        if len(boxes) > 1:
            try:
                rebuilt = _rebuild(demand, parent, boxes)
            except ValueError:
                rebuilt = ()
            if rebuilt and not any(_outside(zone, exterior) for zone in rebuilt):
                candidates.append(rebuilt)
        if len(boxes) == 4:
            continue
        for index, bbox in enumerate(boxes):
            probe = type("P", (), {"demand_bbox": bbox})
            for axis, cuts in _cuts(demand, bbox).items():
                for cut in cuts:
                    first, second = _child_bbox(demand, probe, axis, cut, True), _child_bbox(demand, probe, axis, cut, False)
                    if first and second:
                        pending.append((*boxes[:index], first, second, *boxes[index + 1:]))
    return candidates, bool(pending)


def inspect_row(row, *, deadline=None):
    demand, exterior = demand_from(row), outline(row)
    zones = tuple(raw_zone(demand, source) for source in row["zone_drafts"])
    before = evaluate_composite_coverage(demand, zones, policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY, constraints=CONSTRAINTS)
    old_outside = {item for zone in zones for item in _outside(zone, exterior)}
    active, accepted, probes, removed = list(zones), [], [], []
    unique = {cell.contributing_zone_ids[0] for cell in before.cells
              if len(cell.contributing_zone_ids) == 1 and cell.covered_area_mm2 > 1e-6}
    for zone in tuple(active):
        if deadline and time.monotonic() >= deadline:
            break
        if not _outside(zone, exterior) or zone.id in unique:
            continue
        candidate = tuple(value for value in active if value is not zone)
        check = evaluate_composite_coverage(demand, candidate, policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY, constraints=CONSTRAINTS)
        if check.geometry_and_patterns_valid and check.coverage_passed:
            removed.append(zone.id)
            active = list(candidate)
    # H1 first: relocations only. Recursive splitting is deliberately disabled by default.
    for index, zone in enumerate(tuple(active)):
        if deadline and time.monotonic() >= deadline:
            break
        if not _outside(zone, exterior):
            continue
        relocation = []
        for bbox in transverse_relocations(demand, zone):
            try:
                candidate_zone, = _rebuild(demand, zone, (bbox,))
                if not _outside(candidate_zone, exterior):
                    relocation.append((candidate_zone,))
            except ValueError:
                continue
        probes.append({"zone_id": zone.id, "transverse_candidates": len(relocation)})
        for children in relocation:
            candidate = (*active[:index], *children, *active[index + 1:])
            check = evaluate_composite_coverage(demand, candidate, policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY, constraints=CONSTRAINTS)
            new_outside = {item for item_zone in candidate for item in _outside(item_zone, exterior)}
            if check.geometry_and_patterns_valid and check.coverage_passed and not new_outside-old_outside and len(new_outside) < len(old_outside):
                accepted.append({"zone_id": zone.id, "children": len(children), "before_outside": len(old_outside),
                                 "after_outside": len(new_outside), "metrics": metrics((check,), candidate)})
                active = list(candidate)
                break
    after = evaluate_composite_coverage(demand, tuple(active), policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY, constraints=CONSTRAINTS)
    after_outside = {item for zone in active for item in _outside(zone, exterior)}
    return {"direction": row["direction"], "before": metrics((before,), zones), "after": metrics((after,), active),
            "outside_before": len(old_outside), "outside_after": len(after_outside), "removal_proven_needed": sorted(unique),
            "redundant_removals": removed, "probes": probes, "accepted": accepted, "_zones": tuple(active), "_check": after}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seconds", type=float, default=30)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    packet = report["source_graphics"]
    deadline = time.monotonic() + args.seconds
    rows = [inspect_row(row, deadline=deadline) for row in packet["directions"]]
    before_zones = tuple(raw_zone(demand_from(row), source) for row in packet["directions"] for source in row["zone_drafts"])
    after_zones = tuple(zone for row in rows for zone in row.pop("_zones"))
    for row in rows:
        row.pop("_check")
    from rebar.optimization.services.bar_schedule import build_bar_schedule, composite_schedule_groups
    from rebar.optimization.services.stock_cutting import check_stock_cutting
    before_schedule = build_bar_schedule(composite_schedule_groups(before_zones))
    after_schedule = build_bar_schedule(composite_schedule_groups(after_zones))
    result = {"scope": "v0 read-only source-zone rebuilding; no GA, host, 3D or stock-batch approval",
              "coverage_policy": MONOTONE_SINGLE_STO_COVERAGE_POLICY, "rows": rows,
              "whole": {"before": {"zones": len(before_zones), "positions": len(before_schedule),
                          "bars": sum(row["before"]["bars"] for row in rows), "mass_kg": round(sum(row["before"]["mass_kg"] for row in rows), 6)},
                        "after": {"zones": len(after_zones), "positions": len(after_schedule),
                          "bars": sum(row["after"]["bars"] for row in rows), "mass_kg": round(sum(row["after"]["mass_kg"] for row in rows), 6)},
                        "stock_before": check_stock_cutting(before_schedule, time_limit_s=1),
                        "stock_after": check_stock_cutting(after_schedule, time_limit_s=1)},
              "budget_truncated": time.monotonic() >= deadline}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n")


if __name__ == "__main__":
    main()
