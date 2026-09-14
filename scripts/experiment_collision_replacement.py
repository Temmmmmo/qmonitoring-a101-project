"""Bounded research replacement of coaxial body collisions; never a Revit packet.

Freshly restores the shifted source and independently verifies upstream FE repair.
Finite options are max-D fusion or tangent duplication of one bar. A bounded
catalogue residue search lengthens ONLY new descendants, then an independent
whole-party checker proves actual FE cores, owner fragments, background and stock.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import math
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from correct_small_openings import _code_digest, _read
from experiment_fe_host_repair import decode_bars
from rebar.application.assistant_inputs import source_record, verify_source_records
from rebar.application.physical_layout_recovery import _bytes, _raw
from rebar.optimization.services.collision_replacement import (
    CollisionReplacementLimits, CollisionReplacementOperation, check_collision_replacement,
    freeze_owner_fe_service, operation_bars, owner_service_preserved, transverse_source_geometry_ok,
)
from rebar.optimization.services.cutting import PLATE_11700_CUT_LENGTHS_MM
from rebar.optimization.services.detailing import rebar_mass_kg
from rebar.optimization.services.opening_relocation import collision, collision_pairs, contained, lane_map, material_and_holes
from rebar.optimization.services.physical_host_fit import _host_intervals

TOL = 1e-6


def _validate_args(args):
    if args.output.exists():
        raise ValueError("Existing output must not be overwritten; choose a NEW file")
    if args.confirm_identity_xy is not True:
        raise ValueError("Explicit --confirm-identity-xy required")
    for name, ceiling in (("maximum_mass_kg", 1e9), ("stock_time_limit_s", 60)):
        value = getattr(args, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0.001 <= value <= ceiling:
            raise ValueError(f"{name} must be finite and positive <= {ceiling}")
    for name, ceiling in (("maximum_bar_count", 5000), ("maximum_position_count", 512),
                          ("maximum_operations", 64), ("maximum_residue_states", 11700)):
        value = getattr(args, name)
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError(f"{name} must be a bounded positive integer <= {ceiling}")


def _required(bar, frozen):
    intervals = [frozen[(bar.direction, owner)].required_interval_mm for owner in bar.source_bar_ids]
    positive = [v for v in intervals if v is not None]
    return (min(v[0] for v in positive), max(v[1] for v in positive)) if positive else None


def _position(bar, length, required, material, cover):
    if required is None:
        return None
    lower, upper = required[1]+40*bar.diameter_mm-length, required[0]-40*bar.diameter_mm
    if lower > upper+TOL:
        return None
    if lower > upper:
        lower = upper
    preferred = bar.installed_interval_mm[0]
    base = replace(bar, installed_interval_mm=(preferred, preferred+length))
    choices = []
    for a, b in _host_intervals(base, material, cover, 4096):
        lo, hi = max(a, lower), min(b, upper)
        if lo <= hi:
            start = min(max(preferred, lo), hi)
            candidate = replace(bar, installed_interval_mm=(start, start+length))
            if contained(candidate, material, cover):
                choices.append(candidate)
    if choices:
        return min(choices, key=lambda b: (abs(b.installed_interval_mm[0]-preferred), b.installed_interval_mm))
    # Explicit blocked research alternative, not a successful host fit.
    start = min(max(preferred, lower), upper)
    return replace(bar, installed_interval_mm=(start, start+length))


def _smallest(bar, frozen, material, cover):
    required = _required(bar, frozen)
    return next((candidate for length in PLATE_11700_CUT_LENGTHS_MM
                 if (candidate := _position(bar, length, required, material, cover)) is not None), None)


def _valid_additions(added, removed, current, frozen, sources):
    if any(not transverse_source_geometry_ok(b, sources) for b in added):
        return False
    untouched = tuple(b for b in current if (b.direction, b.id) not in removed)
    if any(collision(b, other) for b in added for other in untouched):
        return False
    if any(collision(b, other) for i, b in enumerate(added) for other in added[i+1:]):
        return False
    owner_keys = {(b.direction, owner) for b in current if (b.direction, b.id) in removed for owner in b.source_bar_ids}
    return all(owner_service_preserved(frozen[key], added, sources) for key in owner_keys)


def propose_operations(before, lanes, problem, host, *, maximum_operations=64):
    frozen = freeze_owner_fe_service(before, lanes, problem)
    sources = lane_map(lanes)
    material, _, _ = material_and_holes(host)
    original = {(str(b.direction), b.id): b for b in before}
    operations, trace, used = [], [], set()
    current = before
    for direction, first_id, second_id in sorted(collision_pairs(before)):
        pair = (original[(direction, first_id)], original[(direction, second_id)])
        row = {"direction": direction, "pair_ids": [first_id, second_id], "candidate_count": 0}
        keys = {(b.direction, b.id) for b in pair}
        if used & keys or len(operations) >= maximum_operations:
            row["status"] = "skipped_disjoint_pair_or_operation_limit"
            trace.append(row)
            continue
        if pair[0].transverse_axis_mm != pair[1].transverse_axis_mm or pair[0].steel_class != pair[1].steel_class:
            row["status"] = "unsupported_noncoaxial_or_material"
            trace.append(row)
            continue
        options = []
        fusion = replace(pair[0], id=first_id+"/collision-fused", diameter_mm=max(b.diameter_mm for b in pair),
            source_bar_ids=tuple(sorted(set(pair[0].source_bar_ids) | set(pair[1].source_bar_ids))))
        fusion = _smallest(fusion, frozen, material, host.side_cover_mm)
        if fusion is not None:
            options.append(CollisionReplacementOperation("fuse", pair[0].direction,
                (first_id, second_id), (first_id, second_id), (fusion,)))
        for target, stationary in (pair, tuple(reversed(pair))):
            gap = (target.diameter_mm+stationary.diameter_mm)/2
            new = tuple(_smallest(replace(target, id=target.id+"/collision-"+side,
                transverse_axis_mm=target.transverse_axis_mm+sign*gap), frozen, material, host.side_cover_mm)
                for side, sign in (("left", -1), ("right", 1)))
            if all(b is not None for b in new):
                options.append(CollisionReplacementOperation("split_sides", target.direction,
                    (first_id, second_id), (target.id,), new))
        valid = []
        for op in options:
            removed = {(op.direction, i) for i in op.removed_ids}
            if not _valid_additions(op.added_bars, removed, current, frozen, sources):
                continue
            old = tuple(b for b in pair if b.id in op.removed_ids)
            host_delta = sum(not contained(b, material, host.side_cover_mm) for b in op.added_bars) - sum(
                not contained(b, material, host.side_cover_mm) for b in old)
            mass_delta = sum(rebar_mass_kg(b.diameter_mm, b.installed_length_mm, 1) for b in op.added_bars) - sum(
                rebar_mass_kg(b.diameter_mm, b.installed_length_mm, 1) for b in old)
            valid.append(((host_delta, mass_delta, len(op.added_bars)-len(old), op.removed_ids), op))
        row["candidate_count"] = len(valid)
        if valid:
            score, chosen = min(valid, key=lambda v: v[0])
            operations.append(chosen)
            current = operation_bars(before, tuple(operations))
            used.update(keys)
            row.update(status="proposed", kind=chosen.kind, removed_ids=chosen.removed_ids,
                host_delta=score[0], mass_delta_kg=score[1])
        else:
            row["status"] = "no_frozen_owner_preserving_candidate"
        trace.append(row)
    return tuple(operations), frozen, trace


def balance_new_descendant_lengths(before, operations, frozen, lanes, host, *, maximum_residue_states=11700):
    """Bounded necessary-residue search; final exact stock checker is mandatory.

