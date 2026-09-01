"""Проверки транскрипции и семантики каталога позиций А101."""

from rebar import Rebar
from rebar.standards import (
    A101_242_FOUNDATION_PARKING_T450_550,
    A101PositionOutcome,
    A101PositionStatus,
    A101_REBAR_PROFILES,
    A101_STEP_SCHEMES_247,
    a101_profile_id_from_metadata,
    available_a101_profile_ids,
    get_a101_profile,
    validate_a101_positions,
)


def test_catalog_contains_all_rows_from_available_a101_tables():
    assert len(A101_REBAR_PROFILES) == 14
    assert {profile.table_id for profile in A101_REBAR_PROFILES} == {
        "2.4.2",
        "2.4.3",
        "2.4.4",
    }
    assert len(available_a101_profile_ids()) == len(A101_REBAR_PROFILES)
    assert all(profile.source.endswith(".pdf") for profile in A101_REBAR_PROFILES)


def test_catalog_marks_only_explicit_red_positions_as_prohibited():
    prohibited = [
        (profile.id, position.additional)
        for profile in A101_REBAR_PROFILES
        for position in profile.positions
        if position.status is A101PositionStatus.PROHIBITED
    ]

    assert prohibited == [
        (
            "a101-2.4.2-foundation-parking-t450-550-v1",
            Rebar(step=100, diameter=32),
        ),
        (
            "a101-2.4.2-parking-slab-t250-300-v1",
            Rebar(step=100, diameter=22),
        ),
    ]


def test_catalog_preserves_double_mesh_rows_and_step_scheme_247():
    double_mesh = [
        position
        for profile in A101_REBAR_PROFILES
        for position in profile.positions
        if position.layer_count == 2
    ]

    assert len(double_mesh) == 5
    assert A101_STEP_SCHEMES_247[0].additional_steps_mm == (300, 150, 100)
    assert A101_STEP_SCHEMES_247[0].note_required_for_step_mm == 100
    assert A101_STEP_SCHEMES_247[1].additional_steps_mm == (200, 100)


def test_position_validation_separates_allowed_prohibited_and_unknown():
    validation = validate_a101_positions(
        A101_242_FOUNDATION_PARKING_T450_550.id,
        (
            ("allowed", Rebar(step=100, diameter=28)),
            ("prohibited", Rebar(step=100, diameter=32)),
            ("unknown", Rebar(step=300, diameter=20)),
        ),
    )

    assert tuple(check.outcome for check in validation.checks) == (
        A101PositionOutcome.ALLOWED,
        A101PositionOutcome.PROHIBITED,
        A101PositionOutcome.UNKNOWN,
    )
    assert validation.allowed_count == 1
    assert validation.prohibited_count == 1
    assert validation.unknown_count == 1


def test_absent_profile_is_not_silently_replaced_by_global_default():
    validation = validate_a101_positions(
        None,
        (("zone", Rebar(step=100, diameter=32)),),
    )

    assert validation.profile_id is None
    assert validation.checks == ()


def test_profile_lookup_and_metadata_protocol_are_stable():
    profile_id = A101_242_FOUNDATION_PARKING_T450_550.id

    assert get_a101_profile(profile_id.upper()).id == profile_id
    assert a101_profile_id_from_metadata(
        {"rebar_mapping": {"a101_profile_id": profile_id.upper()}}
    ) == profile_id
    assert a101_profile_id_from_metadata({}) is None
