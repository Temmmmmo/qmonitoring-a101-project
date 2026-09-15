"""Package a graphic Revit preview of the complete current plan and optional correction.

The extra button draws a NEW drafting view, not structural reinforcement. The
original physical packet and independently bound review are retained unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from package_revit_physical_trial import MAX_REVIEW_BYTES, NEW_FILES as PHYSICAL_FILES, _validate_review
from package_revit_plate_trial import NEW_FILES as PLATE_FILES, _read_bounded
from package_revit_probe import FILES as PROBE_FILES, SOURCE

BUTTON = "QMonitoring.extension/QMonitoring.tab/Diagnostics.panel/PlanPreview.pushbutton"
PREVIEW_FILES = ("QMonitoring.extension/lib/qm_revit_plan_preview.py",
                 "QMonitoring.extension/lib/qm_revit_source_preview.py",
                 "QMonitoring.extension/lib/qm_revit_pruned_preview.py",
                 f"{BUTTON}/script.py", f"{BUTTON}/bundle.yaml", "GRAPHIC_PREVIEW_README.md")

CODE_ONLY_MODULES = ("qm_probe_geometry.py", "qm_revit_probe.py", "qm_trial_geometry.py",
    "qm_trial_input.py", "qm_core_trial.py", "qm_plate_packet.py", "qm_physical_packet.py",
    "qm_revit_trial.py", "qm_trial_worksharing.py", "qm_revit_plan_preview.py", "qm_revit_source_preview.py",
    "qm_revit_pruned_preview.py")


def build_code_only_package(output):
    """Deterministic public code-only bundle; separate extension, no case input."""
    sys.path.insert(0, str(SOURCE / "QMonitoring.extension/lib"))
    from qm_revit_plan_preview import VERSION
    contents = {}
    for name in CODE_ONLY_MODULES:
        contents["QMonitoringPreview.extension/lib/"+name] = (SOURCE / "QMonitoring.extension/lib" / name).read_bytes()
    for name in ("script.py", "bundle.yaml"):
        contents["QMonitoringPreview.extension/QMonitoringPreview.tab/Diagnostics.panel/PlanPreview.pushbutton/"+name] = (SOURCE / BUTTON / name).read_bytes()
    contents["GRAPHIC_PREVIEW_README.md"] = (SOURCE / "GRAPHIC_PREVIEW_README.md").read_bytes()
    manifest = {"schema_version": "qmonitoring-graphic-preview-code-package/v1", "version": VERSION,
        "code_only": True, "placement_eligible": False, "engineering_approval": False,
        "supported_input_schemas": ["source-isofields-zones/v1", "physical-bar-plan-trial/v1", "physical-bar-relocation-draft/v1", "graphic-bar-plan-draft/v1", "graphic-bar-plan-pruned/v1"],
        "files": [{"path": name, "bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                  for name, value in sorted(contents.items())]}
    contents["manifest.json"] = json.dumps(manifest, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2).encode("utf-8")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, value in sorted(contents.items()):
            info = ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, value)
    return output


def _json(content):
    from qm_trial_input import _reject_constant, _unique_object
    from package_revit_physical_trial import _finite_float
    return json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                      parse_constant=_reject_constant, parse_float=_finite_float)


def build_package(output, packet_path, *, review_path, relocation_draft_path=None, relocation_review_path=None):
    sys.path.insert(0, str(SOURCE / "QMonitoring.extension/lib"))
    from qm_physical_packet import load_packet
    from qm_revit_plan_preview import VERSION, _validate_primitives, build_preview_primitives
    from qm_revit_probe import serialize_report_utf8

    if (relocation_draft_path is None) != (relocation_review_path is None):
        raise ValueError("Corrected draft and its independent review must be supplied together")
    packet = load_packet(str(packet_path))
    packet_content = serialize_report_utf8(packet)
    review_content = _read_bounded(review_path, MAX_REVIEW_BYTES, ".json")
    _validate_review(review_content, packet, packet_content)
    _validate_primitives(build_preview_primitives(packet, 0.0, 0.0))
    contents = [(name if name != "README.md" else "REFERENCE_README.md", (SOURCE / name).read_bytes())
                for name in (*PROBE_FILES, *PLATE_FILES, *PHYSICAL_FILES, *PREVIEW_FILES)]
    contents.extend((("physical-bar-plan-trial.json", packet_content), ("engineer-review.json", review_content)))
    selected = "physical-bar-plan-trial.json"
    correction_summary = "Коррекция малых отверстий в этот архив не включена."
    if relocation_draft_path is not None:
        draft_content = _read_bounded(relocation_draft_path, 16*1024*1024, ".json")
        correction_content = _read_bounded(relocation_review_path, 16*1024*1024, ".json")
        draft, correction = _json(draft_content), _json(correction_content)
        digest = hashlib.sha256(packet_content).hexdigest()
        if (not isinstance(draft, dict) or not isinstance(correction, dict)
                or draft.get("schema_version") != "physical-bar-relocation-draft/v1"
                or correction.get("schema_version") != "small-opening-relocation-review/v1"
                or draft.get("placement_eligible") is not False or draft.get("structural_placement_supported") is not False
                or draft.get("source_packet_sha256") != digest or correction.get("source_packet_sha256") != digest
                or correction.get("draft_sha256") != hashlib.sha256(draft_content).hexdigest()
                or correction.get("placement_eligible") is not False or correction.get("engineering_approval") is not False
                or draft.get("original_source_zones") != packet["source_zones"]
                or draft.get("case_id") != packet["case_id"]):
            raise ValueError("Corrected graphic draft/review must bind the exact complete original packet")
        for field in ("physical_bar_count", "additional_mass_kg", "source_zone_count", "position_count"):
            if draft["expected"][field] != packet["expected"][field]:
                raise ValueError("Small-opening correction changed the declared complete inventory")
        for field in ("source_host_report_sha256", "source_report_sha256", "source_to_revit_xy_mm", "binding_source"):
            if draft.get(field) != correction.get(field):
                raise ValueError("Draft host/source binding differs from its review")
        for field in ("policy_id", "moved_bar_count", "moves", "host_blocked_before", "host_blocked_after",
                      "new_same_direction_body_pairs"):
            if draft["correction"][field] != correction.get(field):
                raise ValueError("Draft correction numbers differ from its review")
        _validate_primitives(build_preview_primitives(draft, 0.0, 0.0))
        selected = "physical-bar-relocation-draft.json"
        correction_summary = (f"Перемещено {correction['moved_bar_count']} прямых стержней; "
            f"конфликтов с контуром/отверстиями: {correction['host_blocked_before']} → {correction['host_blocked_after']}. "
            "Это расчётный результат по снимку, не разрешение размещения.")
        contents.extend(((selected, draft_content), ("opening-relocation-review.json", correction_content)))
    readme = f"""# QMonitoring — посмотреть раскладку в Revit, {VERSION}

