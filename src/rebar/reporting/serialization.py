"""Сериализация доменных dataclass и enum в JSON-совместимые значения."""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any


def to_jsonable(value: Any) -> Any:
    """Рекурсивно преобразовать доменное значение для ``json.dumps``."""

    if dataclasses.is_dataclass(value):
        return {
            field.name: to_jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value
