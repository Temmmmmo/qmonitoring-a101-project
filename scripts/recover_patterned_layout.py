"""Recover complete original-demand STO axes and zero-waste stock, then export a rollback trial."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rebar.application.analyze_composite_plate import CompositeDirectionSettings
from rebar.application.layout_snapshot import SELECTIONS, _source_record, load_layout_snapshot
from rebar.application.patterned_layout_recovery import recover_patterned_layout
from rebar.application.plate_revit_trial import build_full_plate_trial
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS


def _code_digest() -> str:
    digest = hashlib.sha256()
    paths = [*sorted((ROOT / "src").rglob("*.py")),
             *sorted((ROOT / "integrations/pyrevit/QMonitoring.extension/lib").glob("*.py")),
             Path(__file__).resolve()]
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def export_patterned_snapshot(
    snapshot: Path, output_dir: Path, *, candidate_id: str | None = None,
    selection: str | None = None, background_origin_mm: float, first_300_offset_mm: float,
    contact_side: str, steel_class: str, phase_source: str,
    maximum_patches_per_direction: int = 32, maximum_zones_per_direction: int = 128,
    maximum_batch_mass_increase_pct: float = 5.0, balance_time_limit_s: float = 20.0,
) -> dict:
    """No overwrite: compute/check the whole bundle before creating its new directory."""
    if output_dir.exists():
        raise ValueError("Output directory already exists; choose a new path")
    code_before = _code_digest()
    loaded = load_layout_snapshot(snapshot, candidate_id=candidate_id, selection=selection)
    configs = tuple(CompositeDirectionSettings(direction, background_origin_mm, first_300_offset_mm,
        0.0, steel_class, phase_source, contact_side) for direction in PLATE_DIRECTIONS)
    recovered = recover_patterned_layout(loaded.problem, loaded.solution, configs,
        maximum_patches_per_direction=maximum_patches_per_direction,
        maximum_zones_per_direction=maximum_zones_per_direction,
        maximum_batch_mass_increase_pct=maximum_batch_mass_increase_pct,
        balance_time_limit_s=balance_time_limit_s)
    if not recovered.stock_balanced:
        raise ValueError("No full coverage + zero-waste batch candidate within the explicit limits; "
                         + str(recovered.report["length_balance_attempts"][-1]))
    for record in loaded.snapshot["source_dxf"]:
        _source_record(record, ".dxf")
    _source_record(loaded.snapshot["source_pdf"], ".pdf")
    if hashlib.sha256(Path(snapshot).read_bytes()).hexdigest() != loaded.source_sha256:
        raise ValueError("Snapshot changed during patterned recovery")
    code_after = _code_digest()
    if code_before != code_after:
        raise ValueError("Core code changed during calculation; rerun on a stable working tree")
    report = recovered.report
    report["source_snapshot"] = {**loaded.snapshot, "sha256": loaded.source_sha256}
    report["code_sha256"] = code_before
    report["code_sha256_after"] = code_after
    report_content = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    report_hash = hashlib.sha256(report_content.encode("utf-8")).hexdigest()
    packet = build_full_plate_trial(report, 0, report_hash)
    sys.path.insert(0, str(ROOT / "integrations/pyrevit/QMonitoring.extension/lib"))
    from qm_plate_packet import validate_packet
    validate_packet(packet)
    actual = report["front"][0]
    engineer = loaded.engineer_comparison
    review = {"schema_version": "patterned-layout-engineer-review/v1", "placement_eligible": False,
        "source_report_sha256": report_hash, "expected": packet["expected"],
        "prior_metrics": report["prior_metrics"], "source_snapshot": report["source_snapshot"],
        "coverage_policy": report["coverage_policy"], "source_demand_preserved": True,
        "source_cell_count_by_direction": [row["source_cell_count"] for row in report["directions"]],
        "uncovered_original_direction_cells": sum(row["candidates"][-1]["coverage"]["uncovered_cell_count"]
                                                   for row in report["directions"]),
        "residual_patches": {"{layer}-{axis}".format(**row["direction"]):
                             row["candidates"][0]["source_recovery"]["residual_patches"]
                             for row in report["directions"]},
        "explicit_research_settings": [row["settings"] for row in report["directions"]],
        "position_count": actual["position_count"], "stock_cutting": actual["stock_cutting"],
        "length_balance_attempts": report["length_balance_attempts"],
        "same_plane_conflicts": report["same_plane_conflicts"],
        "physical_placement_status": report["physical_placement_status"],
        "engineer_comparison": {"mass_kg": engineer["mass_kg"], "physical_bar_count": engineer["physical_bar_count"],
            "specification_rows": engineer["specification_rows"],
            "mass_delta_pct": (actual["additional_mass_kg"] / engineer["mass_kg"] - 1) * 100,
            "bar_delta_pct": (actual["physical_bar_count"] / engineer["physical_bar_count"] - 1) * 100,
            "mass_threshold_15pct_met": actual["additional_mass_kg"] <= 1.15 * engineer["mass_kg"]},
        "blocking_check_ids": report["blocking_check_ids"],
        "scope": "explicit monotone single-addition research; no approved host, depths or 3D collision clearance"}
    expectation = packet["expected"]
    readme = f"""# Полная плита: реальные периодические оси, инженерская проверка

