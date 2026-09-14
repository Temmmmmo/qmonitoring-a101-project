"""Combine freshly checked zone translations, small-hole bypass and FE host repair.

Restores the NEW complete shifted source, not an old unchanged-axis certificate.
Original DXF demand and the entire physical inventory are retained. The output is
research-only JSON, not a Revit packet or a permanent-placement authorization.
"""
from __future__ import annotations

import argparse
import hashlib
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
from rebar.application.layout_snapshot import load_layout_snapshot
from rebar.application.opening_relocation import source_service_lanes
from rebar.application.physical_bar_trial import build_physical_bar_trial
from rebar.application.physical_layout_recovery import _bytes, _raw
from rebar.application.working_host import load_working_host_json
from rebar.application.working_solid_host import MAX_WORKING_REPORT_BYTES, inspect_working_solid
from rebar.optimization.algorithms.fe_host_repair import solve_fe_host_repair
from rebar.optimization.algorithms.opening_relocation import relocate_small_openings
from rebar.optimization.contracts.opening_relocation import OpeningRelocationConfig
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS
from rebar.optimization.services.fe_host_repair import check_fe_host_repair, derive_fe_obligations
from rebar.optimization.services.opening_relocation import check_relocation
from rebar.reporting.serialization import to_jsonable

INPUT_NAMES = ("experiment.json", "shifted-patterned-analysis.json",
    "normalization-report.json", "normalized-physical-review.json")


def _same(first, second):
    # Type-sensitive numeric/bool comparison without depending on dict key order.
    return json.dumps(first, sort_keys=True, allow_nan=False) == json.dumps(second, sort_keys=True, allow_nan=False)


def _validate_args(args):
    if args.output.exists():
        raise ValueError("Existing output must not be overwritten; choose a NEW file")
    if args.confirm_identity_xy is not True:
        raise ValueError("Explicit --confirm-identity-xy required")
    if not args.shifted_dir.is_dir():
        raise ValueError("Existing complete shifted experiment directory required")
    for label, value, ceiling in (("opening_time_limit_s", args.opening_time_limit_s, 600),
        ("fe_time_limit_s", args.fe_time_limit_s, 600), ("stock_time_limit_s", args.stock_time_limit_s, 60)):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0.001 <= value <= ceiling):
            raise ValueError(f"{label} must be finite in 0.001..{ceiling}")
    for label, value, ceiling in (("maximum_fe_candidates_per_bar", args.maximum_fe_candidates_per_bar, 4096),
        ("maximum_total_fe_candidates", args.maximum_total_fe_candidates, 250000)):
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError(f"{label} must be an integer in 1..{ceiling}")


