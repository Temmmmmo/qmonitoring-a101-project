"""One command: four DXF (or a verified snapshot) -> physical plan -> safe pyRevit ZIP.

All geometric operations run in reusable application/core services. No research
artifact, preselected bar ID or saved accepted normalization is required.
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

from rebar.application.analyze_composite_plate import CompositeDirectionSettings
from rebar.application.analyze_plate import PlateDirectionSource
from rebar.application.assistant_inputs import (
    AssistantSourceSelection, analyze_assistant_case, analyze_assistant_sources,
    assistant_genetic_config, source_record, verify_source_records,
)
from rebar.application.layout_snapshot import CASE_MAPPINGS, load_layout_snapshot
from rebar.optimization.contracts.physical import PhysicalNormalizationConfig
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS
from rebar.reporting.serialization import to_jsonable
from rebar.reporting.assistant_handoff import render_assistant_handoff


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")


def _code_digest() -> str:
    digest = hashlib.sha256()
    paths = [*sorted((ROOT / "src").rglob("*.py")),
             *sorted((ROOT / "integrations/pyrevit/QMonitoring.extension").rglob("*.py")),
             Path(__file__).resolve(), ROOT / "scripts/package_revit_physical_trial.py",
             ROOT / "scripts/package_revit_plate_trial.py", ROOT / "scripts/package_revit_probe.py"]
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--case", choices=tuple(CASE_MAPPINGS), help="Verified local DXF/PDF case")
    source.add_argument("--dxf", type=Path, nargs=4, help="Four distinct DXF; directions checked from input")
    source.add_argument("--snapshot", type=Path, help="Strict original-demand GA snapshot; normalization runs anew")
    legend = parser.add_mutually_exclusive_group()
    legend.add_argument("--mapping", help="Explicit mapping for all four --dxf files")
    legend.add_argument("--shk", type=Path, nargs="+", help="One shared SHK or four paired with --dxf")
    parser.add_argument("--case-id", help="Required label for custom --dxf, not an inferred reference")
    parser.add_argument("--materials-root", type=Path, default=ROOT / "Дополнительные материалы")
    parser.add_argument("--candidate-id", help="Only for --snapshot; otherwise selection is automatic")
    parser.add_argument("--source-selection", choices=("auto", "minimum_mass", "minimum_mass_without_extra_bars"),
        default="auto", help="auto uses engineer bar limit for known cases/snapshots, minimum mass for custom DXF")
    parser.add_argument("--maximum-source-bars", type=int, help="Optional UNIFORM starting-point limit, not final gate")
    parser.add_argument("--maximum-source-mass-kg", type=float, help="Optional UNIFORM starting-point limit")
    parser.add_argument("--population", type=int, default=8)
    parser.add_argument("--generations", type=int, default=3)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--background-origin-mm", type=float, required=True)
    parser.add_argument("--first-300-offset-mm", type=float, required=True)
    parser.add_argument("--contact-side", choices=("left", "right"), required=True)
    parser.add_argument("--steel-class", required=True)
    parser.add_argument("--phase-source", required=True, help="Explicit source of phase assumption, not engineering approval")
    parser.add_argument("--allow-diameter-increase", action="store_true",
        help="Explicit opt-in to stronger-diameter consolidation with NEW diameter 40d/contact checks")
    parser.add_argument("--normalization-time-limit-s", type=float, default=120)
    parser.add_argument("--stock-time-limit-s", type=float, default=30)
    parser.add_argument("--maximum-exchange-attempts", type=int, default=300)
    parser.add_argument("--working-host-report", type=Path, help="Exact read-only working Revit host snapshot")
    parser.add_argument("--host-offset-x-mm", type=float)
    parser.add_argument("--host-offset-y-mm", type=float)
    parser.add_argument("--host-binding-source", help="Explicit DXF-to-host binding evidence or hypothesis")
    vertical = parser.add_mutually_exclusive_group()
    vertical.add_argument("--host-axis-depths-mm", type=float, nargs=4,
        metavar=("BOTTOM_X", "BOTTOM_Y", "TOP_X", "TOP_Y"))
    vertical.add_argument("--host-conservative-whole-height", action="store_true",
        help="Explicit conservative XY fit through every slab section, NOT actual Z approval")
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def _validate_args(args) -> None:
    if args.output_dir.exists():
        raise ValueError("Output directory already exists; use a new directory, existing results are never replaced")
    assistant_genetic_config(population=args.population, generations=args.generations, seed=args.seed)
    if args.candidate_id is not None and args.snapshot is None:
        raise ValueError("--candidate-id is only supported with --snapshot")
    if args.dxf:
        if not args.case_id or (args.mapping is None and args.shk is None):
            raise ValueError("Custom --dxf requires --case-id and an explicit --mapping or --shk")
        if args.shk is not None and len(args.shk) not in (1, 4):
            raise ValueError("Provide one common SHK or four SHK paired with the four DXF")
        if args.source_selection == "minimum_mass_without_extra_bars":
            raise ValueError("Custom DXF have no matched engineer count; use --maximum-source-bars explicitly")
    elif args.mapping is not None or args.shk is not None or args.case_id is not None:
        raise ValueError("Mapping/SHK/case-id options apply only to custom --dxf")
    if args.snapshot and (args.maximum_source_bars is not None or args.maximum_source_mass_kg is not None):
        raise ValueError("Snapshot mode uses --source-selection or --candidate-id; custom source limits apply to fresh GA")
    if args.snapshot and (args.population, args.generations, args.seed) != (8, 3, 7):
        raise ValueError("Snapshot mode does not rerun GA; population/generations/seed apply to fresh DXF only")
    if args.candidate_id is not None and args.source_selection != "auto":
        raise ValueError("Choose either --candidate-id or an explicit --source-selection")
    if args.maximum_source_bars is not None and args.maximum_source_bars < 1:
        raise ValueError("maximum source bars must be positive")
    for value, low, high, name in (
        (args.background_origin_mm, -1e8, 1e8, "background-origin-mm"),
        (args.first_300_offset_mm, 0.001, 299.999, "first-300-offset-mm"),
        (args.normalization_time_limit_s, 0.001, 600, "normalization-time-limit-s"),
        (args.stock_time_limit_s, 0.01, 60, "stock-time-limit-s"),
    ):
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{name} must be finite in {low}..{high}")
    if args.maximum_source_mass_kg is not None and (
            not math.isfinite(args.maximum_source_mass_kg) or args.maximum_source_mass_kg <= 0):
        raise ValueError("Maximum source mass must be finite and positive")
    if not 0 <= args.maximum_exchange_attempts <= 300:
        raise ValueError("maximum-exchange-attempts must be in 0..300")
    if not args.steel_class.strip() or not args.phase_source.strip():
        raise ValueError("Explicit nonempty steel class and phase source are required")
    host_fields = (args.host_offset_x_mm, args.host_offset_y_mm, args.host_binding_source)
    if args.working_host_report is None:
        if any(v is not None for v in (*host_fields, args.host_axis_depths_mm)) or args.host_conservative_whole_height:
            raise ValueError("Host placement options require --working-host-report")
    elif (any(v is None for v in host_fields) or not args.host_binding_source.strip()
          or ((args.host_axis_depths_mm is not None) == args.host_conservative_whole_height)):
        raise ValueError("Working host requires explicit XY, binding source and four depths OR whole-height mode")
    if args.working_host_report is not None:
        if any(not math.isfinite(v) or abs(v) > 1e8 for v in host_fields[:2]):
            raise ValueError("Host offsets must be finite millimetres")
        if args.host_axis_depths_mm and any(not math.isfinite(v) or not 1 <= v <= 10000 for v in args.host_axis_depths_mm):
            raise ValueError("Host axis depths must be finite in 1..10000 mm")


def _load_sources(args) -> AssistantSourceSelection:
    config = assistant_genetic_config(population=args.population, generations=args.generations, seed=args.seed)
    preference = args.source_selection
    if preference == "auto":
        preference = "minimum_mass_without_extra_bars" if args.case or args.snapshot else "minimum_mass"
    if args.snapshot:
        loaded = load_layout_snapshot(args.snapshot, candidate_id=args.candidate_id,
            selection=None if args.candidate_id is not None else preference)
        records = [{**row, "role": "dxf"} for row in loaded.snapshot["source_dxf"]]
        records += [{**loaded.snapshot["source_pdf"], "role": "engineer-pdf"},
                    source_record(args.snapshot, role="source-snapshot")]
        return AssistantSourceSelection(loaded.problem, loaded.solution,
            {"schema_version": "assistant-source-selection/v1", "mode": "verified-snapshot",
             "source_files": records, "candidate_id": loaded.snapshot["candidate_id"],
             "snapshot": loaded.snapshot, "snapshot_sha256": loaded.source_sha256,
             "source_metrics": to_jsonable(loaded.solution.metrics), "placement_eligible": False},
            loaded.engineer_comparison)
    if args.case:
        return analyze_assistant_case(args.case, args.materials_root, config=config,
            maximum_source_bars=args.maximum_source_bars, maximum_source_mass_kg=args.maximum_source_mass_kg,
            use_engineer_bar_limit=preference == "minimum_mass_without_extra_bars")
    shk = ([args.shk[0]] * 4 if len(args.shk) == 1 else args.shk) if args.shk else [None] * 4
    return analyze_assistant_sources(tuple(PlateDirectionSource(path, shk_path=legend,
        mapping_id=args.mapping or "auto") for path, legend in zip(args.dxf, shk)),
        case_id=args.case_id, config=config, maximum_source_bars=args.maximum_source_bars,
        maximum_source_mass_kg=args.maximum_source_mass_kg)


def run_pipeline(args) -> dict:
    """Write only fully verified new outputs; expected limitations stay explicit."""
    from rebar.application.physical_layout_recovery import recover_physical_layout

    _validate_args(args)
    started, code_before = perf_counter(), _code_digest()
    host_snapshot, host_record = None, None
    if args.working_host_report is not None:
        from rebar.application.working_host import load_working_host_json
        from rebar.application.working_solid_host import MAX_WORKING_REPORT_BYTES, inspect_working_solid
        with args.working_host_report.open("rb") as stream:
            host_bytes = stream.read(MAX_WORKING_REPORT_BYTES + 1)
        host_snapshot = load_working_host_json(host_bytes, maximum_bytes=MAX_WORKING_REPORT_BYTES)
        inspect_working_solid(host_snapshot)
        host_record = source_record(args.working_host_report, role="working-host-snapshot")
        if host_record["sha256"] != hashlib.sha256(host_bytes).hexdigest():
            raise ValueError("Working host snapshot changed while reading")
    print("1/4: Read original sources and select a complete starting layout", flush=True)
    source = _load_sources(args)
    configs = tuple(CompositeDirectionSettings(direction, args.background_origin_mm,
        args.first_300_offset_mm, 0.0, args.steel_class, args.phase_source, args.contact_side)
        for direction in PLATE_DIRECTIONS)
    normalization_config = PhysicalNormalizationConfig(allow_diameter_increase=args.allow_diameter_increase,
        time_limit_s=args.normalization_time_limit_s, stock_balance_time_limit_s=args.stock_time_limit_s,
        maximum_exchange_attempts=args.maximum_exchange_attempts)
    provenance = {**source.provenance, "code_sha256": code_before}
    print("2/4: Exact STO axes, source recovery, physical consolidation and whole-batch stock", flush=True)
    result = recover_physical_layout(source.problem, source.solution, configs,
        normalization_config=normalization_config, source_provenance=provenance,
        balance_time_limit_s=min(20.0, args.stock_time_limit_s),
        stock_time_limit_s=args.stock_time_limit_s)
    host_result = None
    if host_snapshot is not None and result.packet is not None:
        from rebar.application.physical_host_recovery import fit_physical_layout_to_host
        depths = dict(zip(map(str, PLATE_DIRECTIONS), args.host_axis_depths_mm)) if args.host_axis_depths_mm else None
        print("2b/4: Fit surplus length to actual host without dropping demand, then revalidate the whole plan", flush=True)
        host_result = fit_physical_layout_to_host(result, source.problem, host_snapshot,
            offset_x_mm=args.host_offset_x_mm, offset_y_mm=args.host_offset_y_mm,
            binding_source=args.host_binding_source, axis_depths_mm=depths,
            conservative_whole_height=args.host_conservative_whole_height,
            source_report_sha256=host_record["sha256"], stock_time_limit_s=args.stock_time_limit_s)
        result = host_result.recovery
    print("3/4: Revalidate geometry, source hashes, counts, mass and unresolved pairs", flush=True)
    verify_source_records(source.provenance["source_files"])
    if host_record is not None:
        verify_source_records([host_record])
    if code_before != _code_digest():
        raise ValueError("Code changed during the pipeline; rerun on a stable working tree")
    summary = {"schema_version": "revit-assistant-pipeline/v1", "status": result.status,
        "placement_eligible": False, "case_id": source.problem.case_id,
        "candidate_id": source.provenance["candidate_id"], "code_sha256": code_before,
        "source_provenance": provenance, "normalization_config": to_jsonable(normalization_config),
        "runtime_s": perf_counter() - started,
        "scope": "automatic full research pipeline; unresolved joints/host/Z are not engineering approval"}
    files = {"patterned-analysis.json": result.patterned_report_bytes,
             "physical-normalization.json": result.normalization_report_bytes,
             "source-layout.json": _json_bytes({"schema_version": "assistant-source-layout/v1",
                 "placement_eligible": False, "source_provenance": provenance,
                 "problem": to_jsonable(source.problem), "solution": to_jsonable(source.solution)})}
    if host_record is not None:
        summary["working_host_source"] = host_record
    if host_result is not None:
        files["working-host-fit.json"] = host_result.host_review_bytes
        summary["working_host_fit"] = {key: host_result.host_review[key] for key in (
            "blocked_before", "blocked_after", "accepted_moved_bar_count", "check_criterion", "proposal_accepted")}
    if result.packet is not None:
        sys.path.insert(0, str(ROOT / "integrations/pyrevit/QMonitoring.extension/lib"))
        from qm_physical_packet import VERSION, validate_packet
        from package_revit_physical_trial import _readme, _validate_review, build_package

        validate_packet(result.packet)
        packet, review = result.packet, dict(result.review)
        comparison = None
        if source.engineer_reference is not None:
            engineer = source.engineer_reference
            comparison = {"mass_kg": engineer["mass_kg"], "physical_bar_count": engineer["physical_bar_count"],
                "specification_rows": engineer["specification_rows"],
                "mass_delta_pct": (packet["expected"]["additional_mass_kg"] / engineer["mass_kg"] - 1) * 100,
                "bar_delta_pct": (packet["expected"]["physical_bar_count"] / engineer["physical_bar_count"] - 1) * 100,
                "mass_threshold_15pct_met": packet["expected"]["additional_mass_kg"] <= 1.15 * engineer["mass_kg"],
                "scope": "matched additional reinforcement; not all gates; typologies are not PDF rows"}
            review["engineer_comparison"] = comparison
        review["pipeline"] = {"code_sha256": code_before, "source_provenance": provenance,
                              "status": result.status, "automatic_normalization": True}
        review_content = _json_bytes(review)
        _validate_review(review_content, packet, result.packet_bytes)
        summary.update(expected=packet["expected"], engineer_comparison=comparison,
            unresolved_intersection_pair_count=len(packet["manual_joint_tasks"]),
            normalization_status=result.normalization_report.get("status"),
            packet_sha256=hashlib.sha256(result.packet_bytes).hexdigest())
        files.update({"physical-bar-plan-trial.json": result.packet_bytes,
                      "engineer-review.json": review_content, "README.md": _readme(packet, VERSION)})
    else:
        files["engineer-review.json"] = result.review_bytes
        files["README.md"] = ("# Диагностика автоматического расчёта\n\n"
            "Пакет Revit не создан: " + result.status + ".\n"
            "Полные причины находятся в patterned-analysis.json и physical-normalization.json.\n"
            "Неполная партия не выдаётся за готовую. Исходные требования не понижались.\n").encode("utf-8")
    files["RESULT.md"] = render_assistant_handoff(summary, review if result.packet is not None else result.review,
        host_result.host_review if host_result else None).encode("utf-8")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        if content is not None:
            with (args.output_dir / name).open("xb") as stream:
                stream.write(content)
    if result.packet is not None:
        print("4/4: Package the checked plan and review with the unchanged rollback-only pyRevit runtime", flush=True)
        archive = build_package(args.output_dir / f"qmonitoring-physical-plan-trial-{VERSION}.zip",
            args.output_dir / "physical-bar-plan-trial.json", review_path=args.output_dir / "engineer-review.json")
        summary["archive"] = str(archive)
        summary["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    with (args.output_dir / "pipeline-summary.json").open("xb") as stream:
        stream.write(_json_bytes(summary))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        _validate_args(args)
        summary = run_pipeline(args)
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False), flush=True)
    return 0 if "archive" in summary and not summary["status"].startswith("blocked_") else 2


if __name__ == "__main__":
    raise SystemExit(main())
