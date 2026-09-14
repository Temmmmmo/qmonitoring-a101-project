"""Reproduce a source-bound transverse demand experiment; no bar geometry writes."""
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
from rebar.optimization.contracts.demand_transfer import DemandTransferProfile
from rebar.optimization.services.demand_transfer import (
    construct_local_transverse_transfer, construct_refined_transverse_transfer,
)
from rebar.optimization.services.demand_transfer_diagnostics import (
    retained_transfer_geometry_diagnostics, target_density_summary,
)


def run(args):
    if args.output.exists():
        raise ValueError("A new output path is required; no overwrite")
    started = perf_counter()
    code_records = [source_record(ROOT/path, role="demand-transfer-code") for path in (
        "scripts/experiment_demand_transfer.py", "src/rebar/optimization/contracts/demand_transfer.py",
        "src/rebar/optimization/services/demand_transfer.py",
        "src/rebar/optimization/services/demand_transfer_diagnostics.py")]
    inputs = load_fe_research_inputs(shifted_dir=args.shifted_dir, snapshot=args.snapshot,
        candidate_id=args.candidate_id, working_host_report=args.working_host_report,
        fe_report=args.fe_report, confirm_identity_xy=args.confirm_identity_xy,
        stock_time_limit_s=args.stock_time_limit_s)
    records = [*inputs.source_files, *code_records,
        source_record(args.reference_document, role="source-STO-research-analogy-not-universal-permission")]
    profile = DemandTransferProfile(maximum_transverse_span_mm=args.maximum_transverse_span_mm,
        recipient_clearance_mm=args.recipient_clearance_mm)
    rows = []
    for problem in inputs.problem.direction_problems:
        constructor = construct_refined_transverse_transfer if args.refinement_passes else construct_local_transverse_transfer
        refinement = {"refinement_passes": args.refinement_passes,
            "maximum_transverse_fragment_width_mm": args.maximum_transverse_fragment_width_mm} if args.refinement_passes else {}
        checked, search = constructor(problem, inputs.host,
            source_snapshot_sha256=inputs.loaded.source_sha256,
            host_report_sha256=inputs.input_report["source_host_report_sha256"], profile=profile,
            compression_scales=tuple(args.compression_scales), maximum_candidate_checks=args.maximum_candidate_checks,
            maximum_longitudinal_slices=args.maximum_longitudinal_slices,
            recipient_selection=args.recipient_selection, **refinement)
        certificate = asdict(checked.certificate)
        certificate["direction"] = str(problem.demand.direction)
        rows.append({"direction": str(problem.demand.direction), "certificate": certificate,
            "effective_target_patches": [asdict(p) for p in checked.target_patches],
            "checks": checked.report, "search": search,
            "target_density": target_density_summary(checked),
            "retained_geometry": retained_transfer_geometry_diagnostics(problem, inputs.host, checked)})
        print({"direction": str(problem.demand.direction), "transfers": len(checked.certificate.pieces),
            "remaining_FE_over_material_0_001mm2": checked.report["retained_FE_outside_material_above_0_001mm2"],
            "remaining_FE_over_clearance_0_001mm2": checked.report["retained_FE_outside_clearance_above_0_001mm2"],
            "candidate_checks": search["candidate_geometry_checks"],
            "original_integral_mm3": checked.report["original_additional_demand_integral_mm3"],
            "target_integral_mm3": checked.report["target_additional_demand_integral_mm3"]}, flush=True)
    report = {"schema_version": "source-demand-transfer-experiment/v1", "units": "mm",
        "case_id": inputs.problem.case_id, "candidate_id": args.candidate_id,
        "source_snapshot_sha256": inputs.loaded.source_sha256,
        "source_host_report_sha256": inputs.input_report["source_host_report_sha256"],
        "previous_physical_report_sha256": inputs.input_sha256, "source_files": records,
        "placement_eligible": False, "structural_placement_supported": False, "engineering_approval": False,
        "source_demand_removed": False, "original_source_values_changed": False,
        "original_bar_geometry_changed": False, "legacy_source_certificate_reused": False,
        "directions": rows, "runtime_s": perf_counter()-started,
        "warning": "Transferred target is NOT a solved physical layout. Local<=600mm is an explicit research analogy.",
        "not_checked": ["actual_supply_of_the_transferred_target", "anchorage", "stock", "body_collisions",
            "existing_Revit_background_and_vertical_reinforcement", "actual_Z", "engineering_acceptance"]}
    verify_source_records(records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(_bytes(report))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("shifted-dir", "snapshot", "working-host-report", "fe-report", "output", "reference-document"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--confirm-identity-xy", action="store_true")
    parser.add_argument("--maximum-transverse-span-mm", type=float, default=600)
    parser.add_argument("--recipient-clearance-mm", type=float, default=33)
    parser.add_argument("--compression-scales", nargs="+", type=float, default=[1, .75, .5, .25])
    parser.add_argument("--maximum-candidate-checks", type=int, default=50000)
    parser.add_argument("--maximum-longitudinal-slices", type=int, default=10000)
    parser.add_argument("--stock-time-limit-s", type=float, default=30)
    parser.add_argument("--recipient-selection", choices=("nearest", "minimum-peak"), default="nearest")
    parser.add_argument("--refinement-passes", type=int, default=0)
    parser.add_argument("--maximum-transverse-fragment-width-mm", type=float, default=300)
    try:
        run(parser.parse_args(argv))
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
