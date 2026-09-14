"""Fresh source-bound fixed-stock exterior U experiment; no Revit writes."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))

from research_fe_inputs import load_fe_research_inputs
from rebar.application.assistant_inputs import source_record, verify_source_records
from rebar.application.physical_layout_recovery import _bytes
from rebar.optimization.algorithms.shaped_edge_repair import propose_exterior_u_repair
from rebar.optimization.services.shaped_collisions import check_shaped_collisions
from rebar.optimization.services.shaped_fe_repair import ResearchLayerProfile, check_shaped_fe_repair


def run(args):
    if args.output.exists():
        raise ValueError("New output path required; never overwrite evidence")
    started = perf_counter()
    records = [source_record(ROOT/name, role="shaped-edge-experiment-code") for name in (
        "scripts/experiment_shaped_edge_repair.py",
        "src/rebar/optimization/contracts/shaped_physical.py",
        "src/rebar/optimization/services/shaped_geometry.py",
        "src/rebar/optimization/services/shaped_collisions.py",
        "src/rebar/optimization/services/shaped_fe_repair.py",
        "src/rebar/optimization/algorithms/shaped_edge_repair.py")]
    inputs = load_fe_research_inputs(shifted_dir=args.shifted_dir, snapshot=args.snapshot,
        candidate_id=args.candidate_id, working_host_report=args.working_host_report,
        fe_report=args.fe_report, confirm_identity_xy=args.confirm_identity_xy,
        stock_time_limit_s=args.stock_time_limit_s)
    records.extend(inputs.source_files)
    after, search = propose_exterior_u_repair(inputs.bars, inputs.lanes, inputs.problem, inputs.host)
    checked = check_shaped_fe_repair(inputs.bars, after, inputs.lanes, inputs.problem, inputs.host,
                                    stock_time_limit_s=args.stock_time_limit_s)
    print({"stage": "geometry_source_stock_checked", "U_count": checked["U_substitution_count"],
           "remaining_host": checked["shaped_host_not_proven_after"]}, flush=True)
    collisions = check_shaped_collisions(after)
    checked["whole_party_3D_collision_check"] = collisions
    checked["not_checked"].remove("whole_party_3D_collisions")
    if checked["status"] == "geometry_and_source_pass_not_engineering_approved" and collisions["status"] != "pass":
        checked["status"] = "blocked_3D_collisions"
    physical = []
    for bar in after:
        record = asdict(bar)
        record["direction"] = str(bar.direction)
        for segment, original in zip(record["segments"], bar.segments):
            segment["kind"] = type(original).__name__
        physical.append(record)
    report = {"schema_version": "shaped-edge-repair-experiment/v1", "units": "mm",
        "placement_eligible": False, "engineering_approval": False, "structural_placement_supported": False,
        "case_id": inputs.problem.case_id, "candidate_id": args.candidate_id,
        "source_snapshot_sha256": inputs.loaded.source_sha256,
        "source_host_report_sha256": inputs.input_report["source_host_report_sha256"],
        "source_report_sha256": inputs.input_sha256, "source_files": records,
        "layer_profile": {**asdict(ResearchLayerProfile()), "outer_axis": "X"},
        "checks": checked, "search": search, "physical_bars": physical,
        "runtime_s": perf_counter()-started, "not_checked": checked["not_checked"],
        "warning": "U anchorage and layer order are explicit unapproved research assumptions. Not a Revit packet."}
    verify_source_records(records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(_bytes(report))
    print({key: checked[key] for key in ("shaped_host_not_proven_before",
        "shaped_host_not_proven_after", "U_substitution_count", "physical_metrics", "status")}, flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("shifted-dir", "snapshot", "working-host-report", "fe-report", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--confirm-identity-xy", action="store_true")
    parser.add_argument("--stock-time-limit-s", type=float, default=30)
    try:
        run(parser.parse_args(argv))
    except (ValueError, TypeError, KeyError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
