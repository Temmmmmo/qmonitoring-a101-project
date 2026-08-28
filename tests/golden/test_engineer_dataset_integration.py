"""Локальная сверка пяти машиночитаемых инженерских выдач с исходными PDF."""

from __future__ import annotations

import shutil

import pytest

from rebar.golden import build_engineer_dataset_inventory


def test_five_engineer_pdf_labels_match_manifest(data_dir):
    if shutil.which("pdftotext") is None:
        pytest.skip("для сверки инженерского датасета нужен pdftotext")
    if not data_dir.is_dir():
        pytest.skip(f"нет локальных материалов: {data_dir}")

    payload = build_engineer_dataset_inventory(data_dir)

    summary = payload["summary"]
    if summary["source_available_count"] == 0:
        pytest.skip("полный инженерный датасет не найден")
    assert summary["source_available_count"] == 11
    assert summary["verified_numeric_label_count"] == 5
    failed = [
        case
        for case in payload["cases"]
        if case["metrics_verification_status"] == "failed"
    ]
    assert failed == []
