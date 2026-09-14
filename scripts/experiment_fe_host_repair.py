"""Joint fixed-axis host repair with frozen original FE obligations and full inventory.

Writes a new research report, not a legacy source certificate or Revit packet.
Every input and the actual source DXF are checked before and after the experiment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from correct_small_openings import _code_digest, _load_recovery, _read
from rebar.application.assistant_inputs import source_record, verify_source_records
from rebar.application.layout_snapshot import load_layout_snapshot
from rebar.application.opening_relocation import source_service_lanes
from rebar.application.physical_layout_recovery import _bytes, _raw
from rebar.application.working_host import load_working_host_json
from rebar.application.working_solid_host import MAX_WORKING_REPORT_BYTES, inspect_working_solid
from rebar.optimization.algorithms.fe_host_repair import solve_fe_host_repair
from rebar.optimization.contracts.physical import PhysicalBar
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS
from rebar.optimization.services.fe_host_repair import check_fe_host_repair, derive_fe_obligations
from rebar.optimization.services.opening_relocation import check_relocation


def decode_bars(raw):
    if set(raw) != set(map(str, PLATE_DIRECTIONS)):
        raise ValueError("Exactly four complete physical directions required")
    return tuple(PhysicalBar(b["id"], d, b["steel_class"], b["diameter_mm"], b["coordinate_mm"],
        tuple(b["longitudinal_mm"]), tuple(b["source_bar_ids"])) for d in PLATE_DIRECTIONS for b in raw[str(d)])


def run(args):
    started = perf_counter()
    if args.output.exists():
        raise ValueError("Existing output must not be overwritten")
    code_before = _code_digest()
    script_record = source_record(Path(__file__), role="experiment-code")
    loaded = load_layout_snapshot(args.snapshot, candidate_id=args.candidate_id)
    recovery, _, records = _load_recovery(args, loaded)
    snapshot_record = source_record(args.snapshot, role="source-snapshot")
    if snapshot_record["sha256"] != loaded.source_sha256:
        raise ValueError("Snapshot changed after fresh source verification")
    records.append(snapshot_record)
    draft, draft_bytes, draft_record = _read(args.relocation_draft)
    review, _, review_record = _read(args.relocation_review)
    host_record = source_record(args.working_host_report, role="working-host-snapshot")
    records += [draft_record, review_record, host_record, script_record]
    if (draft["schema_version"] != "physical-bar-relocation-draft/v1"
            or draft["placement_eligible"] is not False or draft["structural_placement_supported"] is not False
            or draft["source_to_revit_xy_mm"] != [0, 0]
            or draft["source_packet_sha256"] != hashlib.sha256(recovery.packet_bytes).hexdigest()
            or draft["source_host_report_sha256"] != host_record["sha256"]
            or review["draft_sha256"] != hashlib.sha256(draft_bytes).hexdigest()):
        raise ValueError("Exact linked research draft, host and explicit identity XY required")
    with args.working_host_report.open("rb") as stream:
        content = stream.read(MAX_WORKING_REPORT_BYTES + 1)
    if len(content) > MAX_WORKING_REPORT_BYTES or hashlib.sha256(content).hexdigest() != host_record["sha256"]:
        raise ValueError("Host snapshot exceeds budget or changed while reading")
    host, _ = inspect_working_solid(load_working_host_json(content, maximum_bytes=MAX_WORKING_REPORT_BYTES))
    lanes = source_service_lanes(recovery.patterned_report, loaded.problem)
    normalized = decode_bars(recovery.normalization_report["accepted"]["raw_bars_by_direction"])
    bars = decode_bars(draft["raw_bars_by_direction"])
    check_relocation(normalized, bars, lanes, loaded.problem, host)
    obligations = derive_fe_obligations(bars, lanes, loaded.problem)
    print("Frozen original FE obligations:", len(obligations), flush=True)
    result, telemetry = solve_fe_host_repair(bars, obligations, host, reassign_lengths=args.reassign_lengths,
        time_limit_s=args.time_limit_s, maximum_candidates_per_bar=args.maximum_candidates_per_bar,
        maximum_total_candidates=args.maximum_total_candidates)
    print("Joint search:", json.dumps({key: value for key, value in telemetry.items()
        if key != "candidate_counts_by_bar"}, ensure_ascii=False), flush=True)
    checks = check_fe_host_repair(bars, result, lanes, loaded.problem, host)
    report = {"schema_version": "fe-host-repair-experiment/v1", "placement_eligible": False,
        "structural_placement_supported": False, "engineering_approval": False,
        "source_demand_removed": False, "source_demand_values_changed": False,
        "source_files": records, "code_sha256": code_before,
        "case_id": loaded.problem.case_id, "candidate_id": loaded.snapshot["candidate_id"],
        "source_to_revit_xy_mm": [0, 0], "reassign_existing_lengths": args.reassign_lengths,
        "checks": checks, "search": telemetry, "raw_bars_by_direction": _raw(result),
        "runtime_s": perf_counter()-started,
        "warning": "NEW physical FE research proof; old rectangular LayoutZone certificates are NOT reused. Not a Revit packet."}
    verify_source_records(records)
    if _code_digest() != code_before:
        raise ValueError("Core code changed during experiment")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(_bytes(report))
    print(json.dumps({key: checks[key] for key in ("status", "host_blocked_before", "host_blocked_after",
        "physical_bar_count", "additional_mass_kg", "changed_bar_count", "same_direction_body_pairs_after",
        "new_body_pairs")}, ensure_ascii=False), flush=True)
    print("Original FE coverage:", checks["source_coverage"]["status"], "stock:", checks["stock_cutting"]["status"], flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-dir", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--working-host-report", type=Path, required=True)
    parser.add_argument("--relocation-draft", type=Path, required=True)
    parser.add_argument("--relocation-review", type=Path, required=True)
    parser.add_argument("--reassign-lengths", action="store_true")
    parser.add_argument("--time-limit-s", type=float, default=60)
    parser.add_argument("--maximum-candidates-per-bar", type=int, default=96)
    parser.add_argument("--maximum-total-candidates", type=int, default=50000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        run(args)
    except (ValueError, TypeError, KeyError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