Кандидат {loaded.snapshot['candidate_id']}, комплект {packet['case_id']}.
Статус физической укладки: **{report['physical_placement_status']}**.
При одной высоте на направление найдено
**{report['same_plane_conflicts']['body_intersection_count']} пар пересекающихся стержней**,
затронуто {report['same_plane_conflicts']['affected_zone_count']} зон.
Это отдельная проверка добавок в одной плоскости, не полная проверка host/фона/3D.
Положительные покрытие и раскрой не отменяют найденные пересечения.

Все четыре направления и весь исходный спрос сохранены. Непокрытых КЭ: 0.
{expectation['zone_count']} зон / {expectation['run_count']} Rebar-наборов /
{expectation['physical_bar_count']} стержней / {actual['position_count']} типоразмеров /
{expectation['additional_mass_kg']:.3f} кг добавки.

Это не равномерная подмена @150/@100: используются периодические оси 100/200 и
профиль касания СТО. Явные исследовательские параметры для всех направлений:
начало фона {background_origin_mm} мм, фаза добавки @300 +{first_300_offset_mm} мм,
сторона касания {contact_side}, класс {steel_class}. Источник выбора: {phase_source}.
Эти значения НЕ объявлены согласованными конструктором.

Отдельный исследовательский профиль допускает только одну добавку, тот же фон,
не меньший диаметр И не больший условный шаг. Слабые зоны не складываются.
Остатки закрыты точными требуемыми схемами; далее проверен безотходный раскрой
11700 мм при нулевом пропиле и неизменном количестве стержней. Полная геометрия
и покрытие повторно проверены после удлинения; производство эти допущения не подтвердило.

Нет подтверждённых host/привязки/глубин/проверки 3D-коллизий и разрешения на выпуск.
`full-plate-trial.json` предназначен только для существующей кнопки Full Plate Trial:
создание всей партии, readback и обязательный откат. Исходная RVT не сохраняется.

Подробности: `engineer-review.json`; воспроизводимый результат: `patterned-analysis.json`.
SHA256 именно patterned-analysis.json: {report_hash}.
"""
    files = {"patterned-analysis.json": report_content,
             "full-plate-trial.json": json.dumps(packet, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
             "engineer-review.json": json.dumps(review, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
             "README.md": readme}
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        with (output_dir / name).open("x", encoding="utf-8") as stream:
            stream.write(content)
    return {"output_dir": str(output_dir), "candidate_id": loaded.snapshot["candidate_id"],
            **expectation, "position_count": actual["position_count"],
            "same_plane_intersection_pairs": report["same_plane_conflicts"]["body_intersection_count"],
            "physical_placement_status": report["physical_placement_status"],
            "engineer_comparison": review["engineer_comparison"], "placement_eligible": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--candidate-id")
    choice.add_argument("--selection", choices=SELECTIONS)
    parser.add_argument("--background-origin-mm", type=float, required=True)
    parser.add_argument("--first-300-offset-mm", type=float, required=True)
    parser.add_argument("--contact-side", choices=("left", "right"), required=True)
    parser.add_argument("--steel-class", required=True)
    parser.add_argument("--phase-source", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--maximum-patches-per-direction", type=int, default=32)
    parser.add_argument("--maximum-zones-per-direction", type=int, default=128)
    parser.add_argument("--maximum-batch-mass-increase-pct", type=float, default=5)
    parser.add_argument("--balance-time-limit-s", type=float, default=20)
    args = parser.parse_args()
    try:
        result = export_patterned_snapshot(args.snapshot, args.output_dir, candidate_id=args.candidate_id,
            selection=args.selection, background_origin_mm=args.background_origin_mm,
            first_300_offset_mm=args.first_300_offset_mm, contact_side=args.contact_side,
            steel_class=args.steel_class, phase_source=args.phase_source,
            maximum_patches_per_direction=args.maximum_patches_per_direction,
            maximum_zones_per_direction=args.maximum_zones_per_direction,
            maximum_batch_mass_increase_pct=args.maximum_batch_mass_increase_pct,
            balance_time_limit_s=args.balance_time_limit_s)
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
