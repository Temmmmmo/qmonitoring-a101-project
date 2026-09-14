"""Check global ORIGINAL demand during fixed-stock q/U repair; no field transfer."""
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
from rebar.optimization.algorithms.shaped_global_repair import propose_global_shaped_repair
from rebar.optimization.services.shaped_fe_repair import ResearchLayerProfile
from rebar.optimization.services.shaped_global_coverage import check_shaped_global_repair


def run(args):
    if args.output.exists():
        raise ValueError("New output path required; no evidence overwrite")
    started = perf_counter()
    records = [source_record(ROOT/name, role="global-shape-experiment-code") for name in (
        "scripts/experiment_global_shaped_repair.py",
        "src/rebar/optimization/contracts/shaped_physical.py",
        "src/rebar/optimization/services/shaped_geometry.py",
        "src/rebar/optimization/services/shaped_collisions.py",
        "src/rebar/optimization/services/shaped_fe_repair.py",
        "src/rebar/optimization/services/shaped_global_coverage.py",
        "src/rebar/optimization/algorithms/shaped_translation_candidates.py",
        "src/rebar/optimization/algorithms/shaped_global_repair.py")]
    inputs = load_fe_research_inputs(shifted_dir=args.shifted_dir, snapshot=args.snapshot,
        candidate_id=args.candidate_id, working_host_report=args.working_host_report,
        fe_report=args.fe_report, confirm_identity_xy=args.confirm_identity_xy,
        stock_time_limit_s=args.stock_time_limit_s)
    records.extend(inputs.source_files)
    print({"stage": "fresh_source_inputs_validated", "physical_bars": len(inputs.bars)}, flush=True)
    after, search = propose_global_shaped_repair(inputs.bars, inputs.lanes, inputs.problem, inputs.host,
        maximum_axes_per_bar=args.maximum_axes_per_bar, maximum_candidates=args.maximum_candidates,
        maximum_passes=args.maximum_passes, time_limit_s=args.time_limit_s,
        maximum_longitudinal_shift_mm=args.maximum_longitudinal_shift_mm)
    print({"stage": "finite_search_finished", "operations": len(search["operations"]),
           "budget_exhausted": search["budget_exhausted"]}, flush=True)
    checked = check_shaped_global_repair(inputs.bars, after, inputs.lanes, inputs.problem, inputs.host,
        stock_time_limit_s=args.stock_time_limit_s,
        maximum_longitudinal_shift_mm=args.maximum_longitudinal_shift_mm)
    physical = []
    for bar in after:
        record = asdict(bar)
        record["direction"] = str(bar.direction)
        for segment, original in zip(record["segments"], bar.segments):
            segment["kind"] = type(original).__name__
        physical.append(record)
    report = {"schema_version": "global-shaped-repair-experiment/v1", "units": "mm",
        "placement_eligible": False, "engineering_approval": False, "structural_placement_supported": False,
        "case_id": inputs.problem.case_id, "candidate_id": args.candidate_id,
        "source_snapshot_sha256": inputs.loaded.source_sha256,
        "source_host_report_sha256": inputs.input_report["source_host_report_sha256"],
        "source_report_sha256": inputs.input_sha256, "source_files": records,
        "layer_profile": {**asdict(ResearchLayerProfile()), "outer_axis": "X"},
        "checks": checked, "search": search, "physical_bars": physical,
        "runtime_s": perf_counter()-started,
        "source_field_transferred": False, "original_source_values_changed": False,
        "warning": "Full original FE is checked; U anchorage and layer order are declared research assumptions, not a structural Revit packet."}
    verify_source_records(records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(_bytes(report))
    print({key: value for key, value in checked.items() if key.endswith("count") or key in (
        "status", "shaped_host_not_proven_before", "shaped_host_not_proven_after", "physical_metrics")}, flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("shifted-dir", "snapshot", "working-host-report", "fe-report", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--confirm-identity-xy", action="store_true")
    parser.add_argument("--stock-time-limit-s", type=float, default=30)
    parser.add_argument("--time-limit-s", type=float, default=180)
    parser.add_argument("--maximum-axes-per-bar", type=int, default=64)
    parser.add_argument("--maximum-candidates", type=int, default=50000)
    parser.add_argument("--maximum-passes", type=int, default=2)
    parser.add_argument("--maximum-longitudinal-shift-mm", type=float, default=0)
    try:
        run(parser.parse_args(argv))
    except (ValueError, TypeError, KeyError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
