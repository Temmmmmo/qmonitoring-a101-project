"""Check finite pure-axis corrections of an entire previously repaired FE party.

Candidate JSON is a search hint, never a geometry/coverage certificate. The new
complete party passes the typed joint checker, with all installed ends retained.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import math
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from correct_small_openings import _code_digest, _read
from research_fe_inputs import load_fe_research_inputs
from rebar.application.assistant_inputs import source_record, verify_source_records
from rebar.application.physical_layout_recovery import _bytes, _raw
from rebar.optimization.services.fe_transverse_repair import _background_and_window, check_fe_transverse_repair
from rebar.optimization.services.opening_relocation import collision, contained, lane_map, material_and_holes


def validate_args(args):
    if args.output.exists():
        raise ValueError("Existing output cannot be overwritten")
    if args.confirm_identity_xy is not True:
        raise ValueError("Explicit identity XY confirmation required")
    for label, value, low, high in (("maximum_shift_mm", args.maximum_shift_mm, 0, 300),
            ("stock_time_limit_s", args.stock_time_limit_s, .001, 60)):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not low <= value <= high):
            raise ValueError(f"{label} must be finite in {low}..{high}")
    for value in (args.maximum_candidates_per_bar, args.maximum_total_candidates):
        if type(value) is not int or not 1 <= value <= 50000:
            raise ValueError("Finite positive candidate resource limits required")


def select_candidates(pool, inputs, *, maximum_shift_mm, maximum_candidates_per_bar, maximum_total_candidates):
    """Finite greedy hint filter; final complete FE certification is mandatory."""
    if (pool["schema_version"] != "free-axis-catalogue-research/v1"
            or any(pool[key] is not False for key in ("placement_eligible", "engineering_approval", "source_demand_removed"))
            or not isinstance(pool["catalogue_candidates"], list)
            or len(pool["catalogue_candidates"]) > len(inputs.bars)):
        raise ValueError("Bounded false-permission axis candidate pool required")
    sources = lane_map(inputs.lanes)
    material, _, _ = material_and_holes(inputs.host)
    original = {(str(bar.direction), bar.id): bar for bar in inputs.bars}
    selected = dict(original)
    seen, rejected, considered, raw_count, truncated = set(), Counter(), 0, 0, False
    for row in pool["catalogue_candidates"]:
        key = row["direction"], row["bar_id"]
        if key not in original or key in seen or not isinstance(row["candidates"], list):
            raise ValueError("Each candidate group must identify one unique existing bar")
        raw_count += len(row["candidates"])
        if raw_count > maximum_total_candidates:
            raise ValueError("Complete raw candidate pool exceeds configured budget")
        seen.add(key)
        bar = original[key]
        if contained(bar, material, inputs.host.side_cover_mm):
            rejected["original_bar_already_contained"] += 1
            continue
        choices = set()
        for item in row["candidates"]:
            q, length = item["coordinate_mm"], item["length_mm"]
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (q, length)):
                raise ValueError("Finite numeric candidate axes/lengths required")
            if abs(length-bar.installed_length_mm) > 1e-6:
                continue
            choices.add(q)
        choices = sorted(choices, key=lambda q: (abs(q-bar.transverse_axis_mm), q))
        truncated |= len(choices) > maximum_candidates_per_bar
        for q in choices[:maximum_candidates_per_bar]:
            considered += 1
            if considered > maximum_total_candidates:
                raise ValueError("Complete finite candidate evaluation exceeds configured budget")
            if not 1e-6 < abs(q-bar.transverse_axis_mm) <= maximum_shift_mm+1e-6:
                rejected["unchanged_or_shift_bound"] += 1
                continue
            candidate = replace(bar, transverse_axis_mm=q)
            try:
                _background_and_window(candidate, tuple(sources[bar.direction, owner] for owner in bar.source_bar_ids))
            except ValueError:
                rejected["source_window_or_background"] += 1
                continue
            if not contained(candidate, material, inputs.host.side_cover_mm):
                rejected["actual_host"] += 1
                continue
            if any(other_key != key and collision(candidate, other) for other_key, other in selected.items()):
                rejected["joint_same_direction_body_collision"] += 1
                continue
            selected[key] = candidate
            break
    return tuple(selected[str(bar.direction), bar.id] for bar in inputs.bars), {
        "method": "bounded_greedy_actual_host_background_and_joint_collision_filter_then_complete_FE_validation",
        "candidate_evaluations": considered, "rejections": dict(rejected),
        "per_bar_pool_truncated": truncated, "global_optimality_proven": False,
        "candidate_coverage_flags_trusted": False}


def run(args):
    validate_args(args)
    started, code = perf_counter(), _code_digest()
    script_record = source_record(Path(__file__), role="experiment-code")
    inputs = load_fe_research_inputs(shifted_dir=args.shifted_dir, snapshot=args.snapshot,
        candidate_id=args.candidate_id, working_host_report=args.working_host_report,
        fe_report=args.fe_report, confirm_identity_xy=args.confirm_identity_xy,
        stock_time_limit_s=args.stock_time_limit_s)
    pool, _, pool_record = _read(args.candidate_pool)
    records = [*inputs.source_files, script_record, pool_record]
    if not isinstance(pool.get("source_files"), list) or not 1 <= len(pool["source_files"]) <= 200:
        raise ValueError("Bounded source-bound candidate pool required")
    pool_records = [{**row, "role": row.get("role", "candidate-pool-support")} for row in pool["source_files"]]
    verify_source_records(pool_records)
    if not any(Path(row["path"]).resolve() == args.fe_report.resolve()
               and row["sha256"] == inputs.input_sha256 for row in pool["source_files"]):
        raise ValueError("Candidate pool does not bind the exact current FE report")
    records.extend(pool_records)
    after, search = select_candidates(pool, inputs, maximum_shift_mm=args.maximum_shift_mm,
        maximum_candidates_per_bar=args.maximum_candidates_per_bar,
        maximum_total_candidates=args.maximum_total_candidates)
    checks = check_fe_transverse_repair(inputs.bars, after, inputs.lanes, inputs.problem, inputs.host,
        maximum_shift_mm=args.maximum_shift_mm, stock_time_limit_s=args.stock_time_limit_s)
    if checks["stock_cutting"]["status"] != "pass":
        raise ValueError("Fresh complete stock verification did not pass")
    report = {"schema_version": "fe-transverse-repair-experiment/v1", "units": "mm",
        "placement_eligible": False, "structural_placement_supported": False, "engineering_approval": False,
        "source_demand_removed": False, "source_demand_values_changed": False,
        "legacy_source_certificate_reused": False, "source_to_revit_xy_mm": [0, 0],
        "case_id": inputs.problem.case_id, "candidate_id": args.candidate_id,
        "source_report_sha256": inputs.input_sha256,
        "source_host_report_sha256": inputs.input_report["source_host_report_sha256"],
        "source_files": records, "code_sha256": code, "raw_bars_by_direction": _raw(after),
        "configuration": {"maximum_shift_mm": args.maximum_shift_mm,
            "maximum_candidates_per_bar": args.maximum_candidates_per_bar,
            "maximum_total_candidates": args.maximum_total_candidates},
        "checks": checks, "search": search, "runtime_s": perf_counter()-started,
        "not_checked": checks["not_checked"],
        "warning": "Full FE-preserving axis research. All prior installed ends retained; not a structural Revit packet."}
    verify_source_records(records)
    if _code_digest() != code:
        raise ValueError("Core code changed during the experiment; rerun stable code")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(_bytes(report))
    print({key: checks[key] for key in ("host_blocked_before", "host_blocked_after", "moved_bar_count",
        "physical_bar_count", "additional_mass_kg", "same_direction_body_pairs_after", "status")}, flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("shifted-dir", "snapshot", "working-host-report", "fe-report", "candidate-pool", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--confirm-identity-xy", action="store_true")
    parser.add_argument("--maximum-shift-mm", type=float, default=300)
    parser.add_argument("--stock-time-limit-s", type=float, default=30)
    parser.add_argument("--maximum-candidates-per-bar", type=int, default=128)
    parser.add_argument("--maximum-total-candidates", type=int, default=5000)
    try:
        run(parser.parse_args(argv))
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