def restore_shifted_inputs(args, loaded, host_record):
    """Bind exact new source→normalization→fresh packet→review, plus original files."""
    files = {name: _read(args.shifted_dir/name) for name in INPUT_NAMES}
    experiment, source, normalization, review = (files[name][0] for name in INPUT_NAMES)
    schemas = ("asymmetric-zone-shift-experiment/v1", "composite-plate-analysis/v1",
        "physical-layout-normalization/v1", "physical-bar-plan-review/v1")
    for value, schema in zip((experiment, source, normalization, review), schemas, strict=True):
        if value["schema_version"] != schema or value["placement_eligible"] is not False:
            raise ValueError("Exact research input schema and literal false placement permissions required")
    if (any(value["engineering_approval"] is not False for value in (experiment, normalization, review))
            or experiment["source_demand_removed"] is not False
            or experiment["source_demand_values_changed"] is not False
            or normalization["source_demand_removed"] is not False
            or source["source_demand_preserved"] is not True or review["source_demand_preserved"] is not True
            or any(value["units"] != "mm" for value in (source, normalization, review))
            or not _same(experiment["source_to_revit_xy_mm"], [0, 0])):
        raise ValueError("Unchanged original demand, mm and explicit identity XY are required")
    if (experiment["case_id"] != loaded.problem.case_id or source["case_id"] != loaded.problem.case_id
            or loaded.snapshot["candidate_id"] != args.candidate_id):
        raise ValueError("Shifted source differs from the freshly restored case/candidate")
    records = list(experiment["source_files"])
    verify_source_records(records)
    expected = {(str(Path(row["path"]).resolve()), row["sha256"])
        for row in (*loaded.snapshot["source_dxf"], loaded.snapshot["source_pdf"])}
    present = {(str(Path(row["path"]).resolve()), row["sha256"])
        for row in records if row["role"] in ("dxf", "engineer-pdf")}
    if present != expected:
        raise ValueError("Shifted experiment DXF/engineer inputs differ from the fresh snapshot")
    for record in records:
        if record["role"] == "source-snapshot" and record["sha256"] != loaded.source_sha256:
            raise ValueError("Recorded original source snapshot SHA differs")
        if record["role"] == "working-host-snapshot" and record["sha256"] != host_record["sha256"]:
            raise ValueError("Recorded working host SHA differs")
    provenance = source["source_provenance"]
    if (not _same(normalization["source_provenance"], provenance)
            or not _same(review["source_provenance"], provenance)
            or provenance["candidate_id"] != args.candidate_id
            or provenance["snapshot_sha256"] != loaded.source_sha256):
        raise ValueError("Shifted source/normalization/review provenance differs")
    source_sha = hashlib.sha256(files["shifted-patterned-analysis.json"][1]).hexdigest()
    normal_sha = hashlib.sha256(files["normalization-report.json"][1]).hexdigest()
    if (normalization["source_report_sha256"] != source_sha or review["source_report_sha256"] != source_sha
            or review["raw_report_sha256"] != normal_sha):
        raise ValueError("Exact shifted source/normalization/review SHA chain differs")
    translated = source["zone_translation"]
    original_sources = [record for record in records if record["role"] == "pipeline-artifact"
        and Path(record["path"]).name == "patterned-analysis.json"]
    if (len(original_sources) != 1 or translated["source_patterned_report_sha256"] != original_sources[0]["sha256"]
            or translated["placement_eligible"] is not False or translated["source_demand_preserved"] is not True
            or translated["method"] != "asymmetric_fixed_phase_fixed_inventory_zone_shift"
            or not _same(experiment["source_batch_after"], source["front"][0])):
        raise ValueError("Complete source translation does not bind its original source and batch")
    binding = normalization["host_fit"]
    if (not _same(binding["source_to_revit_xy_mm"], [0, 0])
            or binding["source_host_report_sha256"] != host_record["sha256"]
            or not _same(experiment["normalized"]["host_fit"], binding)
            or not _same(review["host_fit"], binding)):
        raise ValueError("Normalized shifted plan requires its original exact host/identity XY")
    raw = normalization["accepted"]["raw_bars_by_direction"]
    bars = decode_bars(raw)
    fresh = build_physical_bar_trial(source, raw, original_problem=loaded.problem,
        source_report_sha256=source_sha, raw_report_sha256=normal_sha,
        stock_time_limit_s=args.stock_time_limit_s)
    # Existing host recovery only adds diagnostic blockers to its packet. Rebuild
    # those known annotations for byte-chain comparison, never load/reuse a packet.
    bound_packet = dict(fresh.packet)
    blockers = set(fresh.packet["source_blockers"])
    if binding["blocked_after"]:
        blockers.add("working-host-fit-incomplete")
    if normalization.get("normalization_mass_limit", {}).get("status") == "fail":
        blockers.add("normalization-mass-target-not-met")
    bound_packet["source_blockers"] = sorted(blockers)
    if (not _same(fresh.packet["expected"], review["expected"])
            or not _same(fresh.packet["expected"], experiment["normalized"]["expected"])
            or hashlib.sha256(_bytes(bound_packet)).hexdigest() != review["packet_sha256"]
            or not _same(bound_packet["source_blockers"], review["permanent_blockers"])
            or not _same(fresh.review["source_original_coverage"], review["source_original_coverage"])
            or fresh.review["stock_cutting"]["status"] != "pass"):
        raise ValueError("Normalized shifted expected counts/coverage/packet do not reproduce independently")
    coverage = fresh.review["source_original_coverage"]
    if (len(coverage) != 4 or {row["direction"] for row in coverage} != set(map(str, PLATE_DIRECTIONS))
            or any(row["status"] != "pass" or row["uncovered_cell_count"] != 0 for row in coverage)
            or fresh.packet["expected"]["physical_bar_count"] != len(bars)):
        raise ValueError("Fresh complete four-direction source coverage and physical count required")
    return source, bars, fresh, [*records, *(files[name][2] for name in INPUT_NAMES)]


