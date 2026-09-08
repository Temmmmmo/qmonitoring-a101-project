"""Консервативный пространственный индекс КЭ для ускорения детализации.

Только broad phase: истинное пересечение всё равно вычисляется по полигону.
Независимый hard-валидатор этот индекс не использует.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import median

from ..contracts import BBox, DemandCell
from .geometry import GEOMETRY_TOLERANCE_MM, cell_bbox


@dataclass(frozen=True)
class CellSpatialIndex:
    cells: tuple[DemandCell, ...]
    tile_width: float
    tile_height: float
    buckets: dict[tuple[int, int], tuple[int, ...]]
    global_positions: tuple[int, ...]

    @classmethod
    def build(cls, cells: tuple[DemandCell, ...]) -> CellSpatialIndex:
        boxes = tuple(cell_bbox(cell) for cell in cells)
        widths = [b[2] - b[0] for b in boxes if math.isfinite(b[2] - b[0]) and b[2] > b[0]]
        heights = [b[3] - b[1] for b in boxes if math.isfinite(b[3] - b[1]) and b[3] > b[1]]
        width, height = median(widths) if widths else 1.0, median(heights) if heights else 1.0
        buckets: dict[tuple[int, int], list[int]] = {}
        global_positions = []
        for position, box in enumerate(boxes):
            if not all(math.isfinite(v) for v in box):
                global_positions.append(position)
                continue
            x0, y0, x1, y1 = _tile_range(box, width, height)
            # Огромный полигон не должен создавать миллионы записей индекса.
            if (x1 - x0 + 1) * (y1 - y0 + 1) > 256:
                global_positions.append(position)
                continue
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    buckets.setdefault((x, y), []).append(position)
        return cls(cells, width, height, {key: tuple(v) for key, v in buckets.items()},
                   tuple(global_positions))

    def query(self, bbox: BBox) -> tuple[DemandCell, ...]:
        """Кандидаты в исходном порядке, без ложного исключения граничных КЭ."""
        if not all(math.isfinite(v) for v in bbox):
            return self.cells
        x0, y0, x1, y1 = _tile_range(bbox, self.tile_width, self.tile_height)
        if (x1 - x0 + 1) * (y1 - y0 + 1) > max(1, len(self.buckets)):
            return self.cells
        positions = set(self.global_positions)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                positions.update(self.buckets.get((x, y), ()))
        return tuple(self.cells[position] for position in sorted(positions))


def _tile_range(bbox: BBox, width: float, height: float) -> tuple[int, int, int, int]:
    tolerance = GEOMETRY_TOLERANCE_MM
    return (math.floor((bbox[0] - tolerance) / width), math.floor((bbox[1] - tolerance) / height),
            math.floor((bbox[2] + tolerance) / width), math.floor((bbox[3] + tolerance) / height))
