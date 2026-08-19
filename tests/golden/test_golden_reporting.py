"""Переносимость HTML инженерного golden-case."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import rebar.reporting.golden as golden_reporting
from rebar.golden import PLATE_ZERO_K09, GoldenBarPosition, GoldenSheetMetrics

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _create_fake_sources(root: Path) -> None:
    case = PLATE_ZERO_K09
    dataset = root / case.dataset_dir_name
    task = dataset / case.input_task_dir_name / "Изополя"
    package = dataset / "готовый комплект"
    task.mkdir(parents=True)
    package.mkdir()
    (package / case.engineer_pdf_name).write_bytes(b"fake pdf")
    for sheet in case.sheets:
        (task / sheet.input_png_name).write_bytes(TINY_PNG)


def test_golden_report_embeds_all_images_and_exports_metrics(tmp_path, monkeypatch):
    _create_fake_sources(tmp_path)

    def fake_metrics(_pdf_path: Path, page: int) -> GoldenSheetMetrics:
        expectation = next(sheet for sheet in PLATE_ZERO_K09.sheets if sheet.pdf_page == page)
        return GoldenSheetMetrics(
            tuple(
                GoldenBarPosition(
                    position=str(index + 1),
                    diameter_mm=12,
                    length_mm=1000,
                    quantity=(
                        expectation.expected_bar_count
                        - expectation.expected_position_count
                        + 1
                        if index == 0
                        else 1
                    ),
                    unit_mass_kg=1.0,
                    total_mass_kg=expectation.expected_mass_kg if index == 0 else 0.0,
                )
                for index in range(expectation.expected_position_count)
            )
        )

    def fake_validate(expectation, _actual):
        assert expectation in PLATE_ZERO_K09.sheets

    monkeypatch.setattr(golden_reporting, "extract_sheet_metrics", fake_metrics)
    monkeypatch.setattr(golden_reporting, "validate_sheet_metrics", fake_validate)
    monkeypatch.setattr(
        golden_reporting,
        "render_pdf_page_png",
        lambda _pdf_path, _page, *, dpi: TINY_PNG,
    )

    report = golden_reporting.generate_golden_report(
        PLATE_ZERO_K09,
        tmp_path,
        tmp_path / "report",
    )

    document = report.read_text(encoding="utf-8")
    payload = json.loads((report.parent / "golden.json").read_text(encoding="utf-8"))
    assert document.count('data-testid="golden-sheet-') == 4
    assert document.count("data:image/png;base64,") == 8
    assert "Ручная раскладка инженера" in document
    assert "src=\"/" not in document
    assert payload["case_id"] == "plate-zero-k09"
    assert payload["totals"]["bar_count"] == PLATE_ZERO_K09.expected_bar_count