В архиве {packet['expected']['physical_bar_count']} прямых стержней, четыре направления,
{packet['expected']['additional_mass_kg']:.2f} кг расчётной дополнительной арматуры.
{correction_summary}

1. Распакуй в новую папку. В pyRevit → Custom Extension Directories добавь папку,
   внутри которой лежит `QMonitoring.extension`; убери прежний путь QMonitoring, Reload.
2. Открой рабочую модель и выдели нужную плиту. Рабочие наборы удалять не нужно.
   Для переданного тестового RVT: открой его с «Отсоединить от хранилища» →
   «Сохранить рабочие наборы» и запусти кнопку ДО сохранения RVT.
   Обычная локальная копия Revit также поддержана; прямой запуск в хранилище нет.
3. Нажми **QMonitoring → Diagnostics → Plan Preview**.
4. Выбери **`{selected}`**. Для сравнения исходная партия находится в
   `physical-bar-plan-trial.json`. Вводи смещение XY из проверенной привязки;
   оно не подбирается по картинке. Для изученного снимка плиты 11020633 это 0; 0 мм.
5. Подтверди создание отдельного чертёжного вида и дождись четырёх схем.
   Здесь не выбираются типы Rebar и глубины слоёв. Сделай скриншот и пришли отчёт.

Это **графический просмотр**, не созданная арматура. Новый вид содержит линии
и пояснения; существующие виды и арматура не меняются. Плагин не сохраняет и не
синхронизирует модель, не заимствует плиту. Красные/непроверенные участки не скрываются.
Наличие схемы не означает прохождения инженерных гейтов. Создание всего набора
конструктивных Rebar остаётся отдельным сценарием со своими проверками.

Подробная инструкция: `GRAPHIC_PREVIEW_README.md`.
В 0.1.0 Revit создал вид, но IronPython упал при подсчёте статусов; операция откатилась.
В 0.1.1 этот участок и аналогичная вложенность в readback заменены явными циклами.
Версия 0.1.1 успешно прочитала обратно 902/902 графических линий в настоящем Revit.
Новый исходный режим 0.2.0 (FilledRegion + зоны) пока проверен локально; его первый
запуск в Windows/Revit ещё требуется. Установка без исходных материалов доступна
через отдельный --code-only архив QMonitoringPreview.extension.
"""
    contents.append(("README.md", readme.encode("utf-8")))
    names = [name for name, _ in contents]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate archive entry")
    manifest = {"schema_version": "qmonitoring-graphic-preview-package/v1", "version": VERSION,
        "placement_eligible": False, "recommended_preview_input": selected,
        "files": [{"path": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
                  for name, content in contents]}
    contents.append(("manifest.json", json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in contents:
            archive.writestr(name, content)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--code-only", action="store_true")
    parser.add_argument("--relocation-draft", type=Path)
    parser.add_argument("--relocation-review", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.code_only:
            if any((args.packet, args.review, args.relocation_draft, args.relocation_review)):
                raise ValueError("Code-only mode cannot include case inputs or private reviews")
            print(build_code_only_package(args.output))
            return
        if args.packet is None or args.review is None:
            raise ValueError("Legacy case package requires both --packet and --review; use --code-only for installation")
        print(build_package(args.output, args.packet, review_path=args.review,
            relocation_draft_path=args.relocation_draft, relocation_review_path=args.relocation_review))
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
