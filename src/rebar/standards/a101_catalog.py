"""Проверенная транскрипция таблиц А101 из страниц 15-18 ТЗ."""

from __future__ import annotations

from rebar.models import Rebar

from .contracts import (
    A101PositionStatus,
    A101RebarPosition,
    A101RebarProfile,
    A101StepScheme,
)

SOURCE = "Техническое_задание_А101_ТЗ_Техлаб_2026.pdf"


def _position(
    diameter: int,
    step: int,
    *,
    prohibited: bool = False,
    layer_count: int = 1,
) -> A101RebarPosition:
    return A101RebarPosition(
        additional=Rebar(step=step, diameter=diameter),
        status=(
            A101PositionStatus.PROHIBITED
            if prohibited
            else A101PositionStatus.ALLOWED
        ),
        layer_count=layer_count,
    )


def _positions(
    values: tuple[tuple[int, int], ...],
    *,
    prohibited: frozenset[tuple[int, int]] = frozenset(),
    double_mesh: tuple[tuple[int, int], ...] = (),
) -> tuple[A101RebarPosition, ...]:
    return (
        *(
            _position(diameter, step, prohibited=(diameter, step) in prohibited)
            for diameter, step in values
        ),
        *(
            _position(diameter, step, layer_count=2)
            for diameter, step in double_mesh
        ),
    )


def _profile(
    id: str,
    table_id: str,
    pdf_page: int,
    construction_type: str,
    thickness_label: str,
    background: tuple[int, int],
    positions: tuple[A101RebarPosition, ...],
) -> A101RebarProfile:
    diameter, step = background
    return A101RebarProfile(
        id=id,
        table_id=table_id,
        pdf_page=pdf_page,
        construction_type=construction_type,
        thickness_label=thickness_label,
        background=Rebar(step=step, diameter=diameter),
        positions=positions,
        source=SOURCE,
    )


A101_242_FOUNDATION_PARKING_T450_550 = _profile(
    "a101-2.4.2-foundation-parking-t450-550-v1",
    "2.4.2",
    15,
    "Фундаментная плита подземной автостоянки",
    "t=450 мм; t=550 мм для двухуровневой стоянки",
    (14, 300),
    _positions(
        (
            (14, 300),
            (14, 150),
            (16, 150),
            (20, 150),
            (20, 100),
            (25, 100),
            (28, 100),
            (32, 100),
        ),
        prohibited=frozenset({(32, 100)}),
    ),
)

A101_242_PARKING_SLAB_T200_220 = _profile(
    "a101-2.4.2-parking-slab-t200-220-v1",
    "2.4.2",
    15,
    "Плита перекрытия подземной автостоянки",
    "t=200-220 мм",
    (12, 300),
    _positions(
        ((12, 300), (12, 150), (14, 150), (16, 150), (14, 100), (16, 100))
    ),
)

A101_242_PARKING_SLAB_T250_300 = _profile(
    "a101-2.4.2-parking-slab-t250-300-v1",
    "2.4.2",
    15,
    "Плита покрытия подземной автостоянки",
    "t=250-300 мм",
    (12, 300),
    _positions(
        (
            (12, 300),
            (12, 150),
            (14, 150),
            (16, 150),
            (14, 100),
            (16, 100),
            (20, 100),
            (22, 100),
        ),
        prohibited=frozenset({(22, 100)}),
    ),
)

A101_242_CAPITAL_T500_600 = _profile(
    "a101-2.4.2-capital-t500-600-v1",
    "2.4.2",
    15,
    "Капитель",
    "t=500-600 мм",
    (12, 150),
    _positions(
        (
            (12, 300),
            (12, 150),
            (14, 150),
            (16, 150),
            (14, 100),
            (16, 100),
            (20, 100),
            (22, 100),
            (25, 100),
        )
    ),
)

A101_243_FOUNDATION_T500 = _profile(
    "a101-2.4.3-foundation-residential-t500-v1",
    "2.4.3",
    16,
    "Фундаментная плита жилого корпуса",
    "t=500 мм",
    (14, 300),
    _positions(
        ((14, 300), (14, 150), (20, 150), (20, 100), (25, 100), (28, 100), (32, 100))
    ),
)

A101_243_FOUNDATION_T600_700 = _profile(
    "a101-2.4.3-foundation-residential-t600-700-v1",
    "2.4.3",
    16,
    "Фундаментная плита жилого корпуса",
    "t=600-700 мм",
    (16, 300),
    _positions(
        ((16, 300), (16, 150), (20, 150), (20, 100), (25, 100), (28, 100), (32, 100), (36, 100))
    ),
)

A101_243_FOUNDATION_T800_900 = _profile(
    "a101-2.4.3-foundation-residential-t800-900-v1",
    "2.4.3",
    16,
    "Фундаментная плита жилого корпуса",
    "t=800-900 мм",
    (18, 300),
    _positions(
        ((18, 300), (18, 150), (20, 150), (20, 100), (25, 100), (28, 100), (32, 100), (36, 100))
    ),
)

