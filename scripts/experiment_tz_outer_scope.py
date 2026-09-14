"""Re-scope a verified whole-plate candidate without changing source demand/RVT.

The geometry-first mode is a counterfactual, NOT an accepted placement packet.
Both modes independently report remaining original FE, collision and cutting gates.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))

from research_fe_inputs import load_fe_research_inputs
from rebar.application.assistant_inputs import source_record, verify_source_records
from rebar.application.physical_layout_recovery import _bytes
from rebar.models import Axis, Direction, Layer
from rebar.optimization.algorithms.tz_outer_repair import propose_tz_outer_repair
from rebar.optimization.contracts.shaped_physical import Arc3D, Line3D, ShapedPhysicalBar
from rebar.optimization.services.shaped_geometry import check_shaped_host
from rebar.optimization.services.shaped_global_coverage import check_shaped_global_repair
from rebar.optimization.services.tz_outer_scope import TZ_OUTER_SCOPE, check_tz_outer_batch


def decode_shaped_bars(records):
    if not isinstance(records, list) or not 1 <= len(records) <= 5000:
        raise ValueError("Complete bounded shaped inventory required")
    result = []
    for record in records:
        layer, axis = record["direction"].split("-")
        curves = []
        if not isinstance(record["segments"], list) or not 1 <= len(record["segments"]) <= 16:
            raise ValueError("Bounded explicit curve list required")
        for segment in record["segments"]:
            if segment["kind"] == "Line3D":
                curve = Line3D(tuple(segment["start_mm"]), tuple(segment["end_mm"]))
            elif segment["kind"] == "Arc3D":
                curve = Arc3D(tuple(segment["center_mm"]), tuple(segment["start_mm"]),
                              tuple(segment["normal_unit"]), segment["sweep_rad"])
            else:
                raise ValueError("Explicit supported curve kind required")
            curves.append(curve)
        result.append(ShapedPhysicalBar(record["id"], Direction(Layer(layer), Axis(axis)),
            record["steel_class"], record["diameter_mm"], tuple(record["source_bar_ids"]),
            tuple(curves), record["shape_kind"], tuple(record["demand_segment_indexes"]),
            record["geometry_profile_id"], record["placement_profile_id"], record["selected_cut_length_mm"]))
    return tuple(result)


def run(args):
    if args.output.exists():
        raise ValueError("New output path required; do not overwrite evidence")
    with args.candidate.open("rb") as stream:
        content = stream.read(32*1024*1024+1)
    if len(content) > 32*1024*1024:
        raise ValueError("Candidate exceeds input byte budget")
    raw = json.loads(content)
    if raw["schema_version"] != "global-shaped-repair-experiment/v1" or raw["units"] != "mm":
        raise ValueError("Verified shaped research input in mm required")
    for flag in ("placement_eligible", "engineering_approval", "structural_placement_supported",
                 "source_field_transferred", "original_source_values_changed"):
        if raw[flag] is not False:
            raise ValueError("Explicit research permissions and unchanged source demand required")
    # Old solver/checker code may have evolved since the incumbent was saved.
    # Its recorded claims are NOT reused: fresh full checks follow below. Data
    # and upstream proof files must still match byte-for-byte. Record both code
    # revisions explicitly instead of passing archived claims as current proof.
    archived_code = [r for r in raw["source_files"] if r["role"] == "global-shape-experiment-code"]
    candidate_data = [r for r in raw["source_files"] if r["role"] != "global-shape-experiment-code"]
    verify_source_records(candidate_data)
    inputs = load_fe_research_inputs(shifted_dir=args.shifted_dir, snapshot=args.snapshot,
        candidate_id=args.candidate_id, working_host_report=args.working_host_report,
        fe_report=args.fe_report, confirm_identity_xy=args.confirm_identity_xy,
        stock_time_limit_s=args.stock_time_limit_s)
    if (raw["case_id"] != inputs.problem.case_id or raw["candidate_id"] != args.candidate_id
            or raw["source_snapshot_sha256"] != inputs.loaded.source_sha256
            or raw["source_host_report_sha256"] != inputs.input_report["source_host_report_sha256"]
            or raw["source_report_sha256"] != inputs.input_sha256):
        raise ValueError("Candidate must match the exact original source/host/FE chain")
    bars = decode_shaped_bars(raw["physical_bars"])
    historical = check_shaped_global_repair(inputs.bars, bars, inputs.lanes, inputs.problem,
        inputs.host, stock_time_limit_s=args.stock_time_limit_s, maximum_longitudinal_shift_mm=11700)
    print({"stage": "fresh_source_and_candidate_checked", "bars": len(bars),
           "historical_full_host_failures": historical["shaped_host_not_proven_after"]}, flush=True)
    before_check = check_tz_outer_batch(bars, bars, inputs.lanes, inputs.problem, inputs.host,
                                       stock_time_limit_s=args.stock_time_limit_s)
    print({"stage": "new_scope_before", "outer_failures": before_check["external_boundary_failures_after"]}, flush=True)
    after, search = propose_tz_outer_repair(bars, inputs.lanes, inputs.problem, inputs.host,
        mode=args.mode, allow_transverse=args.allow_transverse,
        repair_collisions=args.repair_collisions,
        maximum_candidates=args.maximum_candidates, time_limit_s=args.time_limit_s)
    checked = check_tz_outer_batch(bars, after, inputs.lanes, inputs.problem, inputs.host,
                                  stock_time_limit_s=args.stock_time_limit_s)
    actual_failures = [(str(b.direction), b.id) for b in after
                       if check_shaped_host(b, inputs.host)["status"] != "pass"]
    physical = []
    for bar in after:
        record = asdict(bar)
        record["direction"] = str(bar.direction)
        for segment, original in zip(record["segments"], bar.segments):
            segment["kind"] = type(original).__name__
        physical.append(record)
    records = [*inputs.source_files, *candidate_data, source_record(args.candidate, role="shaped-incumbent")]
    records.extend(source_record(Path(r["path"]), role="fresh-shape-code") for r in archived_code)
    records.extend(source_record(ROOT/name, role="TZ-scope-experiment-code") for name in (
        "scripts/experiment_tz_outer_scope.py", "src/rebar/optimization/services/tz_outer_scope.py",
        "src/rebar/optimization/algorithms/tz_outer_repair.py"))
    report = {"schema_version": "tz-outer-scope-experiment/v1", "units": "mm", "policy": TZ_OUTER_SCOPE,
        "case_id": inputs.problem.case_id, "candidate_id": args.candidate_id,
        "source_candidate_sha256": hashlib.sha256(content).hexdigest(),
        "source_snapshot_sha256": inputs.loaded.source_sha256,
        "source_host_report_sha256": raw["source_host_report_sha256"], "source_files": records,
        "archived_solver_code_records_not_reused_as_proof": archived_code,
        "incumbent_revalidated_with_current_code": True,
        "before_TZ_scope": before_check, "checks": checked, "search": search, "physical_bars": physical,
        "historical_full_host_failures_not_TZ_failures": historical["shaped_host_not_proven_after"],
        "actual_Revit_host_check_not_a_TZ_gate": {"failures_after": len(actual_failures), "ids": actual_failures},
        "placement_eligible": False, "engineering_approval": False, "structural_placement_supported": False,
        "source_field_transferred": False, "original_source_values_changed": False,
        "warning": "Scope-limited engineering research result; excluded holes/cover do not authorize real Revit placement."}
    verify_source_records(records)
    if args.candidate.read_bytes() != content:
        raise ValueError("Candidate changed during computation")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(_bytes(report))
    print({"output": str(args.output), "mode": args.mode, "transverse": args.allow_transverse,
        "outer_after": checked["external_boundary_failures_after"], "changes": checked["changed_bar_count"],
        "uncovered_FE": checked["source_coverage"]["uncovered_cell_count"],
        "collision_pairs": checked["collisions"]["proven_collision_pair_count"],
        "blockers": checked["tz_blockers"], "budget_exhausted": search["budget_exhausted"]}, flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("candidate", "shifted-dir", "snapshot", "working-host-report", "fe-report", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--confirm-identity-xy", action="store_true")
    parser.add_argument("--mode", choices=("geometry-first", "preserve-demand"), default="preserve-demand")
    parser.add_argument("--allow-transverse", action="store_true")
    parser.add_argument("--repair-collisions", action="store_true")
    parser.add_argument("--maximum-candidates", type=int, default=20000)
    parser.add_argument("--time-limit-s", type=float, default=120)
    parser.add_argument("--stock-time-limit-s", type=float, default=30)
    try:
        run(parser.parse_args(argv))
    except (ValueError, TypeError, KeyError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
