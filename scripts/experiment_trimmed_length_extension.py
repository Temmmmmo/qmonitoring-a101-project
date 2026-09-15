"""Bounded whole-segment catalogue extension, research only; no FE clipping."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, replace
import math
from pathlib import Path
import time

from audit_trimmed_demand import ROOT, _read, load_trimmed_inputs
from rebar.application.assistant_inputs import source_record, verify_source_records
from rebar.application.physical_layout_recovery import _bytes
from rebar.models import Axis
from rebar.optimization.contracts.shaped_physical import Line3D
from rebar.optimization.services.cutting import PLATE_11700_CUT_LENGTHS_MM
from rebar.optimization.services.shaped_collisions import _straight_pair, check_shaped_collisions
from rebar.optimization.services.shaped_geometry import (
    check_shaped_host, main_horizontal_interval_mm, shaped_batch_metrics,
    shaped_cut_length_mm, shaped_mass_kg, shaped_position_key,
)
from rebar.optimization.services.shaped_global_coverage import _strict_coverage
from rebar.optimization.services.trimmed_length_cleanup import (
    _all_offers, _covered_regions, _demand_regions, _lost_regions, _stock,
    _validate_inputs, check_trimmed_length_cleanup,
)
from rebar.optimization.services.tz_boundary_trim import straight_outer_intersections
from rebar.reporting.serialization import to_jsonable


def segment(bar, low, high):
    along = 0 if bar.direction.axis is Axis.X else 1
    a, b = list(bar.segments[0].start_mm), list(bar.segments[0].end_mm)
    a[along], b[along] = low, high
    return replace(bar, segments=(Line3D(tuple(a), tuple(b)),), selected_cut_length_mm=high-low)


def separate_from_every_other(candidate, others):
    """No accepted modified bar participates in ANY old, new or uncertain pair."""
    a, b = candidate.segments[0].start_mm, candidate.segments[0].end_mm
    for other in others:
        if (candidate.direction, candidate.id) == (other.direction, other.id):
            continue
        c, d = other.segments[0].start_mm, other.segments[0].end_mm
        radius = (candidate.diameter_mm+other.diameter_mm)/2
        # Exact-coordinate AABB separation avoids quadratic rational work.
        if any(max(min(a[i], b[i]), min(c[i], d[i]))-
               min(max(a[i], b[i]), max(c[i], d[i])) > radius for i in range(3)):
            continue
        result = _straight_pair(candidate, other)
        if result is None or result["status"] != "separated":
            return False
    return True


def run(args):
    if args.output.exists() or not 0 < args.time_limit_s <= 60:
        raise ValueError("New output and positive search budget <=60s required")
    if not math.isfinite(args.maximum_mass_kg) or args.maximum_mass_kg <= 0:
        raise ValueError("Finite positive mass cap required")
    records = [source_record(ROOT/path, role="length-extension-experiment-code") for path in (
        "scripts/experiment_trimmed_length_extension.py",
        "src/rebar/optimization/services/trimmed_length_cleanup.py",
        "src/rebar/optimization/services/cutting.py",
    )]
    cleanup, _ = _read(args.cleanup_report)
    records.append(source_record(args.cleanup_report, role="exact-cleanup-input"))
    inputs = load_trimmed_inputs(report_dir=args.report_dir, snapshot=args.snapshot,
        working_host_report=args.working_host_report, candidate_id=args.candidate_id,
        stock_time_limit_s=args.stock_time_limit_s)
    wanted = {(str(v["direction"]["layer"])+"-"+str(v["direction"]["axis"]), v["id"])
              for v in cleanup["bars_after"]}
    before = tuple(b for b in inputs.bars if (str(b.direction), b.id) in wanted)
    if _bytes(to_jsonable([asdict(b) for b in before])) != _bytes(cleanup["bars_after"]):
        raise ValueError("Cleanup bars must be exact unchanged subset of freshly replayed trim")
    cleanup_check = check_trimmed_length_cleanup(inputs.bars, before, inputs.lanes,
        inputs.problem, inputs.host, stock_time_limit_s=args.stock_time_limit_s)
    if not cleanup_check["accepted_nonregression"]:
        raise ValueError("Fresh cleanup check rejected source subset")
    sources, domain = _validate_inputs(before, inputs.lanes, inputs.problem, inputs.host)
    metrics_before = shaped_batch_metrics(before)
    if metrics_before["mass_kg"] > args.maximum_mass_kg:
        raise ValueError("Input already exceeds explicit mass cap")
    positions = Counter(shaped_position_key(b) for b in before)
    current, mass = list(before), metrics_before["mass_kg"]
    candidates = []
    for i, bar in enumerate(before):
        length = shaped_cut_length_mm(bar)
        if length in PLATE_11700_CUT_LENGTHS_MM:
            continue
        for target in [v for v in PLATE_11700_CUT_LENGTHS_MM if v > length][:2]:
            increment = shaped_mass_kg(segment(bar, 0, target))-shaped_mass_kg(bar)
            new_key = shaped_position_key(segment(bar, 0, target))
            benefit = int(positions[shaped_position_key(bar)] == 1)-int(new_key not in positions)
            candidates.append((-benefit, increment, i, target))
    candidates.sort()
    started, reasons, actions, considered, changed = time.monotonic(), Counter(), [], 0, set()
    for _, increment, index, target in candidates:
        if time.monotonic()-started >= args.time_limit_s:
            break
        if index in changed:
            continue
        bar = current[index]
        considered += 1
        if mass+increment > args.maximum_mass_kg:
            reasons["mass_cap"] += 1
            continue
        oldlo, oldhi = main_horizontal_interval_mm(bar)
        along = 0 if bar.direction.axis is Axis.X else 1
        low = min(s.footprint.bounds[along] for s in domain.sections)
        high = max(s.footprint.bounds[along+2] for s in domain.sections)
        windows = straight_outer_intersections(segment(bar, low, high), inputs.host, respect_openings=True)
        feasible = [(max(a, oldhi-target), min(oldlo, b-target)) for a, b in windows
                    if a <= oldlo and b >= oldhi and max(a, oldhi-target) <= min(oldlo, b-target)]
        if not feasible:
            reasons["no_whole_host_interval"] += 1
            continue
        accepted = None
        for lo, hi in feasible:
            starts = (max(lo, min(hi, (oldlo+oldhi-target)/2)), lo, hi, math.ceil(lo), math.floor(hi))
            for start in dict.fromkeys(starts):
                if not lo <= start <= hi:
                    continue
                proposed = segment(bar, start, start+target)
                a, b = main_horizontal_interval_mm(proposed)
                if not a <= oldlo < oldhi <= b or shaped_cut_length_mm(proposed) != target:
                    reasons["nonexact_length_or_not_superset"] += 1
                    continue
                if check_shaped_host(proposed, domain)["status"] != "pass":
                    reasons["body_host"] += 1
                    continue
                if not separate_from_every_other(proposed, current):
                    reasons["collision_or_uncertainty"] += 1
                    continue
                accepted = proposed
                break
            if accepted is not None:
                break
        if accepted is None:
            continue
        oldkey, newkey = shaped_position_key(bar), shaped_position_key(accepted)
        if len(positions)+int(newkey not in positions)-int(positions[oldkey] == 1) > len(positions):
            reasons["would_increase_positions"] += 1
            continue
        current[index] = accepted
        positions[oldkey] -= 1
        if not positions[oldkey]:
            del positions[oldkey]
        positions[newkey] += 1
        changed.add(index)
        mass += shaped_mass_kg(accepted)-shaped_mass_kg(bar)
        actions.append({"direction": str(bar.direction), "bar_id": bar.id,
            "before_interval_mm": (oldlo, oldhi), "after_interval_mm": main_horizontal_interval_mm(accepted),
            "before_length_mm": shaped_cut_length_mm(bar), "after_length_mm": target})
    search_time = time.monotonic()-started
    after = tuple(current)
    old_offers, new_offers = _all_offers(before, sources), _all_offers(after, sources)
    demanded = _demand_regions(inputs.problem)
    losses = _lost_regions(_covered_regions(old_offers, demanded), _covered_regions(new_offers, demanded))
    if losses or any(check_shaped_host(b, domain)["status"] != "pass" for b in after):
        raise ValueError("Independent exact full coverage/body non-regression failed")
    old_collisions, new_collisions = check_shaped_collisions(before), check_shaped_collisions(after)
    for name in ("proven_collision_pairs", "uncertain_pairs"):
        if _bytes(old_collisions[name]) != _bytes(new_collisions[name]):
            raise ValueError("Full collision pairs changed; accepted modifications must be separated")
    coverage_before = {p: _strict_coverage(inputs.problem, offers) for p, offers in old_offers.items()}
    coverage_after = {p: _strict_coverage(inputs.problem, offers) for p, offers in new_offers.items()}
    metrics_after = shaped_batch_metrics(after)
    if metrics_after["mass_kg"] > args.maximum_mass_kg or metrics_after["position_count"] > metrics_before["position_count"]:
        raise ValueError("Independent full metric cap failed")
    stock = _stock(after, args.stock_time_limit_s)
    records = [*inputs.source_files, *records]
    verify_source_records(records)
    result = {"schema_version": "trimmed-length-extension-experiment/v1", "units": "mm",
        "source_files": records, "case_id": inputs.problem.case_id, "physical_bars": to_jsonable([asdict(b) for b in after]),
        "metrics_before": metrics_before, "metrics_after": metrics_after,
        "coverage_before": coverage_before, "coverage_after": coverage_after, "collisions": new_collisions,
        "stock_cutting": stock, "actions": actions, "rejected": dict(reasons),
        "search": {"time_s": search_time, "maximum_time_s": args.time_limit_s, "catalogue_options": 2,
            "generated": len(candidates), "considered": considered, "time_limit_reached": search_time >= args.time_limit_s},
        "material_failures": 0, "previously_covered_geometry_lost": losses,
        "maximum_mass_kg": args.maximum_mass_kg, "concrete_cover_included": False,
        "exact_old_segment_containment": True, "coordinates_rounded": False, "positive_area_loss_tolerance_mm2": 0,
        "source_demand_removed": False, "source_demand_transferred": False,
        "placement_eligible": False, "engineering_approval": False,
        "not_checked": ["existing_Revit_reinforcement", "engineering_anchorage_capacity", "Revit_readback"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(_bytes(result))
    print({"before": metrics_before, "after": metrics_after, "actions": len(actions), "rejected": dict(reasons),
        "coverage_before": {k: v["uncovered_cell_count"] for k, v in coverage_before.items()},
        "coverage_after": {k: v["uncovered_cell_count"] for k, v in coverage_after.items()},
        "pairs": new_collisions["proven_collision_pair_count"], "stock": stock["status"], "search": result["search"]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("report-dir", "snapshot", "working-host-report", "cleanup-report", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--candidate-id", default="plate:52")
    parser.add_argument("--maximum-mass-kg", type=float, default=3310.574)
    parser.add_argument("--time-limit-s", type=float, default=60)
    parser.add_argument("--stock-time-limit-s", type=float, default=10)
    run(parser.parse_args())
