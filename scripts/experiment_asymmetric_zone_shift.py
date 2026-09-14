"""Shift complete zones inside a real host, without clipping demand or bars.

Fresh DXF + immutable pipeline restore the SOURCE zones (1196 bars in the working
case), not the 902-bar normalized execution plan. Optional normalization builds a
new downstream report and rechecks the host; nothing is sent to Revit.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from correct_small_openings import _code_digest, _load_recovery
from rebar.application.analyze_composite_plate import CompositeDirectionSettings, _direction_candidate, _placements
from rebar.application.assistant_inputs import source_record, verify_source_records
from rebar.application.asymmetric_zone_shift import shift_composite_plate_to_host
from rebar.application.layout_snapshot import load_layout_snapshot
from rebar.application.physical_bar_trial import _revalidate_sources
from rebar.application.physical_host_recovery import fit_physical_layout_to_host
from rebar.application.physical_layout_recovery import _bytes, recover_physical_layout_from_report
from rebar.application.working_host import load_working_host_json
from rebar.application.working_solid_host import MAX_WORKING_REPORT_BYTES, inspect_working_solid
from rebar.optimization.contracts.composite_coverage import MONOTONE_SINGLE_STO_COVERAGE_POLICY
from rebar.optimization.contracts.composite_search import CompositeSearchProblem
from rebar.optimization.contracts.physical import PhysicalNormalizationConfig
from rebar.optimization.services.bar_schedule import build_bar_schedule, composite_schedule_groups
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.cutting import PLATE_11700_BATCH_PROFILE
from rebar.optimization.services.patterned_conflicts import check_patterned_same_plane_conflicts
from rebar.optimization.services.stock_cutting import check_stock_cutting
from rebar.reporting.serialization import to_jsonable


def verify_recovery_bindings(recovery):
    """Keep the original source→normalization→packet→read-review byte chain intact."""
    if recovery.packet is None or recovery.packet_bytes is None:
        raise ValueError("A complete original diagnostic physical pipeline is required")
    for value, content in ((recovery.patterned_report, recovery.patterned_report_bytes),
            (recovery.normalization_report, recovery.normalization_report_bytes),
            (recovery.packet, recovery.packet_bytes), (recovery.review, recovery.review_bytes)):
        if _bytes(value) != content:
            raise ValueError("Original pipeline dictionary differs from its bound bytes")
    source_sha = hashlib.sha256(recovery.patterned_report_bytes).hexdigest()
    normal_sha = hashlib.sha256(recovery.normalization_report_bytes).hexdigest()
    packet_sha = hashlib.sha256(recovery.packet_bytes).hexdigest()
    if (recovery.packet["source_report_sha256"] != source_sha
            or recovery.normalization_report["source_report_sha256"] != source_sha
            or recovery.packet["raw_report_sha256"] != normal_sha
            or recovery.review["packet_sha256"] != packet_sha):
        raise ValueError("Original pipeline source/normalization/packet hash chain differs")


def verify_recorded_policies(summary, recovery, host_sha):
    recorded = recovery.normalization_report
    if summary["normalization_config"] != recorded["configuration"]:
        raise ValueError("Recorded normalization configuration differs from pipeline summary")
    binding = recorded["host_fit"]
    if (binding["source_to_revit_xy_mm"] != [0, 0]
            or binding["source_host_report_sha256"] != host_sha):
        raise ValueError("Original working-host binding is not the same explicit identity XY")
    if summary["expected"] != recovery.packet["expected"]:
        raise ValueError("Original normalized comparison metrics differ from their packet")


def restore_zones(report, problem):
    """Revalidate the entire source first; preserve its exact asymmetric endpoints."""
    _revalidate_sources(report, problem, 0, hashlib.sha256(_bytes(report)).hexdigest())
    groups, settings = [], []
    for original, row, selected in zip(problem.direction_problems, report["directions"],
            report["front"][0]["direction_candidate_indexes"], strict=True):
        setting = CompositeDirectionSettings(**{**row["settings"], "direction": original.demand.direction})
        placement = dict(_placements(original.demand, setting))
        constraints = replace(original.constraints, cutting_profile=PLATE_11700_BATCH_PROFILE)
        candidate = next(c for c in row["candidates"] if c["candidate_index"] == selected)
        along = 0 if str(setting.direction).endswith("X") else 1
        zones = []
        for draft in candidate["zone_drafts"]:
            zone = build_composite_zone(original.demand, tuple(draft["demand_bbox_mm"]),
                draft["level_index"], draft["source_zone_id"], placement[draft["level_index"]],
                constraints=constraints, installed_lengths_mm=tuple(c["installed_length_mm"] for c in draft["components"]))
            zone = replace(zone, components=tuple(replace(c, longitudinal_interval_mm=(
                raw["bar_axis_bbox_mm"][along], raw["bar_axis_bbox_mm"][along+2]))
                for c, raw in zip(zone.components, draft["components"], strict=True)))
            zones.append(zone)
        groups.append(tuple(zones))
        settings.append(setting)
    return tuple(groups), tuple(settings)


def rebuild_report(source, problem, groups, settings):
    """Use existing coverage/export services; never carry an old geometry certificate."""
    started = perf_counter()
    report = deepcopy(source)
    for original, zones, setting, row in zip(problem.direction_problems, groups, settings, report["directions"], strict=True):
        constraints = replace(original.constraints, cutting_profile=PLATE_11700_BATCH_PROFILE)
        search = CompositeSearchProblem(original.demand, _placements(original.demand, setting),
            constraints, MONOTONE_SINGLE_STO_COVERAGE_POLICY)
        row["candidates"] = [_direction_candidate(search, zones, setting, 0)]
    schedule = build_bar_schedule(group for zones, setting in zip(groups, settings, strict=True)
        for group in composite_schedule_groups(zones, steel_class=setting.steel_class))
    stock = check_stock_cutting(schedule, time_limit_s=30)
    if stock["status"] != "pass":
        raise ValueError("Translated full source batch did not pass fresh stock verification")
    point = {"direction_candidate_indexes": [0]*4, "zone_count": sum(map(len, groups)),
        "position_count": len(schedule), "physical_bar_count": sum(p.physical_bar_count for p in schedule),
        "additional_mass_kg": math.fsum(p.total_mass_kg for p in schedule),
        "bar_schedule": to_jsonable(schedule), "stock_cutting": stock}
    prior = source["front"][0]
    for key in ("zone_count", "position_count", "physical_bar_count", "additional_mass_kg"):
        if not math.isclose(point[key], prior[key], rel_tol=0, abs_tol=1e-6):
            raise ValueError("Source translation changed complete batch metric: "+key)
    conflicts = check_patterned_same_plane_conflicts(tuple(z for group in groups for z in group),
        assume_same_depth_per_direction=True)
    report.update(front=[point], selected_index=0, diagnostic_front_before_cutting=[],
        length_balance_attempts=[], same_plane_conflicts=to_jsonable(conflicts),
        physical_placement_status="asymmetric_shift_research_host_and_3d_not_accepted",
        status="full_coverage_candidates_found", host_envelope=None,
        front_scope="translated_complete_source_with_fresh_checks_not_engineering_acceptance",
        runtime_ms=(perf_counter()-started)*1000)
    # Keep unresolved engineering assumptions. Never advertise a stale host pass.
    report["blocking_check_ids"] = sorted(set(report["blocking_check_ids"]) |
        {"working-solid-host-full-placement-review", "asymmetric-zone-shift-research"})
    _revalidate_sources(report, problem, 0, hashlib.sha256(_bytes(report)).hexdigest())
    return report


def run(args):
    if args.output_dir.exists():
        raise ValueError("Output directory exists; choose a NEW directory")
    if args.confirm_identity_xy is not True:
        raise ValueError("Explicit --confirm-identity-xy required; no inferred alignment")
    if (isinstance(args.maximum_transverse_shift_mm, bool) or not math.isfinite(args.maximum_transverse_shift_mm)
            or not 0 <= args.maximum_transverse_shift_mm <= 1200):
        raise ValueError("Transverse shift must be finite in 0..1200 mm")
    if (type(args.maximum_candidates_per_zone) is not int or not 1 <= args.maximum_candidates_per_zone <= 128
            or type(args.maximum_passes) is not int or not 1 <= args.maximum_passes <= 8):
        raise ValueError("Candidate/pass resource limits are invalid")
    start, code = perf_counter(), _code_digest()
    script_record = source_record(Path(__file__), role="experiment-code")
    loaded = load_layout_snapshot(args.snapshot, candidate_id=args.candidate_id)
    recovery, summary, records = _load_recovery(args, loaded)
    verify_recovery_bindings(recovery)
    snap = source_record(args.snapshot, role="source-snapshot")
    host_record = source_record(args.working_host_report, role="working-host-snapshot")
    if snap["sha256"] != loaded.source_sha256 or host_record["sha256"] != summary["working_host_source"]["sha256"]:
        raise ValueError("Snapshot or working host differs from verified pipeline")
    verify_recorded_policies(summary, recovery, host_record["sha256"])
    records.extend((script_record, snap, host_record))
    with args.working_host_report.open("rb") as stream:
        content = stream.read(MAX_WORKING_REPORT_BYTES+1)
    if len(content) > MAX_WORKING_REPORT_BYTES or hashlib.sha256(content).hexdigest() != host_record["sha256"]:
        raise ValueError("Host changed or exceeded input limit")
    host_json = load_working_host_json(content, maximum_bytes=MAX_WORKING_REPORT_BYTES)
    host, _ = inspect_working_solid(host_json)
    groups, settings = restore_zones(recovery.patterned_report, loaded.problem)
    print("Restored complete original zones:", sum(map(len, groups)), flush=True)
    moved = shift_composite_plate_to_host(loaded.problem, groups, settings, host,
        maximum_transverse_shift_mm=args.maximum_transverse_shift_mm,
        maximum_candidates_per_zone=args.maximum_candidates_per_zone, maximum_passes=args.maximum_passes)
    changed = rebuild_report(recovery.patterned_report, loaded.problem, moved.direction_zones, settings)
    changed["zone_translation"] = {"source_patterned_report_sha256": hashlib.sha256(recovery.patterned_report_bytes).hexdigest(),
        "method": "asymmetric_fixed_phase_fixed_inventory_zone_shift",
        "changed_zones": moved.report.get("changed_zones", []), "placement_eligible": False,
        "source_demand_preserved": True}
    outputs = {"shifted-patterned-analysis.json": changed}
    normalized = None
    if args.normalize:
        # Reuse the recorded experiment policy, not a new diameter/mass relaxation.
        print("Recomputing normalization with the ORIGINAL recorded policy", flush=True)
        config = PhysicalNormalizationConfig(**summary["normalization_config"])
        rebuilt = recover_physical_layout_from_report(changed, loaded.problem, normalization_config=config,
            candidate_index=0, stock_time_limit_s=30)
        if rebuilt.packet is not None:
            fitted = fit_physical_layout_to_host(rebuilt, loaded.problem, host_json,
                offset_x_mm=0, offset_y_mm=0, binding_source="Explicit identity XY; immutable original working-host binding",
                conservative_whole_height=True, source_report_sha256=host_record["sha256"], stock_time_limit_s=30)
            rebuilt = fitted.recovery
            outputs["normalized-host-review.json"] = fitted.host_review
        outputs.update({"normalized-physical-review.json": rebuilt.review,
                        "normalization-report.json": rebuilt.normalization_report})
        # Do not package a newly normalized trial as a verified Revit delivery.
        normalized = {"status": rebuilt.status, "expected": None if rebuilt.packet is None else rebuilt.packet["expected"],
            "host_fit": rebuilt.normalization_report.get("host_fit"), "placement_eligible": False,
            "baseline_before_small_opening_relocation": {"expected": summary["expected"],
                "working_host_fit": summary["working_host_fit"]}}
    report = {"schema_version": "asymmetric-zone-shift-experiment/v1", "placement_eligible": False,
        "engineering_approval": False, "source_demand_removed": False, "source_demand_values_changed": False,
        "source_to_revit_xy_mm": [0, 0], "case_id": loaded.problem.case_id,
        "source_files": records, "code_sha256": code, "zone_shift": moved.report,
        "source_batch_before": recovery.patterned_report["front"][0], "source_batch_after": changed["front"][0],
        "normalized": normalized, "runtime_s": perf_counter()-start,
        "comparison_scope": "Source parametric zones and normalized physical execution are DIFFERENT count levels; compare each with its own baseline",
        "not_checked": ["normative_edge_anchorage", "actual_Z_background_and_XY_collisions", "Revit_readback", "engineering_acceptance"]}
    outputs["experiment.json"] = report
    verify_source_records(records)
    if _code_digest() != code:
        raise ValueError("Code changed during experiment; rerun the immutable source set")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    for name, value in outputs.items():
        with (args.output_dir/name).open("xb") as stream:
            stream.write(_bytes(value))
    print(json.dumps({"output": str(args.output_dir), "zone_shift": moved.report.get("status"),
        "normalized": None if normalized is None else normalized["status"]}, ensure_ascii=False), flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("pipeline-dir", "snapshot", "working-host-report", "output-dir"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--confirm-identity-xy", action="store_true")
    parser.add_argument("--maximum-transverse-shift-mm", type=float, default=600)
    parser.add_argument("--maximum-candidates-per-zone", type=int, default=32)
    parser.add_argument("--maximum-passes", type=int, default=2)
    parser.add_argument("--normalize", action="store_true")
    try:
        run(parser.parse_args(argv))
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.error(str(error))
    # Successful research, never an engineering/placement gate pass.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
