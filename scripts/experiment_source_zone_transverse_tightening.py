"""Read-only v0 single-zone tightening in both axes, guarded by common coverage."""
import argparse
import json
import runpy
from pathlib import Path

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from rebar.models import Axis
from rebar.optimization.contracts.composite_coverage import MONOTONE_SINGLE_STO_COVERAGE_POLICY
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.detailing import typical_transverse_cell_size_mm


BASE = runpy.run_path(str(Path(__file__).with_name("experiment_source_zone_tightening.py")))
CONSTRAINTS = BASE["CONSTRAINTS"]
demand_from = BASE["demand_from"]
metrics = BASE["metrics"]
outline = BASE["outline"]
outside_ids = BASE["outside_ids"]
placement = BASE["placement"]
raw_zone = BASE["raw_zone"]


def both_axes_bbox(demand, zone):
    original, window = tuple(zone["demand_bbox_mm"]), box(*zone["demand_bbox_mm"])
    parts = [Polygon(cell.poly).intersection(window) for cell in demand.cells if demand.level(cell.level_index).requires_extra
             and Polygon(cell.poly).intersection(window).area > 1e-6]
    if not parts:
        return original, "no_additional_intersections"
    x1, y1, x2, y2 = unary_union(parts).bounds
    minimum = CONSTRAINTS.min_width_cells * typical_transverse_cell_size_mm(demand)
    if demand.direction.axis is Axis.X and y2-y1 < minimum:
        y1, y2 = original[1], original[3]
    if demand.direction.axis is Axis.Y and x2-x1 < minimum:
        x1, x2 = original[0], original[2]
    return (x1, y1, x2, y2), "tightened" if (x1, y1, x2, y2) != original else "unchanged"


def candidate_zone(demand, source):
    bbox, status = both_axes_bbox(demand, source)
    return build_composite_zone(demand, bbox, source["level_index"], source["source_zone_id"], placement(source),
                                constraints=CONSTRAINTS), status


def check(demand, zones):
    return evaluate_composite_coverage(demand, zones, policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY, constraints=CONSTRAINTS)


def inspect(packet):
    result = {"directions": [], "mode": "all-candidates"}
    for row in packet["directions"]:
        demand, border = demand_from(row), outline(row)
        old = tuple(raw_zone(demand, value) for value in row["zone_drafts"])
        proposals = [candidate_zone(demand, value) for value in row["zone_drafts"]]
        new = tuple(value for value, _ in proposals)
        old_check, new_check = check(demand, old), check(demand, new)
        if not (new_check.geometry_and_patterns_valid and new_check.coverage_passed):
            result["mode"] = "locally-fitting-greedy"
            new = list(old)
            for index, (proposal, status) in enumerate(proposals):
                if status != "tightened" or outside_ids((proposal,), border):
                    continue
                trial = (*new[:index], proposal, *new[index+1:])
                trial_check = check(demand, trial)
                if trial_check.geometry_and_patterns_valid and trial_check.coverage_passed:
                    new = list(trial)
            new = tuple(new)
            new_check = check(demand, new)
        old_outside, new_outside = outside_ids(old, border), outside_ids(new, border)
        result["directions"].append({"direction": row["direction"],
            "statuses": {kind: sum(status == kind for _, status in proposals) for kind in ("tightened", "unchanged", "no_additional_intersections")},
            "before": metrics((old_check,), old), "after": metrics((new_check,), new),
            "outside_removed": len(old_outside-new_outside), "outside_introduced": len(new_outside-old_outside),
            "accepted": new_check.geometry_and_patterns_valid and new_check.coverage_passed and not new_outside-old_outside})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    result = {"scope": "v0 only; source logical zones; no host/3D/stock-batch approval", **inspect(report["source_graphics"])}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n")


if __name__ == "__main__":
    main()
