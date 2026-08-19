"""Golden-проверка против реального PDF организаторов (локально, не в CI)."""

from __future__ import annotations

import shutil

import pytest

from rebar.golden import (
    PLATE_ZERO_K09,
    GoldenSourceNotFoundError,
    extract_sheet_metrics,
    resolve_golden_case_files,
    validate_sheet_metrics,
)


def test_plate_zero_pdf_specifications_match_golden(data_dir):
    if shutil.which("pdftotext") is None:
        pytest.skip("для интеграционного golden-теста нужен pdftotext")
    try:
        files = resolve_golden_case_files(PLATE_ZERO_K09, data_dir)
    except GoldenSourceNotFoundError as error:
        pytest.skip(str(error))

    totals = {"positions": 0, "bars": 0, "mass": 0.0}
    for expectation in PLATE_ZERO_K09.sheets:
        metrics = extract_sheet_metrics(files.engineer_pdf, expectation.pdf_page)
        validate_sheet_metrics(expectation, metrics)
        totals["positions"] += metrics.position_count
        totals["bars"] += metrics.bar_count
        totals["mass"] += metrics.total_mass_kg

    assert totals["positions"] == 79
    assert totals["bars"] == 1019
    assert round(totals["mass"], 2) == 3177.64

