"""Read-only v0 edge-assumption probe: 80d total may move along a source bar."""
import argparse
import json
import runpy
from pathlib import Path

from shapely.affinity import translate
from shapely.geometry import box

from rebar.models import Axis, Rebar
from rebar.optimization.services.anchorage import FixedDiameterAnchoragePolicy


BASE = runpy.run_path(str(Path(__file__).with_name("experiment_source_zone_tightening.py")))
outline = BASE["outline"]
SOURCE_STAGE = BASE["SOURCE_STAGE"]


def along(box_values, axis):
    return (box_values[0], box_values[2]) if axis is Axis.X else (box_values[1], box_values[3])


def shifted_frame(box_values, axis, shift):
    return translate(box(*box_values), xoff=shift if axis is Axis.X else 0, yoff=shift if axis is Axis.Y else 0)


def choose_shift(border, box_values, axis, core, length, target):
    lo, hi = along(box_values, axis)
    start, end = core
    if length < end-start+2*target-1e-6:
        return None
    lower, upper = end-hi, start-lo  # contains core; unlike the old 40d-at-each-end window
    if lower > upper + 1e-6:
        return None
    candidates = {lower, upper, min(max(0., lower), upper)}
    candidates.update(lower + (upper-lower)*index/100 for index in range(101))
    feasible = [value for value in candidates if abs((hi-lo)-length) <= 1e-6 and border.covers(shifted_frame(box_values, axis, value))]
    return min(feasible, key=abs) if feasible else None


def inspect(packet):
    policy = FixedDiameterAnchoragePolicy()
    result = {"components": 0, "sample_budget_per_component": 101, "old_outside": 0, "edge_fixed": 0,
              "introduced_outside": 0, "outside_without_sampled_fit": 0, "rejected_invalid_total_80d": 0,
              "old_per_end_40d_fail": 0, "accepted_fit_component_count": 0,
              "accepted_fit_per_end_40d_fail": 0, "accepted_fit_total_80d_fail": 0,
              "accepted_fit_core_containment_fail": 0,
              "remaining_ids_by_direction": {}, "examples": []}
    for row in packet["directions"]:
        border, axis = outline(row), Axis(row["direction"]["axis"])
        for zone in row["zone_drafts"]:
            core = along(zone["demand_bbox_mm"], axis)
            for component in zone["components"]:
                bbox, length = component["bar_axis_bbox_mm"], component["installed_length_mm"]
                lo, hi = along(bbox, axis)
                target = policy.extension_each_end_mm(Rebar(1, component["diameter_mm"]))
                left, right = core[0]-lo, hi-core[1]
                result["components"] += 1
                result["old_per_end_40d_fail"] += left < target-1e-6 or right < target-1e-6
                old_outside = not border.covers(box(*bbox))
                result["old_outside"] += old_outside
                identifier = f"{zone['source_zone_id']}:{component['component_index']}"
                if length < core[1]-core[0]+2*target-1e-6:
                    result["rejected_invalid_total_80d"] += 1
                    if old_outside:
                        result["remaining_ids_by_direction"].setdefault(str(row["direction"]), []).append(identifier)
                    continue
                shift = choose_shift(border, bbox, axis, core, length, target)
                if shift is None:
                    result["outside_without_sampled_fit"] += old_outside
                    if old_outside:
                        result["remaining_ids_by_direction"].setdefault(str(row["direction"]), []).append(identifier)
                    continue
                new_outside = not border.covers(shifted_frame(bbox, axis, shift))
                result["edge_fixed"] += old_outside and not new_outside
                result["introduced_outside"] += not old_outside and new_outside
                new_lo, new_hi = lo+shift, hi+shift
                new_left, new_right = core[0]-new_lo, new_hi-core[1]
                result["accepted_fit_component_count"] += 1
                result["accepted_fit_per_end_40d_fail"] += new_left < target-1e-6 or new_right < target-1e-6
                result["accepted_fit_total_80d_fail"] += new_left+new_right < 2*target-1e-6
                result["accepted_fit_core_containment_fail"] += new_left < -1e-6 or new_right < -1e-6
                if old_outside and new_outside:
                    result["remaining_ids_by_direction"].setdefault(str(row["direction"]), []).append(identifier)
                if old_outside and not new_outside and len(result["examples"]) < 5:
                    result["examples"].append({"zone_id": zone["source_zone_id"], "direction": row["direction"],
                        "diameter_mm": component["diameter_mm"], "shift_mm": round(shift, 3),
                        "old_interval_mm": [lo, hi], "new_interval_mm": [round(new_lo, 3), round(new_hi, 3)],
                        "core_interval_mm": list(core), "left_mm": round(new_left, 3), "right_mm": round(new_right, 3),
                        "total_extension_mm": round(new_left+new_right, 3), "target_each_end_mm": target, "length_mm": length})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    if report["source_graphics"].get("source_stage") != SOURCE_STAGE or report["source_graphics"].get("units") != "mm":
        raise ValueError("Expected original source packet in mm")
    result = {"scope": "v0 only; user-approved edge assumption: total 80d, not 40d each end; no host/3D/stock approval",
              **inspect(report["source_graphics"])}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n")


if __name__ == "__main__":
    main()
