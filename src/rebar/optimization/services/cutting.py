"""Политики выбора фактической длины прямого арматурного отрезка."""

from __future__ import annotations

from dataclasses import dataclass

from .geometry import GEOMETRY_TOLERANCE_MM

# Кратные отрезки из таблицы готового КЖ. Профиль применяется только явно.
PLATE_11700_CUT_LENGTHS_MM = (
    1170.0,
    1300.0,
    1460.0,
    1670.0,
    1950.0,
    2340.0,
    2925.0,
    3900.0,
    4875.0,
    5850.0,
    6825.0,
    7800.0,
    8775.0,
    9750.0,
    11700.0,
)


@dataclass(frozen=True)
class CutLengthCatalog:
    """Отсортированный проектный каталог допустимых длин отрезков."""

    id: str
    lengths_mm: tuple[float, ...]
    stock_length_mm: float | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("идентификатор каталога длин не может быть пустым")
        if not self.lengths_mm:
            raise ValueError("каталог длин не может быть пустым")
        if tuple(sorted(set(self.lengths_mm))) != self.lengths_mm:
            raise ValueError("длины каталога должны быть уникальными и возрастать")
        if any(length <= 0 for length in self.lengths_mm):
            raise ValueError("длины каталога должны быть положительными")
        if self.stock_length_mm is not None and self.stock_length_mm <= 0:
            raise ValueError("длина товарного прутка должна быть положительной")

    def select_length_mm(self, minimum_length_mm: float) -> float:
        """Округлить минимальную длину вверх до доступного отрезка."""

        if minimum_length_mm <= 0:
            raise ValueError("минимальная длина отрезка должна быть положительной")
        for length in self.lengths_mm:
            if length + GEOMETRY_TOLERANCE_MM >= minimum_length_mm:
                return length
        raise ValueError(
            f"длина {minimum_length_mm:.3f} мм превышает максимум каталога "
            f"{self.id!r}: {self.lengths_mm[-1]:.3f} мм"
        )


PLATE_11700_CATALOG = CutLengthCatalog(
    id="plate-11700",
    lengths_mm=PLATE_11700_CUT_LENGTHS_MM,
    stock_length_mm=11700.0,
)


def select_installed_length_mm(
    minimum_length_mm: float,
    allowed_lengths_mm: tuple[float, ...] = (),
) -> float:
    """Оставить непрерывную длину или округлить её по явному каталогу."""

    if minimum_length_mm <= 0:
        raise ValueError("минимальная длина отрезка должна быть положительной")
    if not allowed_lengths_mm:
        return minimum_length_mm
    return CutLengthCatalog("constraints", allowed_lengths_mm).select_length_mm(
        minimum_length_mm
    )

