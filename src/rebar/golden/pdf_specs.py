"""Извлечение спецификаций и превью из эталонного инженерного PDF."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .models import GoldenBarPosition, GoldenSheetExpectation, GoldenSheetMetrics

_SPEC_ROW = re.compile(
    r"^\s*(?P<position>\d+|[^\W\d_]\s?\d+)\s+"
    r"ГОСТ\s*34028-2016\s+.*?"
    r"[⌀Øø]\s*(?P<diameter>\d+).*?"
    r"\bL\s*=\s*(?P<length>\d+)\s+"
    r"(?P<quantity>\d+)\s+"
    r"(?P<unit_mass>\d+(?:[.,]\d+)?)\s+"
    r"(?P<total_mass>\d+(?:[.,]\d+)?)\s*$",
    re.UNICODE,
)


class GoldenPdfToolError(RuntimeError):
    """Poppler отсутствует либо не смог прочитать инженерный PDF."""


class GoldenReferenceMismatchError(AssertionError):
    """Текущая спецификация PDF не совпала с зафиксированным golden-case."""


def _number(value: str) -> float:
    return float(value.replace(",", "."))


def parse_specification_text(text: str) -> GoldenSheetMetrics:
    """Разобрать строки таблицы ``Спецификация элементов`` из ``pdftotext -raw``."""

    positions: list[GoldenBarPosition] = []
    for line in text.splitlines():
        match = _SPEC_ROW.match(line)
        if match is None:
            continue
        positions.append(
            GoldenBarPosition(
                position=match.group("position").replace(" ", ""),
                diameter_mm=int(match.group("diameter")),
                length_mm=int(match.group("length")),
                quantity=int(match.group("quantity")),
                unit_mass_kg=_number(match.group("unit_mass")),
                total_mass_kg=_number(match.group("total_mass")),
            )
        )
    if not positions:
        raise GoldenPdfToolError("В тексте страницы не найдены строки спецификации")
    return GoldenSheetMetrics(tuple(positions))


def extract_sheet_metrics(pdf_path: Path, page: int) -> GoldenSheetMetrics:
    """Извлечь контрольные числа одного листа через Poppler ``pdftotext``."""

    executable = shutil.which("pdftotext")
    if executable is None:
        raise GoldenPdfToolError(
            "Не найден pdftotext (Poppler); установите poppler для golden-проверки"
        )
    result = subprocess.run(
        [executable, "-f", str(page), "-l", str(page), "-raw", str(pdf_path), "-"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        message = result.stderr.strip() or f"код возврата {result.returncode}"
        raise GoldenPdfToolError(f"pdftotext не прочитал {pdf_path}: {message}")
    return parse_specification_text(result.stdout)


def validate_sheet_metrics(
    expectation: GoldenSheetExpectation,
    actual: GoldenSheetMetrics,
) -> None:
    """Остановить отчёт, если PDF перестал совпадать с проверенными числами."""

    mismatches: list[str] = []
    if actual.position_count != expectation.expected_position_count:
        mismatches.append(
            f"позиций {actual.position_count} != {expectation.expected_position_count}"
        )
    if actual.bar_count != expectation.expected_bar_count:
        mismatches.append(f"стержней {actual.bar_count} != {expectation.expected_bar_count}")
    if abs(actual.total_mass_kg - expectation.expected_mass_kg) > 0.01:
        mismatches.append(
            f"масса {actual.total_mass_kg:.2f} != {expectation.expected_mass_kg:.2f} кг"
        )
    if mismatches:
        joined = "; ".join(mismatches)
        raise GoldenReferenceMismatchError(
            f"Лист {expectation.pdf_page} ({expectation.title}) не совпал с эталоном: {joined}"
        )


def render_pdf_page_png(pdf_path: Path, page: int, *, dpi: int = 120) -> bytes:
    """Отрендерить одну страницу PDF в PNG и вернуть байты для inline HTML."""

    executable = shutil.which("pdftoppm")
    if executable is None:
        raise GoldenPdfToolError(
            "Не найден pdftoppm (Poppler); установите poppler для HTML-превью"
        )
    with tempfile.TemporaryDirectory(prefix="rebar-golden-") as temp_dir:
        prefix = Path(temp_dir) / f"page-{page}"
        result = subprocess.run(
            [
                executable,
                "-f",
                str(page),
                "-l",
                str(page),
                "-singlefile",
                "-png",
                "-r",
                str(dpi),
                str(pdf_path),
                str(prefix),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        output_path = prefix.with_suffix(".png")
        if result.returncode != 0 or not output_path.is_file():
            message = result.stderr.strip() or f"код возврата {result.returncode}"
            raise GoldenPdfToolError(f"pdftoppm не отрендерил {pdf_path}: {message}")
        return output_path.read_bytes()

