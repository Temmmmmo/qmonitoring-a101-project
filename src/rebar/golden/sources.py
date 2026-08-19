"""Поиск локальных файлов golden-case с учётом Unicode-нормализации путей."""

from __future__ import annotations

import unicodedata
from pathlib import Path

from .models import GoldenCaseDefinition, GoldenCaseFiles


class GoldenSourceNotFoundError(FileNotFoundError):
    """Материалы организаторов для эталона не найдены или неоднозначны."""


def _normalised(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _matching_paths(root: Path, name: str, *, directories: bool | None = None) -> list[Path]:
    expected = _normalised(name)
    matches: list[Path] = []
    for candidate in root.rglob("*"):
        if _normalised(candidate.name) != expected:
            continue
        if directories is True and not candidate.is_dir():
            continue
        if directories is False and not candidate.is_file():
            continue
        matches.append(candidate)
    return matches


def _find_unique(root: Path, name: str, *, directories: bool | None = None) -> Path:
    matches = _matching_paths(root, name, directories=directories)
    if len(matches) != 1:
        kind = "каталог" if directories else "файл"
        raise GoldenSourceNotFoundError(
            f"Ожидался один {kind} {name!r} внутри {root}, найдено: {len(matches)}"
        )
    return matches[0]


def resolve_golden_case_files(
    case: GoldenCaseDefinition,
    data_dir: Path,
) -> GoldenCaseFiles:
    """Найти PDF инженера и четыре входных PNG относительно каталога материалов."""

    if not data_dir.is_dir():
        raise GoldenSourceNotFoundError(f"Каталог материалов не найден: {data_dir}")

    if _normalised(data_dir.name) == _normalised(case.dataset_dir_name):
        dataset_dir = data_dir
    else:
        dataset_dir = _find_unique(data_dir, case.dataset_dir_name, directories=True)

    task_dir = _find_unique(dataset_dir, case.input_task_dir_name, directories=True)
    engineer_pdf = _find_unique(dataset_dir, case.engineer_pdf_name, directories=False)
    input_png_by_direction = {
        sheet.direction: _find_unique(task_dir, sheet.input_png_name, directories=False)
        for sheet in case.sheets
    }
    return GoldenCaseFiles(
        engineer_pdf=engineer_pdf,
        input_png_by_direction=input_png_by_direction,
    )

