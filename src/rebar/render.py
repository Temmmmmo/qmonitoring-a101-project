"""Визуальная сверка: Mosaic -> PNG. Простой рендер для глазами-проверки ingest.

Рисуем каждый КЭ его ACI-цветом (ezdxf.colors.aci2rgb). Полезно, чтобы убедиться, что
распарсили геометрию и цвета правильно (сравнить с исходным PNG). TODO для Codex.
"""

from __future__ import annotations

from pathlib import Path

from ezdxf.colors import aci2rgb
from PIL import Image, ImageDraw

from .models import Mosaic


def render_mosaic(mosaic: Mosaic, out_path: str, px_per_mm: float = 0.05) -> str:
    """Отрисовать Mosaic в PNG. Вернуть путь.

    Каждый Cell — залитый полигон цветом aci2rgb(cell.aci). Ось Y обычно вверх
    (в САПР), у изображений вниз — не забыть перевернуть. Реализация свободная
    (PIL/opencv/matplotlib). Цель — быстрая визуальная валидация, не красота.
    """
    if px_per_mm <= 0:
        raise ValueError("px_per_mm должен быть положительным")
    if not mosaic.cells:
        raise ValueError("нельзя отрисовать пустую мозаику")

    xmin, ymin, xmax, ymax = mosaic.bbox
    padding = 2
    width = max(1, round((xmax - xmin) * px_per_mm) + 2 * padding)
    height = max(1, round((ymax - ymin) * px_per_mm) + 2 * padding)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    def to_pixel(point: tuple[float, float]) -> tuple[float, float]:
        x, y = point
        return (
            padding + (x - xmin) * px_per_mm,
            padding + (ymax - y) * px_per_mm,
        )

    for cell in mosaic.cells:
        try:
            rgb = tuple(aci2rgb(cell.aci))
        except (IndexError, ValueError):
            rgb = (0, 0, 0)
        draw.polygon([to_pixel(point) for point in cell.poly], fill=rgb)

    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG")
    return str(destination)
