"""Явный ограниченный host: горизонтальная прямоугольная призма и сквозные проёмы."""
from __future__ import annotations

from dataclasses import dataclass
import math

from .problem import BBox


@dataclass(frozen=True)
class RectangularHostEnvelope:
    outer_mm: BBox
    openings_mm: tuple[BBox, ...]
    bottom_z_mm: float
    top_z_mm: float
    top_cover_mm: float
    bottom_cover_mm: float
    side_cover_mm: float
    source: str

    def __post_init__(self):
        for box in (self.outer_mm, *self.openings_mm):
            if (len(box) != 4 or any(isinstance(v, bool) or not math.isfinite(v) for v in box)
                    or box[2] <= box[0] or box[3] <= box[1]):
                raise ValueError("host требует конечные невырожденные прямоугольники")
        numbers = (self.bottom_z_mm, self.top_z_mm, self.top_cover_mm, self.bottom_cover_mm, self.side_cover_mm)
        if any(isinstance(v, bool) or not math.isfinite(v) for v in numbers):
            raise ValueError("невалидные высоты или защитные слои")
        if self.top_z_mm <= self.bottom_z_mm or min(numbers[2:]) < 0 or not self.source.strip():
            raise ValueError("нужны положительная толщина, неотрицательный cover и источник геометрии")
        outer = self.outer_mm
        for box in self.openings_mm:
            if not (outer[0] < box[0] < box[2] < outer[2] and outer[1] < box[1] < box[3] < outer[3]):
                raise ValueError("проём должен строго находиться внутри контура")
        if len(self.openings_mm) > 128:
            raise ValueError("превышен лимит 128 проёмов")
