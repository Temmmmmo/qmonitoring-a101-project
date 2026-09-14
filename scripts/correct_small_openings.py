"""Correct a full physical plan around small holes; write a NEW graphic-only draft.

The original pipeline and legacy Revit packet remain immutable. Fresh source DXF
are re-ingested by the snapshot loader; every input is checked again before output.
This command never starts Revit and never authorizes structural placement.
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

from rebar.application.assistant_inputs import source_record, verify_source_records
from rebar.application.layout_snapshot import SELECTIONS, load_layout_snapshot
from rebar.application.opening_relocation import correct_small_openings
from rebar.application.physical_layout_recovery import PhysicalLayoutRecoveryResult, _bytes
from rebar.application.working_host import load_working_host_json
from rebar.application.working_solid_host import MAX_WORKING_REPORT_BYTES, inspect_working_solid
from rebar.optimization.contracts.opening_relocation import OpeningRelocationConfig
from rebar.reporting.serialization import to_jsonable

MAX_PIPELINE_JSON_BYTES = 64 * 1024 * 1024
PIPELINE_FILES = (
    "pipeline-summary.json", "source-layout.json", "patterned-analysis.json",
    "physical-normalization.json", "physical-bar-plan-trial.json", "engineer-review.json",
)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Nonfinite JSON number: " + value)


def _read(path, maximum=MAX_PIPELINE_JSON_BYTES):
    with path.open("rb") as stream:
        content = stream.read(maximum + 1)
    if not content or len(content) > maximum:
        raise ValueError(f"Empty or oversized input: {path.name}")
    value = json.loads(content.decode("utf-8"), object_pairs_hook=_unique,
                       parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object: " + path.name)
    # Application contracts bind exact canonical bytes, not a silently rewritten file.
    if _bytes(value) != content:
        raise ValueError("Expected canonical immutable pipeline JSON: " + path.name)
    record = source_record(path, role="pipeline-artifact")
    if record["sha256"] != hashlib.sha256(content).hexdigest():
        raise ValueError("Input changed while reading: " + path.name)
    return value, content, record


def _code_digest():
    digest = hashlib.sha256()
    for path in [*sorted((ROOT / "src").rglob("*.py")), Path(__file__).resolve()]:
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-dir", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--candidate-id")
    choice.add_argument("--selection", choices=SELECTIONS)
    parser.add_argument("--working-host-report", type=Path, required=True)
    parser.add_argument("--host-offset-x-mm", type=float, required=True)
    parser.add_argument("--host-offset-y-mm", type=float, required=True)
    parser.add_argument("--host-binding-source", required=True)
    parser.add_argument("--maximum-shift-mm", type=float, default=300)
    parser.add_argument("--maximum-candidates-per-bar", type=int, default=32)
    parser.add_argument("--maximum-total-candidates", type=int, default=12000)
    parser.add_argument("--maximum-coverage-atoms", type=int, default=100000)
    parser.add_argument("--maximum-pair-checks", type=int, default=2000000)
    parser.add_argument("--time-limit-s", type=float, default=30)
    parser.add_argument("--stock-time-limit-s", type=float, default=10)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def _validate_args(args):
    if args.output_dir.exists():
        raise ValueError("Output directory already exists; choose a NEW directory")
    if not args.pipeline_dir.is_dir():
        raise ValueError("Existing complete pipeline directory required")
    if not args.host_binding_source.strip():
        raise ValueError("An explicit nonempty host binding source is required")
    for value, low, high, label in (
        (args.host_offset_x_mm, -1e8, 1e8, "host-offset-x-mm"),
        (args.host_offset_y_mm, -1e8, 1e8, "host-offset-y-mm"),
        (args.maximum_shift_mm, 0.001, 300, "maximum-shift-mm"),
        (args.time_limit_s, 0.001, 600, "time-limit-s"),
        (args.stock_time_limit_s, 0.001, 60, "stock-time-limit-s"),
    ):
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{label} must be finite in {low}..{high}")
    for value, maximum, label in (
        (args.maximum_candidates_per_bar, 128, "maximum-candidates-per-bar"),
        (args.maximum_total_candidates, 20000, "maximum-total-candidates"),
        (args.maximum_coverage_atoms, 200000, "maximum-coverage-atoms"),
        (args.maximum_pair_checks, 5000000, "maximum-pair-checks"),
    ):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"{label} must be an integer in 1..{maximum}")


def _load_recovery(args, loaded):
    files = {name: _read(args.pipeline_dir / name) for name in PIPELINE_FILES}
    summary, source = files["pipeline-summary.json"][0], files["source-layout.json"][0]
    if (summary["schema_version"] != "revit-assistant-pipeline/v1"
            or source["schema_version"] != "assistant-source-layout/v1"
            or summary["placement_eligible"] is not False
            or source["placement_eligible"] is not False):
        raise ValueError("An unchanged research pipeline and source layout are required")
    if (summary["candidate_id"] != loaded.snapshot["candidate_id"]
            or summary["case_id"] != loaded.problem.case_id
            or source["problem"] != to_jsonable(loaded.problem)
            or source["solution"] != to_jsonable(loaded.solution)):
        raise ValueError("Pipeline does not match the freshly restored source candidate")
    provenance = summary["source_provenance"]
    if source["source_provenance"] != provenance:
        raise ValueError("Pipeline source provenance differs")
    records = provenance["source_files"]
    verify_source_records(records)
    expected = {(str(Path(row["path"]).resolve()), row["sha256"])
                for row in (*loaded.snapshot["source_dxf"], loaded.snapshot["source_pdf"])}
    present = {(str(Path(row["path"]).resolve()), row["sha256"]) for row in records
               if row["role"] in ("dxf", "engineer-pdf")}
    if expected != present:
        raise ValueError("Pipeline source files differ from the fresh snapshot")
    recorded_snapshot = [row for row in records if row["role"] == "source-snapshot"]
    if recorded_snapshot and (len(recorded_snapshot) != 1
            or recorded_snapshot[0]["sha256"] != loaded.source_sha256):
        raise ValueError("Pipeline snapshot SHA256 differs")
    values = [files[name] for name in PIPELINE_FILES[2:]]
    patterned, normalization, packet, review = [entry[0] for entry in values]
    if any(value.get("source_provenance") != provenance for value in (patterned, normalization, review)):
        raise ValueError("Physical stages are not bound to the selected original sources")
    if (summary["packet_sha256"] != hashlib.sha256(files["physical-bar-plan-trial.json"][1]).hexdigest()
            or packet["case_id"] != loaded.problem.case_id):
        raise ValueError("Pipeline physical packet binding differs")
    recovery = PhysicalLayoutRecoveryResult(summary["status"], patterned, normalization, packet, review,
        *(entry[1] for entry in values))
    return recovery, summary, [*records, *(entry[2] for entry in files.values())]


def _result_md(result, provenance):
    review, draft = result.review, result.draft
    classification = review["search"]["classification"]
    return ("# Обход малых отверстий: полный проверенный черновик\n\n"
        f"Плита: `{draft['case_id']}`. Исходный кандидат: `{provenance['candidate_id']}`.\n\n"
        f"- Сдвинуто стержней: **{review['moved_bar_count']}**.\n"
        f"- Не помещаются в общий контур сечений: **{review['host_blocked_before']} → "
        f"{review['host_blocked_after']}**. Нерешённые стержни не удалены.\n"
        f"- Полная партия: **{review['physical_bar_count']} стержней / "
        f"{review['additional_mass_kg']:.3f} кг**; масса, длины, диаметры, владельцы и количество сохранены.\n"
        f"- Исходных КЭ без покрытия после коррекции: **{review['source_coverage_after']['uncovered_cell_count']}**.\n"
        f"- Пары пересечений одного направления: **{review['same_direction_body_pairs_before']} → "
        f"{review['same_direction_body_pairs_after']}**; новых **{review['new_same_direction_body_pairs']}**.\n"
        f"- Повторная проверка раскроя исходной неизменной партии: `{review['stock_cutting']['status']}`.\n\n"
        f"Причины исходных ограничений: наружный контур — **{classification.get('outer_boundary', 0)}**, "
        f"отверстие шириной от 300 мм — **{classification.get('opening_width_not_below_300', 0)}**, "
        f"допущены к поиску обхода — **{classification.get('eligible', 0)}**. "
        "Это не один общий счётчик малых отверстий.\n\n"
        "Явный исследовательский профиль: только замкнутые прямоугольные отверстия шириной строго менее 300 мм "
        "поперёк стержня. Поперечная проекция — наша консервативная формализация слова «ширина», "
        "не отдельно подтверждённое универсальное правило. Сдвиг внутри всех исходных зон; полные 40d сохранены. "
        "Исходный спрос не обрезан. Области обслуживания сдвигаются, но не расширяются.\n\n"
        "Это конечный совместный поиск, а не доказательство невозможности иных раскладок. "
        "Общее количество само по себе не заменяет геометрическую проверку покрытия.\n\n"
        "`physical-bar-relocation-draft.json` предназначен для отдельного графического просмотра. "
        "Это НЕ legacy Physical Plan Trial и НЕ команда создания Rebar. "
        "Фактические Z, X/Y-коллизии, существующая арматура и Windows/Revit readback ещё не проверены. "
        "Загибы, муфты и постоянное размещение не добавлены; `placement_eligible=false`.\n\n"
        "Полные причины, все КЭ, сдвиги, поисковые ограничения и SHA256: "
        "`opening-relocation-review.json`. Исходные файлы и старый пакет не изменялись.\n")


def run(args):
    _validate_args(args)
    started, code_before = perf_counter(), _code_digest()
    print("1/3: Re-ingest original DXF and verify the complete source candidate", flush=True)
    loaded = load_layout_snapshot(args.snapshot, candidate_id=args.candidate_id, selection=args.selection)
    recovery, summary, records = _load_recovery(args, loaded)
    snapshot_record = source_record(args.snapshot, role="source-snapshot")
    if snapshot_record["sha256"] != loaded.source_sha256:
        raise ValueError("Snapshot changed after fresh source verification")
    records.append(snapshot_record)
    with args.working_host_report.open("rb") as stream:
        host_bytes = stream.read(MAX_WORKING_REPORT_BYTES + 1)
    host = load_working_host_json(host_bytes, maximum_bytes=MAX_WORKING_REPORT_BYTES)
    inspect_working_solid(host)
    host_record = source_record(args.working_host_report, role="working-host-snapshot")
    if host_record["sha256"] != hashlib.sha256(host_bytes).hexdigest():
        raise ValueError("Host changed while reading")
    records.append(host_record)
    if "working_host_source" in summary:
        if summary["working_host_source"]["sha256"] != host_record["sha256"]:
            raise ValueError("Host-aware pipeline was prepared for a different host snapshot")
        host_fit, _, host_fit_record = _read(args.pipeline_dir / "working-host-fit.json")
        records.append(host_fit_record)
        if (host_fit["source_host_report_sha256"] != host_record["sha256"]
                or host_fit["source_to_revit_xy_mm"] != [args.host_offset_x_mm, args.host_offset_y_mm]
                or host_fit["binding_source"] != args.host_binding_source):
            raise ValueError("Host-aware pipeline binding/XY differs; do not silently change placement")
    config = OpeningRelocationConfig(args.maximum_shift_mm, args.maximum_candidates_per_bar,
        args.maximum_total_candidates, args.maximum_coverage_atoms, args.maximum_pair_checks, args.time_limit_s)
    print("2/3: Joint small-hole correction; whole-plan coverage, inventory, background and collisions", flush=True)
    result = correct_small_openings(recovery, loaded.problem, host, config=config,
        offset_x_mm=args.host_offset_x_mm, offset_y_mm=args.host_offset_y_mm,
        binding_source=args.host_binding_source, source_report_sha256=host_record["sha256"],
        stock_time_limit_s=args.stock_time_limit_s)
    verify_source_records(records)
    if _code_digest() != code_before:
        raise ValueError("Core code changed during correction; rerun on stable sources")
    provenance = {"candidate_id": loaded.snapshot["candidate_id"], "snapshot_sha256": loaded.source_sha256,
        "code_sha256": code_before, "source_files": records, "configuration": to_jsonable(config),
        "runtime_s": perf_counter() - started, "full_original_demand_reingested": True}
    review = {**result.review, "execution_provenance": provenance}
    files = {"physical-bar-relocation-draft.json": result.draft_bytes,
             "opening-relocation-review.json": _bytes(review),
             "RESULT.md": _result_md(result, provenance).encode("utf-8")}
    print("3/3: Write new graphic-only draft and review; legacy packet is unchanged", flush=True)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        with (args.output_dir / name).open("xb") as stream:
            stream.write(content)
    return review


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        review = run(args)
    except (ValueError, TypeError, KeyError, OSError) as error:
        parser.error(str(error))
    print(json.dumps({key: review[key] for key in ("moved_bar_count", "host_blocked_before", "host_blocked_after",
        "physical_bar_count", "additional_mass_kg", "new_same_direction_body_pairs", "placement_eligible")},
        ensure_ascii=False, allow_nan=False), flush=True)
    # Exit 0 means a completed research correction, never structural authorization.
    return 2 if review["host_blocked_after"] or review["same_direction_body_pairs_after"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