One best (host failures, added length) path per residue is retained. This is not
an exhaustive search over cut packings or alternative pair operations.
"""
    current = operation_bars(before, operations)
    sources = lane_map(lanes)
    material, _, _ = material_and_holes(host)
    added = [b for op in operations for b in op.added_bars]
    replacements, telemetry = {}, []
    for group in sorted({(b.steel_class, b.diameter_mm) for b in added}):
        members = [b for b in added if (b.steel_class, b.diameter_mm) == group]
        total = math.fsum(b.installed_length_mm for b in current if (b.steel_class, b.diameter_mm) == group)
        if abs(total-round(total)) > TOL:
            raise ValueError("Catalogue residue search requires exact integer-mm total")
        states = {0: (0, 0, ())}
        for bar in members:
            variants = []
            for length in PLATE_11700_CUT_LENGTHS_MM:
                if length < bar.installed_length_mm-TOL:
                    continue
                candidate = _position(bar, length, _required(bar, frozen), material, host.side_cover_mm)
                if candidate is None or not transverse_source_geometry_ok(candidate, sources):
                    continue
                if any(collision(candidate, other) for other in current if (other.direction, other.id) != (bar.direction, bar.id)):
                    continue
                variants.append((int(round(length-bar.installed_length_mm)),
                    int(not contained(candidate, material, host.side_cover_mm)), candidate))
            new_states = {}
            for residue, (blocked, added_length, choices) in states.items():
                for increment, new_blocked, candidate in variants:
                    key = (residue+increment) % 11700
                    score = (blocked+new_blocked, added_length+increment)
                    previous = new_states.get(key)
                    if previous is None or score < previous[:2]:
                        new_states[key] = (*score, (*choices, candidate))
            if len(new_states) > maximum_residue_states:
                raise ValueError("Residue state budget exceeded; no truncated stock proof")
            states = new_states
        target = -int(round(total)) % 11700
        result = states.get(target)
        telemetry.append({"steel_class": group[0], "diameter_mm": group[1], "state_count": len(states),
            "target_residue_mm": target, "status": "residue_balanced_not_cutting_proof" if result else "no_bounded_residue_candidate",
            "added_length_mm": result[1] if result else None})
        if result:
            replacements.update(((b.direction, b.id), b) for b in result[2])
    balanced = tuple(replace(op, added_bars=tuple(replacements.get((b.direction, b.id), b) for b in op.added_bars)) for op in operations)
    return balanced, telemetry


def _operation_json(op):
    return {"kind": op.kind, "direction": str(op.direction), "pair_ids": op.pair_ids,
        "removed_ids": op.removed_ids, "added_bars_by_direction": _raw(op.added_bars)}


def restore_transverse_input(path, restored, *, stock_time_limit_s=30):
    """Optional newer axis proof; never trust an archived checks/status field."""
    from rebar.optimization.services.fe_transverse_repair import check_fe_transverse_repair
    value, _, record = _read(path)
    if (value["schema_version"] != "fe-transverse-repair-experiment/v1"
            or value["units"] != "mm"
            or json.dumps(value["source_to_revit_xy_mm"], allow_nan=False) != "[0, 0]"
            or any(value[key] is not False for key in ("placement_eligible", "structural_placement_supported",
                "engineering_approval", "source_demand_removed", "source_demand_values_changed"))
            or value["source_report_sha256"] != restored.input_sha256
            or value["case_id"] != restored.problem.case_id
            or value["candidate_id"] != restored.loaded.snapshot["candidate_id"]):
        raise ValueError("Exact same upstream FE source and literal research-only transverse schema required")
    records = list(value["source_files"])
    verify_source_records(records)
    expected = {(str(Path(v["path"]).resolve()), v["sha256"], v["role"]) for v in restored.source_files}
    actual = {(str(Path(v["path"]).resolve()), v["sha256"], v["role"]) for v in records}
    if not expected <= actual:
        raise ValueError("Transverse report omits or changes independently restored source chain")
    after = decode_bars(value["raw_bars_by_direction"])
    checks = check_fe_transverse_repair(restored.bars, after, restored.lanes, restored.problem, restored.host,
        maximum_shift_mm=value["configuration"]["maximum_shift_mm"], stock_time_limit_s=stock_time_limit_s)
    return after, checks, [*records, record]


def run(args):
    _validate_args(args)
    # Shared read-only loader independently replays source -> opening -> FE repair.
    from research_fe_inputs import load_fe_research_inputs

    started, code_before = perf_counter(), _code_digest()
    own_records = [source_record(Path(__file__), role="experiment-code"),
        source_record(ROOT/"scripts/research_fe_inputs.py", role="experiment-helper-code")]
    restored = load_fe_research_inputs(shifted_dir=args.shifted_dir, snapshot=args.snapshot,
        candidate_id=args.candidate_id, working_host_report=args.working_host_report,
        fe_report=args.combined_repair, confirm_identity_xy=args.confirm_identity_xy,
        stock_time_limit_s=args.stock_time_limit_s)
    before, lanes, problem, host = restored.bars, restored.lanes, restored.problem, restored.host
    records = [*restored.source_files, *own_records]
    transverse_checks = None
    if args.transverse_report is not None:
        before, transverse_checks, extra_records = restore_transverse_input(args.transverse_report, restored,
            stock_time_limit_s=args.stock_time_limit_s)
        records.extend(extra_records)
    print("1/3: Freeze every original owner FE fragment and propose bounded pair replacements", flush=True)
    operations, frozen, trace = propose_operations(before, lanes, problem, host, maximum_operations=args.maximum_operations)
    print("2/3: Rebalance only new descendants against the complete 11700-mm batch", flush=True)
    balanced, stock_search = balance_new_descendant_lengths(before, operations, frozen, lanes, host,
        maximum_residue_states=args.maximum_residue_states)
    limits = CollisionReplacementLimits(args.maximum_mass_kg, args.maximum_bar_count,
        args.maximum_position_count, maximum_operations=args.maximum_operations)
    print("3/3: Independent owner/full-FE/background/pairs/host/cutting verification", flush=True)
    after, checks = check_collision_replacement(before, balanced, lanes, problem, host,
        limits=limits, stock_time_limit_s=args.stock_time_limit_s)
    from rebar.optimization.services.host_conflict_diagnostics import (
        diagnose_physical_host_failures, inspect_source_material_mismatch, inspect_straight_40d_bbox_obstruction,
    )
    report = {"schema_version": "collision-replacement-experiment/v1", "units": "mm",
        "placement_eligible": False, "structural_placement_supported": False, "engineering_approval": False,
        "source_demand_removed": False, "source_demand_values_changed": False,
        "legacy_source_certificate_reused": False, "source_to_revit_xy_mm": [0, 0],
        "case_id": problem.case_id, "candidate_id": args.candidate_id,
        "source_files": records, "code_sha256": code_before, "operations": [_operation_json(op) for op in balanced],
        "checks": checks, "upstream_transverse_checks": transverse_checks,
        "source_report_sha256": restored.input_sha256,
        "host_diagnostics": diagnose_physical_host_failures(after, host),
        "source_material_mismatch": inspect_source_material_mismatch(problem, host),
        "straight_40d_obstruction": inspect_straight_40d_bbox_obstruction(problem, host),
        "raw_bars_by_direction": _raw(after), "search": {"pair_options": trace, "stock_residue": stock_search,
            "globally_optimal": False, "exhaustive_stock_packing_search": False},
        "limits": {"maximum_mass_kg": args.maximum_mass_kg, "maximum_bar_count": args.maximum_bar_count,
            "maximum_position_count": args.maximum_position_count}, "runtime_s": perf_counter()-started,
        "warning": "NEW physical owner-union research schema, not a phase/zone certificate or Revit packet. Host regression is NOT accepted."}
    verify_source_records(records)
    if _code_digest() != code_before:
        raise ValueError("Core code changed during replacement experiment")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(_bytes(report))
    print(json.dumps({key: checks[key] for key in ("status", "physical_bar_count", "additional_mass_kg",
        "position_count", "host_blocked_before", "host_blocked_after", "same_direction_body_pairs_after")}), flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("shifted-dir", "snapshot", "working-host-report", "combined-repair", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--transverse-report", type=Path)
    parser.add_argument("--confirm-identity-xy", action="store_true")
    parser.add_argument("--maximum-mass-kg", type=float, default=2878.76*1.15)
    parser.add_argument("--maximum-bar-count", type=int, default=1227)
    parser.add_argument("--maximum-position-count", type=int, default=74)
    parser.add_argument("--maximum-operations", type=int, default=64)
    parser.add_argument("--maximum-residue-states", type=int, default=11700)
    parser.add_argument("--stock-time-limit-s", type=float, default=30)
    try:
        run(parser.parse_args(argv))
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
