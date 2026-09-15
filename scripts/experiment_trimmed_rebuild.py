"""Bounded hypothesis test on a replayed hole-trimmed batch; never a web default."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from audit_trimmed_demand import ROOT, load_trimmed_inputs
from rebar.application.assistant_inputs import source_record, verify_source_records
from rebar.application.physical_layout_recovery import _bytes
from rebar.optimization.algorithms.trimmed_repair import rebuild_trimmed_zones
from rebar.optimization.algorithms.trimmed_length_cleanup import prune_redundant_trimmed_bars


def run(args):
    if args.output.exists():
        raise ValueError("New output file required; historical results are immutable")
    code = [source_record(ROOT/path, role="rebuild-experiment-code") for path in (
        "scripts/experiment_trimmed_rebuild.py", "src/rebar/optimization/algorithms/trimmed_repair.py",
        "src/rebar/optimization/services/trimmed_repair.py", "src/rebar/optimization/algorithms/trimmed_length_cleanup.py",
        "src/rebar/optimization/services/trimmed_length_cleanup.py")]
    inputs = load_trimmed_inputs(report_dir=args.report_dir, snapshot=args.snapshot,
        working_host_report=args.working_host_report, candidate_id=args.candidate_id,
        stock_time_limit_s=args.stock_time_limit_s)
    print({"stage": "replayed_original_DXF_and_trim", "physical_bars": len(inputs.bars),
        "presence_FE": inputs.checks["geometric_presence"]["uncovered_cell_count"]}, flush=True)
    initial, cleanup = inputs.bars, None
    if args.prune_first:
        initial, cleanup = prune_redundant_trimmed_bars(initial, inputs.lanes, inputs.problem, inputs.host,
            stock_time_limit_s=args.stock_time_limit_s)
        print({"stage": "nonregression_cleanup", "physical_bars": len(initial)}, flush=True)
    bars, checks = rebuild_trimmed_zones(initial, inputs.before, inputs.lanes, inputs.problem, inputs.host,
        maximum_mass_kg=args.maximum_mass_kg, maximum_shift_mm=args.maximum_shift_mm,
        maximum_candidates=args.maximum_candidates, maximum_additions=args.maximum_additions,
        time_limit_s=args.time_limit_s, stock_time_limit_s=args.stock_time_limit_s,
        additional_cut_lengths_mm=(585., 650., 780., 900., 975.) if args.short_stock_divisors else ())
    records = [*inputs.source_files, *code]
    verify_source_records(records)
    summary = {"metrics": checks["physical_metrics"],
        "presence_FE": checks["geometric_presence"]["uncovered_cell_count"],
        "control40d_FE": checks["coverage_with_control_40d"]["uncovered_cell_count"],
        "material_failures": checks["material_boundary_failures_after"],
        "collisions": checks["collisions"]["proven_collision_pair_count"],
        "uncertain_pairs": checks["collisions"]["uncertain_pair_count"],
        "stock_status": checks["stock_cutting"]["status"],
        "accepted_changes": len(checks["search"]["actions"]),
        "search": {k: v for k, v in checks["search"].items() if k != "actions"}}
    result = {"schema_version": "trimmed-rebuild-hypothesis/v1", "units": "mm", "case_id": inputs.problem.case_id,
        "source_files": records, "hypothesis": "whole-catalogue-lane-replacements-after-physical-cut",
        "summary": summary, "checks": checks, "cleanup_before_rebuild": cleanup,
        "physical_bars": [asdict(bar) for bar in bars],
        "placement_eligible": False, "engineering_approval": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Convert enums using the shared serialization boundary, never str(dataclass).
    from rebar.reporting.serialization import to_jsonable
    with args.output.open("xb") as stream:
        stream.write(_bytes(to_jsonable(result)))
    print(summary, flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("report-dir", "snapshot", "working-host-report", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--candidate-id", default="plate:52")
    parser.add_argument("--prune-first", action="store_true")
    parser.add_argument("--short-stock-divisors", action="store_true",
                        help="Research hypothesis: test exact 11700 divisors 585/650/780/900/975 mm")
    parser.add_argument("--maximum-mass-kg", type=float, required=True)
    parser.add_argument("--maximum-shift-mm", type=float, default=150)
    parser.add_argument("--maximum-candidates", type=int, default=2000)
    parser.add_argument("--maximum-additions", type=int, default=128)
    parser.add_argument("--time-limit-s", type=float, default=30)
    parser.add_argument("--stock-time-limit-s", type=float, default=10)
    run(parser.parse_args())