def run(args):
    _validate_args(args)
    started, code_before = perf_counter(), _code_digest()
    script_record = source_record(Path(__file__), role="experiment-code")
    # decode_bars is reused from a research CLI; bind its file as well as core code.
    decoder_record = source_record(ROOT/"scripts/experiment_fe_host_repair.py", role="experiment-helper-code")
    loaded = load_layout_snapshot(args.snapshot, candidate_id=args.candidate_id)
    snapshot_record = source_record(args.snapshot, role="source-snapshot")
    if snapshot_record["sha256"] != loaded.source_sha256:
        raise ValueError("Snapshot changed after fresh restoration")
    host_record = source_record(args.working_host_report, role="working-host-snapshot")
    print("1/3: Independently restore the NEW shifted source and complete normalized plan", flush=True)
    source, bars, fresh, records = restore_shifted_inputs(args, loaded, host_record)
    records.extend((script_record, decoder_record, snapshot_record, host_record))
    with args.working_host_report.open("rb") as stream:
        content = stream.read(MAX_WORKING_REPORT_BYTES+1)
    if len(content) > MAX_WORKING_REPORT_BYTES or hashlib.sha256(content).hexdigest() != host_record["sha256"]:
        raise ValueError("Host snapshot exceeds budget or changed while reading")
    host, _ = inspect_working_solid(load_working_host_json(content, maximum_bytes=MAX_WORKING_REPORT_BYTES))
    lanes = source_service_lanes(source, loaded.problem)
    opening_config = OpeningRelocationConfig(maximum_candidates_per_bar=128,
        maximum_total_candidates=20000, time_limit_s=args.opening_time_limit_s)
    print("2/3: Joint small-hole bypass of the complete shifted plan", flush=True)
    opened = relocate_small_openings(bars, lanes, loaded.problem, host, config=opening_config)
    opening_checks = check_relocation(bars, opened.bars, lanes, loaded.problem, host,
        maximum_shift_mm=opening_config.maximum_shift_mm)
    obligations = derive_fe_obligations(opened.bars, lanes, loaded.problem)
    print("3/3: Joint frozen-FE host repair with unchanged whole-batch length inventory", flush=True)
    result, telemetry = solve_fe_host_repair(opened.bars, obligations, host, reassign_lengths=True,
        time_limit_s=args.fe_time_limit_s, maximum_candidates_per_bar=args.maximum_fe_candidates_per_bar,
        maximum_total_candidates=args.maximum_total_fe_candidates)
    checks = check_fe_host_repair(opened.bars, result, lanes, loaded.problem, host,
        stock_time_limit_s=args.stock_time_limit_s)
    if len(result) != len(bars) or checks["physical_bar_count"] != len(bars):
        raise ValueError("Combined repair changed the complete physical bar count")
    report = {"schema_version": "shifted-physical-repair-experiment/v1", "units": "mm",
        "placement_eligible": False, "structural_placement_supported": False, "engineering_approval": False,
        "source_demand_removed": False, "source_demand_values_changed": False,
        "source_to_revit_xy_mm": [0, 0], "source_host_report_sha256": host_record["sha256"],
        "case_id": loaded.problem.case_id, "candidate_id": args.candidate_id,
        "source_files": records, "code_sha256": code_before,
        "stages": {"restored_shifted_normalized": {"expected": fresh.packet["expected"],
            "source_original_coverage": fresh.review["source_original_coverage"],
            "stock_cutting": fresh.review["stock_cutting"], "new_source_certificate_checked": True},
            "small_openings": {"configuration": to_jsonable(opening_config), "checks": opening_checks,
                "search": opened.review.get("search", {})},
            "fe_host_repair": {"configuration": {"reassign_lengths": True,
                "maximum_candidates_per_bar": args.maximum_fe_candidates_per_bar,
                "maximum_total_candidates": args.maximum_total_fe_candidates,
                "time_limit_s": args.fe_time_limit_s}, "search": telemetry}},
        "checks": checks, "raw_bars_by_direction": _raw(result), "runtime_s": perf_counter()-started,
        "warning": "Combined new-source research only. Old unchanged-axis certificates are NOT reused. Not a Revit packet.",
        "not_checked": ["normative_edge_anchorage", "actual_Z_and_existing_XY_reinforcement",
            "Revit_readback", "engineering_acceptance"]}
    verify_source_records(records)
    if _code_digest() != code_before:
        raise ValueError("Core code changed during combined experiment")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(_bytes(report))
    print(json.dumps({"before_small_openings": opening_checks["host_blocked_before"],
        "after_small_openings": opening_checks["host_blocked_after"], "after_fe_repair": checks["host_blocked_after"],
        "physical_bar_count": checks["physical_bar_count"], "status": checks["status"]}), flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("shifted-dir", "snapshot", "working-host-report", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--confirm-identity-xy", action="store_true")
    parser.add_argument("--opening-time-limit-s", type=float, default=60)
    parser.add_argument("--fe-time-limit-s", type=float, default=60)
    parser.add_argument("--stock-time-limit-s", type=float, default=30)
    parser.add_argument("--maximum-fe-candidates-per-bar", type=int, default=512)
    parser.add_argument("--maximum-total-fe-candidates", type=int, default=50000)
    try:
        run(parser.parse_args(argv))
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
