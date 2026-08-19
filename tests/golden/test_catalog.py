"""Контракт первого инженерного golden-case."""

from rebar.golden import PLATE_ZERO_K09


def test_plate_zero_has_four_directions_and_verified_totals():
    case = PLATE_ZERO_K09

    assert {str(sheet.direction) for sheet in case.sheets} == {
        "bottom-X",
        "bottom-Y",
        "top-X",
        "top-Y",
    }
    assert {sheet.pdf_page for sheet in case.sheets} == {8, 9, 10, 11}
    assert case.expected_position_count == 79
    assert case.expected_bar_count == 1019
    assert case.expected_mass_kg == 3177.64

