"""Независимая проверка фактически применённых позиций по каталогу А101."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from rebar.models import Rebar

from .a101_catalog import get_a101_profile
from .contracts import (
    A101PositionOutcome,
    A101PositionStatus,
    A101PositionValidation,
    A101ZonePositionCheck,
)


def a101_profile_id_from_metadata(meta: Mapping[str, Any]) -> str | None:
    """Получить профиль из прямого поля либо протокола явного mapping."""

    direct = meta.get("a101_profile_id")
    if isinstance(direct, str) and direct.strip():
        return direct.strip().casefold()
    mapping = meta.get("rebar_mapping")
    if not isinstance(mapping, Mapping):
        return None
    nested = mapping.get("a101_profile_id")
    return nested.strip().casefold() if isinstance(nested, str) and nested.strip() else None


def validate_a101_positions(
    profile_id: str | None,
    zones: Iterable[tuple[str, Rebar]],
) -> A101PositionValidation:
    """Проверить однослойные позиции зон, не считая отсутствующую строку запретом.

    Красная строка выбранного профиля даёт ``prohibited``. Позиция, которой в таблице
    нет, остаётся ``unknown``: таблицы названы рекомендательными, и ТЗ явно запрещает
    только выделенные красным позиции.
    """

    normalized_zones = tuple(zones)
    if profile_id is None:
        return A101PositionValidation(None, None, None, ())

    profile = get_a101_profile(profile_id)
    by_rebar = {
        position.additional: position
        for position in profile.positions
        if position.layer_count == 1
    }
    checks = []
    for zone_id, rebar in normalized_zones:
        position = by_rebar.get(rebar)
        if position is None:
            outcome = A101PositionOutcome.UNKNOWN
        elif position.status is A101PositionStatus.PROHIBITED:
            outcome = A101PositionOutcome.PROHIBITED
        else:
            outcome = A101PositionOutcome.ALLOWED
        checks.append(A101ZonePositionCheck(zone_id, rebar, outcome))

    return A101PositionValidation(
        profile_id=profile.id,
        table_id=profile.table_id,
        profile_title=f"{profile.construction_type}, {profile.thickness_label}",
        checks=tuple(checks),
    )
