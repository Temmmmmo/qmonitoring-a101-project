"""Проверка manifest инженерских выдач и подготовка слабых plate-level меток."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .dataset_catalog import ENGINEER_REFERENCE_CASES
from .dataset_models import (
    EngineerMetricStatus,
    EngineerReferenceCase,
    EngineerReferenceMetrics,
)
from .pdf_specs import (
    GoldenPdfToolError,
    GoldenReferenceMismatchError,
    extract_sheet_metrics,
)
from .sources import GoldenSourceNotFoundError, resolve_engineer_reference_files


def extract_engineer_reference_metrics(
    case: EngineerReferenceCase,
    pdf_path: Path,
) -> EngineerReferenceMetrics:
    """Повторно извлечь и строго сверить релевантные листы одной выдачи."""

    if case.metric_status is not EngineerMetricStatus.VERIFIED_PDF_SPEC or not case.sheets:
        raise GoldenPdfToolError(
            f"Выдача {case.id!r} не имеет проверенного машиночитаемого набора листов"
        )

    extracted = tuple(
        (sheet, extract_sheet_metrics(pdf_path, sheet.pdf_page)) for sheet in case.sheets
    )
    mismatches: list[str] = []
    for sheet, metrics in extracted:
        if metrics.position_count != sheet.expected_position_count:
            mismatches.append(
                f"лист {sheet.pdf_page}: позиций {metrics.position_count} "
                f"!= {sheet.expected_position_count}"
            )
        if metrics.bar_count != sheet.expected_bar_count:
            mismatches.append(
                f"лист {sheet.pdf_page}: стержней {metrics.bar_count} "
                f"!= {sheet.expected_bar_count}"
            )
        if abs(metrics.total_mass_kg - sheet.expected_mass_kg) > 0.01:
            mismatches.append(
                f"лист {sheet.pdf_page}: масса {metrics.total_mass_kg:.2f} "
                f"!= {sheet.expected_mass_kg:.2f} кг"
            )
    if mismatches:
        raise GoldenReferenceMismatchError(
            f"Инженерская выдача {case.id!r} не совпала с manifest: " + "; ".join(mismatches)
        )
    return EngineerReferenceMetrics(extracted)


def _metrics_payload(metrics: EngineerReferenceMetrics | None) -> dict[str, object] | None:
    if metrics is None:
        return None
    return {
        "position_count": metrics.position_count,
        "physical_bar_count": metrics.bar_count,
        "total_mass_kg": metrics.total_mass_kg,
    }


def _expected_metrics_payload(case: EngineerReferenceCase) -> dict[str, object] | None:
    if case.expected_position_count is None:
        return None
    return {
        "position_count": case.expected_position_count,
        "physical_bar_count": case.expected_bar_count,
        "total_mass_kg": case.expected_mass_kg,
    }


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def build_engineer_dataset_inventory(
    data_dir: Path,
    *,
    cases: Iterable[EngineerReferenceCase] = ENGINEER_REFERENCE_CASES,
) -> dict[str, object]:
    """Проверить доступные локальные источники и вернуть переносимый JSON payload."""

    case_list = tuple(cases)
    items: list[dict[str, object]] = []
    source_available_count = 0
    verified_numeric_label_count = 0

    for case in case_list:
        files = None
        metrics = None
        error_message = None
        source_status = "available"
        metrics_verification_status = "not_machine_readable"
        try:
            files = resolve_engineer_reference_files(case, data_dir)
            source_available_count += 1
            if case.metric_status is EngineerMetricStatus.VERIFIED_PDF_SPEC:
                metrics = extract_engineer_reference_metrics(case, files.engineer_pdf)
                metrics_verification_status = "verified_against_pdf"
                verified_numeric_label_count += 1
        except GoldenSourceNotFoundError as error:
            source_status = "missing"
            metrics_verification_status = "not_checked"
            error_message = str(error)
        except (GoldenPdfToolError, GoldenReferenceMismatchError) as error:
            metrics_verification_status = "failed"
            error_message = str(error)

        input_sets = []
        for input_set in case.input_sets:
            resolved = files.dxf_by_input_set[input_set.id] if files is not None else {}
            input_sets.append(
                {
                    "input_set_id": input_set.id,
                    "title": input_set.title,
                    "direction_mapping_verified": input_set.direction_mapping_verified,
                    "dxf_by_direction": {
                        str(direction): (
                            _relative(resolved[direction], data_dir)
                            if direction in resolved
                            else file_name
                        )
                        for direction, file_name in input_set.dxf_by_direction
                    },
                }
            )

        items.append(
            {
                "case_id": case.id,
                "title": case.title,
                "source_status": source_status,
                "engineer_pdf": (
                    _relative(files.engineer_pdf, data_dir)
                    if files is not None
                    else "/".join(case.engineer_pdf_parts)
                ),
                "input_sets": input_sets,
                "metric_status": case.metric_status.value,
                "metrics_verification_status": metrics_verification_status,
                "expected_metrics": _expected_metrics_payload(case),
                "actual_metrics": _metrics_payload(metrics),
                "specification_sheets": [
                    {
                        "pdf_page": sheet.pdf_page,
                        "directions": [str(direction) for direction in sheet.directions],
                        "position_count": sheet.expected_position_count,
                        "physical_bar_count": sheet.expected_bar_count,
                        "total_mass_kg": sheet.expected_mass_kg,
                    }
                    for sheet in case.sheets
                ],
                "notes": list(case.notes),
                "error": error_message,
            }
        )

    input_set_count = sum(len(case.input_sets) for case in case_list)
    declared_numeric_label_count = sum(
        case.metric_status is EngineerMetricStatus.VERIFIED_PDF_SPEC for case in case_list
    )
    return {
        "schema_version": 1,
        "summary": {
            "reference_case_count": len(case_list),
            "input_set_count": input_set_count,
            "source_available_count": source_available_count,
            "declared_numeric_label_count": declared_numeric_label_count,
            "verified_numeric_label_count": verified_numeric_label_count,
            "unreadable_pdf_count": sum(
                case.metric_status is EngineerMetricStatus.PDF_TEXT_UNREADABLE
                for case in case_list
            ),
            "end_to_end_golden_count": 1,
        },
        "cases": items,
        "limitations": [
            "Числовые итоги являются слабыми plate-level демонстрациями, а не метками Точки 3.",
            "Только plate-zero-k09 имеет явный mapping и подключённый end-to-end golden-test.",
            "Для шести старых PDF нужен OCR или ручная контрольная транскрипция.",
            "Геометрия инженерских зон потребует DWG-to-DXF; PDF даёт только агрегаты спецификаций.",
            "Один типовой результат 3-14 этажей связан с двумя входными комплектами.",
        ],
    }
