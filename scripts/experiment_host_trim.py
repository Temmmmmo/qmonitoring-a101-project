"""Bounded read-only research: can shorter catalogue bars remove stock-tail host failures?

No anchorage is shortened and no original demand/source owner is removed. The
candidate bars are NOT a Revit packet. A stock failure remains a stock failure.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from correct_small_openings import _code_digest, _load_recovery, _read
from rebar.application.assistant_inputs import source_record, verify_source_records
from rebar.application.layout_snapshot import load_layout_snapshot
from rebar.application.opening_relocation import source_service_lanes
from rebar.application.physical_layout_recovery import _bytes, _raw
from rebar.application.working_host import load_working_host_json
from rebar.application.working_solid_host import MAX_WORKING_REPORT_BYTES, inspect_working_solid
from rebar.optimization.contracts.physical import PhysicalBar
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS
from rebar.optimization.services.bar_schedule import BarScheduleGroup, build_bar_schedule
from rebar.optimization.services.cutting import PLATE_11700_CUT_LENGTHS_MM
from rebar.optimization.services.opening_relocation import (
    check_relocation, collision, collision_pairs, contained, coverage, lane_map,
    material_and_holes, source_geometry_ok,
)
from rebar.optimization.services.physical_host_fit import _host_intervals, _source_window
from rebar.optimization.services.stock_cutting import check_stock_cutting

NOT_CHECKED = (
    "actual_Z_and_cross_direction_3D_collisions",
    "existing_revit_reinforcement",
    "Revit_readback",
    "engineering_acceptance",
)


def shorter_options(bar, sources, material, cover, *, subset_only):
    """Exact finite catalogue starts; all full40d source intervals are retained."""
    _source_window(bar, sources)
    required_lo = min(p.required_interval_mm[0] for p in sources) - 40 * bar.diameter_mm
    required_hi = max(p.required_interval_mm[1] for p in sources) + 40 * bar.diameter_mm
    options = []
    for length in PLATE_11700_CUT_LENGTHS_MM:
        if not required_hi-required_lo-1e-6 <= length < bar.installed_length_mm-1e-6:
            continue
        source_window = (required_hi-length, required_lo)
        start = min(source_window[1], max(source_window[0], bar.installed_interval_mm[0]))
        proposal = replace(bar, installed_interval_mm=(start, start+length))
        _source_window(proposal, sources)
        for a, b in _host_intervals(proposal, material, cover, 4096):
            lo, hi = max(a, source_window[0]), min(b, source_window[1])
            if subset_only:
                lo, hi = max(lo, bar.installed_interval_mm[0]), min(hi, bar.installed_interval_mm[1]-length)
            if lo > hi:
                continue
            start = min(hi, max(lo, bar.installed_interval_mm[0]))
            candidate = replace(proposal, installed_interval_mm=(start, start+length))
            _source_window(candidate, sources)
            if not contained(candidate, material, cover):
                raise ValueError("Interval candidate failed independent body containment")
            options.append(candidate)
    return tuple(options)


def inspect_trims(bars, lanes, problem, host):
    """All inventory remains; contained-subinterval shortening cannot add body pairs."""
    material, _, _ = material_and_holes(host)
    sources = lane_map(lanes)
    before = coverage(problem, bars, sources)
    if before["status"] != "pass" or any(not source_geometry_ok(b, sources) for b in bars):
        raise ValueError("Complete original coverage and every full40d owner required")
    proposed, entries, alternatives = [], [], {}
    for bar in bars:
        if contained(bar, material, host.side_cover_mm):
            proposed.append(bar)
            continue
        parents = tuple(sources[(bar.direction, key)].source for key in bar.source_bar_ids)
        subset = shorter_options(bar, parents, material, host.side_cover_mm, subset_only=True)
        free = shorter_options(bar, parents, material, host.side_cover_mm, subset_only=False)
        changed = min(subset, key=lambda b: (b.installed_length_mm, b.installed_interval_mm)) if subset else bar
        proposed.append(changed)
        alternatives[(str(bar.direction), bar.id)] = subset
        entries.append({"direction": str(bar.direction), "bar_id": bar.id,
            "diameter_mm": bar.diameter_mm, "original_length_mm": bar.installed_length_mm,
            "minimum_full40d_union_length_mm": max(p.required_interval_mm[1] for p in parents)
                - min(p.required_interval_mm[0] for p in parents) + 80 * bar.diameter_mm,
            "geometrically_possible_lengths_mm": sorted({v.installed_length_mm for v in free}),
            "contained_subinterval_lengths_mm": sorted({v.installed_length_mm for v in subset}),
            "exact_divisor_11700_lengths_mm": sorted({v.installed_length_mm for v in subset
                if 11700 % round(v.installed_length_mm) == 0}),
            "chosen_interval_mm": list(changed.installed_interval_mm),
            "chosen_length_mm": changed.installed_length_mm, "changed": changed != bar})
    proposed = tuple(proposed)
    old_pairs, new_pairs = collision_pairs(bars), collision_pairs(proposed)
    if new_pairs-old_pairs:
        raise ValueError("Unexpected new body pair after subinterval shortening")
    after = coverage(problem, proposed, sources)
    if after["status"] != "pass" or any(not source_geometry_ok(b, sources) for b in proposed):
        raise ValueError("Trimming lost original coverage or full40d")
    schedule = build_bar_schedule(tuple(BarScheduleGroup(f"{b.direction}/{b.id}", b.diameter_mm,
        b.installed_length_mm, 1, b.steel_class) for b in proposed))
    old_schedule = build_bar_schedule(tuple(BarScheduleGroup(f"{b.direction}/{b.id}", b.diameter_mm,
        b.installed_length_mm, 1, b.steel_class) for b in bars))
    stock = check_stock_cutting(schedule, time_limit_s=30)
    report = {"schema_version": "physical-host-stock-trim-experiment/v1", "placement_eligible": False,
        "scope": "optimistic stock-tail geometric repair; unbalanced full batch is not an accepted plan",
        "not_checked": list(NOT_CHECKED),
        "body_collision_scope": "same_direction_only_no_Z_or_cross_direction_check",
        "host_blocked_before": len(entries), "geometrically_shortenable": sum(bool(r["geometrically_possible_lengths_mm"]) for r in entries),
        "contained_subinterval_shortenable": sum(r["changed"] for r in entries),
        "exact_divisor_shortenable": sum(bool(r["exact_divisor_11700_lengths_mm"]) for r in entries),
        "host_blocked_after_shortest_subinterval": sum(not contained(b, material, host.side_cover_mm) for b in proposed),
        "physical_bar_count_before": len(bars), "physical_bar_count_after": len(proposed),
        "mass_before_kg": sum(p.total_mass_kg for p in old_schedule),
        "mass_after_kg": sum(p.total_mass_kg for p in schedule),
        "same_direction_body_pairs_before": len(old_pairs), "same_direction_body_pairs_after": len(new_pairs),
        "new_body_pairs": len(new_pairs-old_pairs), "full_new40d_preserved": True,
        "source_coverage": after, "stock_cutting": stock, "bars": entries,
        "raw_bars_by_direction": _raw(proposed), "structural_placement_supported": False}
    return report, alternatives


def _contained_length_positions(bar, length, parents, material, cover, others):
    """Exact host/source intervals and body-contact event positions, not a grid."""
    required_lo = min(p.required_interval_mm[0] for p in parents) - 40 * bar.diameter_mm
    required_hi = max(p.required_interval_mm[1] for p in parents) + 40 * bar.diameter_mm
    window = (required_hi-length, required_lo)
    if window[0] > window[1]:
        return ()
    start = min(window[1], max(window[0], bar.installed_interval_mm[0]))
    candidate = replace(bar, installed_interval_mm=(start, start+length))
    _source_window(candidate, parents)
    neighbours = [b for b in others if b.direction == bar.direction
        and abs(b.transverse_axis_mm-bar.transverse_axis_mm) < (b.diameter_mm+bar.diameter_mm)/2 - 1e-6]
    result = []
    for a, b in _host_intervals(candidate, material, cover, 4096):
        lo, hi = max(a, window[0]), min(b, window[1])
        if lo > hi:
            continue
        events = {lo, hi, min(hi, max(lo, bar.installed_interval_mm[0]))}
        for other in neighbours:
            events.update((other.installed_interval_mm[0]-length, other.installed_interval_mm[1]))
        for start in sorted((p for p in events if lo <= p <= hi),
                            key=lambda p: (abs(p-bar.installed_interval_mm[0]), p)):
            item = replace(candidate, installed_interval_mm=(start, start+length))
            if not any(collision(item, other) for other in neighbours) and contained(item, material, cover):
                _source_window(item, parents)
                result.append(item)
    return tuple(result)


def inspect_stock_exchanges(bars, lanes, problem, host, alternatives, *, maximum_pairs=902):
    """Swap two existing same-material lengths; no extra stock, steel or short40d."""
    material, _, _ = material_and_holes(host)
    sources = lane_map(lanes)
    checked, geometry_donors, accepted = 0, 0, []
    old_pairs = collision_pairs(bars)
    old_stock = Counter((b.steel_class, b.diameter_mm, round(b.installed_length_mm, 6)) for b in bars)
    for index, first in enumerate(bars):
        for shortened in alternatives.get((str(first.direction), first.id), ()):
            if any(collision(shortened, other) for j, other in enumerate(bars) if j != index):
                continue
            for donor_index, donor in enumerate(bars):
                if (donor_index == index or donor.steel_class != first.steel_class
                        or donor.diameter_mm != first.diameter_mm
                        or abs(donor.installed_length_mm-shortened.installed_length_mm) > 1e-6):
                    continue
                checked += 1
                if checked > maximum_pairs:
                    return {"status": "not_checked_pair_limit", "pair_limit": maximum_pairs,
                        "pairs_checked": checked-1, "placement_eligible": False, "accepted": accepted}
                parents = tuple(sources[(donor.direction, key)].source for key in donor.source_bar_ids)
                # The sole already-relocated transverse bar stays untouched here;
                # the old-axis fitter must never receive rewritten source certificates.
                if any(p.transverse_axis_mm != donor.transverse_axis_mm for p in parents):
                    continue
                others = tuple(shortened if j == index else b for j, b in enumerate(bars) if j != donor_index)
                positions = _contained_length_positions(donor, first.installed_length_mm, parents,
                    material, host.side_cover_mm, others)
                if not positions:
                    continue
                geometry_donors += 1
                extended = positions[0]
                full = tuple(shortened if j == index else extended if j == donor_index else b
                             for j, b in enumerate(bars))
                after_pairs = collision_pairs(full)
                changed_ids = {(str(first.direction), first.id), (str(donor.direction), donor.id)}
                if after_pairs-old_pairs or any((p[0], p[1]) in changed_ids or (p[0], p[2]) in changed_ids
                                              for p in after_pairs):
                    raise ValueError("Changed bar remains in a body collision after stock exchange")
                if Counter((b.steel_class, b.diameter_mm, round(b.installed_length_mm, 6)) for b in full) != old_stock:
                    raise ValueError("Exchange changed installed cutting inventory")
                checked_coverage = coverage(problem, full, sources)
                if checked_coverage["status"] != "pass" or any(not source_geometry_ok(b, sources) for b in full):
                    raise ValueError("Exchange changed source coverage, owners or full40d")
                schedule = build_bar_schedule(tuple(BarScheduleGroup(f"{b.direction}/{b.id}", b.diameter_mm,
                    b.installed_length_mm, 1, b.steel_class) for b in full))
                stock = check_stock_cutting(schedule, time_limit_s=30)
                blocked_after = sum(not contained(b, material, host.side_cover_mm) for b in full)
                accepted.append({"shortened": {"direction": str(first.direction), "bar_id": first.id,
                    "before_mm": list(first.installed_interval_mm), "after_mm": list(shortened.installed_interval_mm)},
                    "extended": {"direction": str(donor.direction), "bar_id": donor.id,
                    "before_mm": list(donor.installed_interval_mm), "after_mm": list(extended.installed_interval_mm)},
                    "host_blocked_after": blocked_after,
                    "full_layout_status": "blocked_host" if blocked_after else "not_engineering_accepted",
                    "not_checked": list(NOT_CHECKED),
                    "body_collision_scope": "same_direction_only_no_Z_or_cross_direction_check",
                    "physical_bar_count": len(full), "mass_kg": sum(p.total_mass_kg for p in schedule),
                    "complete_material_length_count_inventory_unchanged": True,
                    "source_axes_owners_and_full40d_preserved": True,
                    "changed_bars_with_body_collisions": 0,
                    "same_direction_body_pairs": len(after_pairs), "new_body_pairs": len(after_pairs-old_pairs),
                    "stock_cutting": stock, "source_coverage": checked_coverage,
                    "raw_bars_by_direction": _raw(full)})
                # One verified full witness is enough for feasibility, not optimality.
                if stock["status"] == "pass":
                    return {"status": "full_inventory_exchange_found", "pairs_checked": checked,
                        "geometrically_compatible_donor_count": geometry_donors,
                        "placement_eligible": False, "accepted": accepted,
                        "scope": "first independently checked whole-party witness; not a best or fully hosted layout"}
    return {"status": "no_exchange_in_finite_length_pairs", "pairs_checked": checked,
        "geometrically_compatible_donor_count": geometry_donors,
        "placement_eligible": False, "accepted": accepted}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-dir", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--working-host-report", type=Path, required=True)
    parser.add_argument("--relocation-draft", type=Path, required=True)
    parser.add_argument("--relocation-review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise ValueError("Existing output must not be overwritten")
        code_before = _code_digest()
        script_record = source_record(Path(__file__), role="experiment-code")
        loaded = load_layout_snapshot(args.snapshot, candidate_id=args.candidate_id)
        recovery, _, records = _load_recovery(args, loaded)
        draft, draft_bytes, draft_record = _read(args.relocation_draft)
        review, _, review_record = _read(args.relocation_review)
        host_record = source_record(args.working_host_report, role="working-host-snapshot")
        records += [draft_record, review_record, host_record, script_record]
        if (draft["schema_version"] != "physical-bar-relocation-draft/v1"
                or draft["placement_eligible"] is not False
                or draft["source_to_revit_xy_mm"] != [0, 0]
                or draft["source_packet_sha256"] != hashlib.sha256(recovery.packet_bytes).hexdigest()
                or draft["source_host_report_sha256"] != host_record["sha256"]
                or review["draft_sha256"] != hashlib.sha256(draft_bytes).hexdigest()):
            raise ValueError("Exact linked research draft, host and explicit identity XY required")
        with args.working_host_report.open("rb") as stream:
            content = stream.read(MAX_WORKING_REPORT_BYTES + 1)
        if len(content) > MAX_WORKING_REPORT_BYTES:
            raise ValueError("Working host report exceeds bounded size")
        if hashlib.sha256(content).hexdigest() != host_record["sha256"]:
            raise ValueError("Host snapshot changed while reading")
        host, _ = inspect_working_solid(load_working_host_json(content, maximum_bytes=MAX_WORKING_REPORT_BYTES))
        def decode(raw):
            return tuple(PhysicalBar(b["id"], d, b["steel_class"], b["diameter_mm"], b["coordinate_mm"],
                tuple(b["longitudinal_mm"]), tuple(b["source_bar_ids"])) for d in PLATE_DIRECTIONS for b in raw[str(d)])
        lanes = source_service_lanes(recovery.patterned_report, loaded.problem)
        before = decode(recovery.normalization_report["accepted"]["raw_bars_by_direction"])
        bars = decode(draft["raw_bars_by_direction"])
        check_relocation(before, bars, lanes, loaded.problem, host)
        report, alternatives = inspect_trims(bars, lanes, loaded.problem, host)
        report["stock_preserving_exchange"] = inspect_stock_exchanges(bars, lanes, loaded.problem, host, alternatives)
        report["original_full_layout_status"] = "blocked_host" if report["host_blocked_before"] else "not_engineering_accepted"
        verify_source_records(records)
        if _code_digest() != code_before:
            raise ValueError("Core code changed during experiment")
        report["source_files"] = records
        report["code_sha256"] = code_before
        report["source_demand_removed"] = False
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("xb") as stream:
            stream.write(_bytes(report))
        print("Independent shortening hypothesis (NOT stock-balanced):")
        print(json.dumps({key: report[key] for key in ("host_blocked_before", "geometrically_shortenable",
            "contained_subinterval_shortenable", "exact_divisor_shortenable", "host_blocked_after_shortest_subinterval",
            "mass_before_kg", "mass_after_kg", "new_body_pairs")}, ensure_ascii=False))
        print("Independent shortening whole-batch stock: " + report["stock_cutting"]["status"])
        exchange = report["stock_preserving_exchange"]
        print("SEPARATE inventory-preserving exchange: " + exchange["status"])
        for accepted in exchange["accepted"]:
            print(json.dumps({"host_blocked_after": accepted["host_blocked_after"],
                "physical_bar_count": accepted["physical_bar_count"], "mass_kg": accepted["mass_kg"],
                "mass_delta_kg": accepted["mass_kg"]-report["mass_before_kg"],
                "stock_status": accepted["stock_cutting"]["status"],
                "full_source_coverage_status": accepted["source_coverage"]["status"],
                "new_same_direction_body_pairs": accepted["new_body_pairs"],
                "changed_bars_with_body_collisions": accepted["changed_bars_with_body_collisions"],
                "full_layout_status": accepted["full_layout_status"],
                "not_checked": accepted["not_checked"],
                "placement_eligible": False}, ensure_ascii=False))
        return 0
    except (ValueError, TypeError, KeyError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
