"""Преобразование строгих и слабых инженерских меток в общий benchmark-контракт."""

import pytest

from rebar.golden import (
    PLATE_ZERO_K09,
    get_engineer_reference_case,
    plate_metric_reference_from_engineer,
    plate_metric_reference_from_golden,
)


def test_strict_golden_becomes_plate_metric_reference():
    reference = plate_metric_reference_from_golden(PLATE_ZERO_K09)

    assert reference.id == "plate-zero-k09"
    assert reference.source_kind == "strict_golden"
    assert reference.expected_position_count == 79
    assert reference.expected_bar_count == 1019
    assert reference.expected_mass_kg == 3177.64


def test_verified_engineer_pdf_becomes_weak_plate_metric_reference():
    reference = plate_metric_reference_from_engineer(
        get_engineer_reference_case("k09-minus-2")
    )

    assert reference.source_kind == "verified_pdf_spec"
    assert reference.expected_position_count == 51
    assert reference.expected_bar_count == 856
    assert reference.expected_mass_kg == 3260.62


def test_unreadable_pdf_cannot_be_used_as_numeric_reference():
    with pytest.raises(ValueError, match="не имеет проверенных числовых метрик"):
        plate_metric_reference_from_engineer(
            get_engineer_reference_case("legacy-kj00-s1-2")
        )
