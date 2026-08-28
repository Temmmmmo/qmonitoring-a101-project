"""Контракт полного manifest инженерских решений."""

from rebar.golden import (
    ENGINEER_REFERENCE_CASES,
    PLATE_ZERO_K09,
    EngineerMetricStatus,
    get_engineer_reference_case,
)


def test_engineer_dataset_has_eleven_outputs_and_fifteen_inputs():
    cases = ENGINEER_REFERENCE_CASES

    assert len(cases) == 11
    assert len({case.id for case in cases}) == 11
    assert sum(len(case.input_sets) for case in cases) == 15
    assert sum(
        case.metric_status is EngineerMetricStatus.VERIFIED_PDF_SPEC for case in cases
    ) == 5


def test_each_input_set_contains_all_four_unique_directions():
    for case in ENGINEER_REFERENCE_CASES:
        for input_set in case.input_sets:
            assert len(input_set.dxf_by_direction) == 4
            assert {str(direction) for direction, _ in input_set.dxf_by_direction} == {
                "bottom-X",
                "bottom-Y",
                "top-X",
                "top-Y",
            }
            assert len(set(input_set.direction_file_names.values())) == 4


def test_plate_zero_dataset_entry_matches_strict_golden_totals():
    case = get_engineer_reference_case("plate-zero-k09")

    assert case.expected_position_count == PLATE_ZERO_K09.expected_position_count
    assert case.expected_bar_count == PLATE_ZERO_K09.expected_bar_count
    assert case.expected_mass_kg == PLATE_ZERO_K09.expected_mass_kg


def test_verified_weak_labels_have_positive_expected_metrics():
    verified = [
        case
        for case in ENGINEER_REFERENCE_CASES
        if case.metric_status is EngineerMetricStatus.VERIFIED_PDF_SPEC
    ]

    assert {case.id for case in verified} == {
        "k09-above-1",
        "k09-foundation",
        "k09-minus-2",
        "k09-typical-3-14",
        "plate-zero-k09",
    }
    for case in verified:
        assert case.expected_position_count is not None and case.expected_position_count > 0
        assert case.expected_bar_count is not None and case.expected_bar_count > 0
        assert case.expected_mass_kg is not None and case.expected_mass_kg > 0
