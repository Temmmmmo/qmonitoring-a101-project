"""Разбор таблиц спецификации из текстового слоя инженерного PDF."""

import pytest

from rebar.golden import (
    GoldenReferenceMismatchError,
    GoldenSheetExpectation,
    parse_specification_text,
    validate_sheet_metrics,
)
from rebar.models import Axis, Direction, Layer


def test_parse_specification_text_separates_positions_bars_and_mass():
    text = """
    Спецификация элементов дополнительного армирования
    1 ГОСТ34028-2016 ⌀12А500С L= 2340 5 2.08 10.4
    П 1 ГОСТ 34028-2016 ⌀16А500С L = 3900 7 6,15 43,05
    случайная подпись на плане
    """

    metrics = parse_specification_text(text)

    assert metrics.position_count == 2
    assert metrics.bar_count == 12
    assert metrics.total_mass_kg == 53.45
    assert metrics.positions[1].position == "П1"
    assert metrics.positions[1].diameter_mm == 16
    assert metrics.positions[1].length_mm == 3900


def test_validate_sheet_metrics_reports_changed_reference():
    metrics = parse_specification_text(
        "1 ГОСТ34028-2016 ⌀12А500С L= 2340 5 2.08 10.4"
    )
    expectation = GoldenSheetExpectation(
        direction=Direction(Layer.BOTTOM, Axis.X),
        title="test",
        engineer_axis_label="test",
        pdf_page=1,
        input_png_name="input.png",
        expected_position_count=1,
        expected_bar_count=6,
        expected_mass_kg=10.4,
    )

    with pytest.raises(GoldenReferenceMismatchError, match="стержней 5 != 6"):
        validate_sheet_metrics(expectation, metrics)