A101_243_FOUNDATION_T1000_1100 = _profile(
    "a101-2.4.3-foundation-residential-t1000-1100-v1",
    "2.4.3",
    16,
    "Фундаментная плита жилого корпуса",
    "t=1000-1100 мм",
    (20, 300),
    _positions(
        ((20, 300), (20, 150), (25, 150), (25, 100), (28, 100), (32, 100), (36, 100)),
        double_mesh=((32, 100),),
    ),
)

A101_243_FOUNDATION_T1200_1300 = _profile(
    "a101-2.4.3-foundation-residential-t1200-1300-v1",
    "2.4.3",
    16,
    "Фундаментная плита жилого корпуса",
    "t=1200-1300 мм",
    (22, 300),
    _positions(
        ((22, 300), (22, 150), (25, 150), (25, 100), (28, 100), (32, 100), (36, 100)),
        double_mesh=((32, 100), (36, 100)),
    ),
)

A101_243_FOUNDATION_T1400_1500 = _profile(
    "a101-2.4.3-foundation-residential-t1400-1500-v1",
    "2.4.3",
    16,
    "Фундаментная плита жилого корпуса",
    "t=1400-1500 мм",
    (25, 300),
    _positions(
        ((25, 300), (25, 150), (28, 100), (32, 100), (36, 100)),
        double_mesh=((32, 100), (36, 100)),
    ),
)

A101_244_ATS3_ZERO_T240 = _profile(
    "a101-2.4.4-ats3-zero-t240-v1",
    "2.4.4",
    17,
    "АТС 3.0, плита перекрытия на отм. 0.000",
    "t=240 мм",
    (12, 300),
    _positions(((12, 300), (12, 150), (16, 150), (16, 100))),
)

A101_244_ATS3_TYPICAL_T200 = _profile(
    "a101-2.4.4-ats3-typical-t200-v1",
    "2.4.4",
    17,
    "АТС 3.0, плита перекрытия типового этажа, включая перекрытие над -2 этажом",
    "t=200 мм",
    (10, 300),
    _positions(((10, 300), (10, 150), (12, 150), (12, 100), (16, 150), (16, 100))),
)

A101_244_ATS2_ZERO_T200 = _profile(
    "a101-2.4.4-ats2-zero-t200-v1",
    "2.4.4",
    17,
    "АТС 2.0, плита перекрытия на отм. 0.000",
    "t=200 мм",
    (12, 300),
    _positions(((12, 300), (12, 150), (16, 150), (16, 100))),
)

A101_244_ATS2_TYPICAL_T160 = _profile(
    "a101-2.4.4-ats2-typical-t160-v1",
    "2.4.4",
    17,
    "АТС 2.0, плита перекрытия типового этажа",
    "t=160 мм",
    (10, 240),
    _positions(((10, 240), (10, 120), (12, 120), (16, 120))),
)

A101_REBAR_PROFILES = (
    A101_242_CAPITAL_T500_600,
    A101_242_FOUNDATION_PARKING_T450_550,
    A101_242_PARKING_SLAB_T200_220,
    A101_242_PARKING_SLAB_T250_300,
    A101_243_FOUNDATION_T500,
    A101_243_FOUNDATION_T600_700,
    A101_243_FOUNDATION_T800_900,
    A101_243_FOUNDATION_T1000_1100,
    A101_243_FOUNDATION_T1200_1300,
    A101_243_FOUNDATION_T1400_1500,
    A101_244_ATS2_TYPICAL_T160,
    A101_244_ATS2_ZERO_T200,
    A101_244_ATS3_TYPICAL_T200,
    A101_244_ATS3_ZERO_T240,
)

_PROFILE_BY_ID = {profile.id: profile for profile in A101_REBAR_PROFILES}
if len(_PROFILE_BY_ID) != len(A101_REBAR_PROFILES):
    raise ValueError("идентификаторы профилей А101 должны быть уникальными")

A101_STEP_SCHEMES_247 = (
    A101StepScheme("Плиты", 300, (300, 150, 100), note_required_for_step_mm=100),
    A101StepScheme("Стены", 200, (200, 100), note_required_for_step_mm=100),
)


def available_a101_profile_ids() -> tuple[str, ...]:
    """Вернуть стабильный перечень профилей каталога."""

    return tuple(profile.id for profile in A101_REBAR_PROFILES)


def get_a101_profile(profile_id: str) -> A101RebarProfile:
    """Вернуть явно выбранный профиль либо отклонить неизвестный идентификатор."""

    try:
        return _PROFILE_BY_ID[profile_id.strip().casefold()]
    except KeyError as error:
        raise KeyError(f"неизвестный профиль А101: {profile_id!r}") from error
