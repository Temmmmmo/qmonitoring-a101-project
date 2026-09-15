"""Physically cut straight bars at the measured outer outline; keep all checks."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))

from experiment_tz_outer_scope import decode_shaped_bars
from research_fe_inputs import load_fe_research_inputs
from rebar.application.assistant_inputs import source_record, verify_source_records
from rebar.application.physical_layout_recovery import _bytes
from rebar.optimization.algorithms.tz_boundary_trim import trim_straight_bars_to_outer_boundary
from rebar.optimization.services.tz_boundary_trim import check_boundary_trim


def run(args):
    if args.output.exists():
        raise ValueError("New output path required; previous evidence must not be overwritten")
    code = [source_record(ROOT/p, role="physical-boundary-trim-code") for p in (
        "scripts/experiment_boundary_trim.py", "src/rebar/optimization/algorithms/tz_boundary_trim.py",
        "src/rebar/optimization/services/tz_boundary_trim.py", "src/rebar/optimization/services/tz_outer_scope.py",
        "src/rebar/optimization/services/shaped_geometry.py", "src/rebar/optimization/services/shaped_fe_repair.py",
        "src/rebar/optimization/services/shaped_global_coverage.py", "src/rebar/optimization/services/stock_cutting.py")]
    with args.candidate.open("rb") as stream:
        content = stream.read(32*1024*1024+1)
    if len(content) > 32*1024*1024:
        raise ValueError("Bounded complete candidate required")
    raw = json.loads(content)
    if raw.get("units") != "mm" or raw.get("placement_eligible") is not False or raw.get("engineering_approval") is not False:
        raise ValueError("Explicit unapproved physical candidate in mm required")
    inputs = load_fe_research_inputs(shifted_dir=args.shifted_dir, snapshot=args.snapshot,
        candidate_id=args.candidate_id, working_host_report=args.working_host_report,
        fe_report=args.fe_report, confirm_identity_xy=args.confirm_identity_xy,
        stock_time_limit_s=args.stock_time_limit_s)
    if raw["case_id"] != inputs.problem.case_id or raw["candidate_id"] != args.candidate_id:
        raise ValueError("Exact case and candidate binding required")
    # Archived code is never reused as an acceptance certificate. Data files
    # remain exact immutable inputs, and all result checks are recomputed below.
    data_records = [r for r in raw["source_files"] if Path(r["path"]).suffix != ".py"]
    verify_source_records(data_records)
    required_hashes = {source_record(args.snapshot, role="snapshot")["sha256"],
                       source_record(args.working_host_report, role="host")["sha256"]}
    if not required_hashes <= {r["sha256"] for r in data_records}:
        raise ValueError("Candidate must refer to these exact source/host files")
    before = decode_shaped_bars(raw["physical_bars"])
    print({"stage": "loaded_fresh_original_demand_and_host", "bars": len(before)}, flush=True)
    after, mapping = trim_straight_bars_to_outer_boundary(before, inputs.host, lanes=inputs.lanes,
        nudge_edge_axis=args.nudge_edge_axis, discard_empty_intersections=args.discard_empty_intersections,
        respect_openings=args.respect_openings)
    checked = check_boundary_trim(before, after, mapping, inputs.lanes, inputs.problem,
                                  inputs.host, stock_time_limit_s=args.stock_time_limit_s,
                                  nudge_edge_axis=args.nudge_edge_axis,
                                  discard_empty_intersections=args.discard_empty_intersections,
                                  respect_openings=args.respect_openings)
    physical = []
    for bar in after:
        record = asdict(bar)
        record["direction"] = str(bar.direction)
        for serialized, original in zip(record["segments"], bar.segments):
            serialized["kind"] = type(original).__name__
        physical.append(record)
    records = [*inputs.source_files, *data_records, *code, source_record(args.candidate, role="before-trim-candidate")]
    verify_source_records(records)
    if args.candidate.read_bytes() != content:
        raise ValueError("Candidate changed during computation")
    result = {"schema_version": "tz-boundary-trim-experiment/v1", "units": "mm",
        "case_id": raw["case_id"], "candidate_id": args.candidate_id,
        "source_candidate_sha256": hashlib.sha256(content).hexdigest(),
        "source_snapshot_sha256": inputs.loaded.source_sha256,
        "source_host_report_sha256": source_record(args.working_host_report, role="host")["sha256"],
        "source_files": records, "checks": checked, "physical_bars": physical,
        "placement_eligible": False, "engineering_approval": False, "structural_placement_supported": False,
        "source_field_transferred": False, "original_source_values_changed": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(_bytes(result))
    print({"output": str(args.output), "outside_before": checked["external_boundary_failures_before"],
        "outside_after": checked["external_boundary_failures_after"],
        "geometric_uncovered_FE": checked["geometric_presence"]["uncovered_cell_count"],
        "control40d_uncovered_FE": checked["coverage_with_control_40d"]["uncovered_cell_count"],
        "collision_pairs": checked["collisions"]["proven_collision_pair_count"],
        "uncertain_pairs": checked["collisions"]["uncertain_pair_count"],
        "stock_status": checked["stock_cutting"]["status"], "metrics": checked["physical_metrics"]}, flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("candidate", "shifted-dir", "snapshot", "working-host-report", "fe-report", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--confirm-identity-xy", action="store_true")
    parser.add_argument("--nudge-edge-axis", action="store_true",
                        help="Permit only a radius-sized inward nudge for a bar axis lying on the edge")
    parser.add_argument("--respect-openings", action="store_true",
                        help="Physically split at measured closed holes too; concrete cover remains excluded")
    parser.add_argument("--discard-empty-intersections", action="store_true",
                        help="Record explicit original-to-empty mappings; never delete the original FE demand")
    parser.add_argument("--stock-time-limit-s", type=float, default=30)
    try:
        run(parser.parse_args())
    except (ValueError, TypeError, KeyError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
