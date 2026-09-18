"""Read reinforcement recipes from the narrow legend above a LIRA PNG.

DXF remains the source of geometry, colour order and numerical As boundaries.
The raster supplies the reinforcement recipes; every occupied DXF colour must
have an explicitly recognised recipe. No guessed or default recipes are used.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from threading import Lock

import numpy as np
from ezdxf.colors import aci2rgb
from PIL import Image

from .legend import parse_recipe
from .models import Band, Cell, Mosaic

_LABEL = re.compile(r"s\d+d\d+(?:\+s\d+d\d+)*", re.IGNORECASE)
_OCR_LOCK = Lock()


@lru_cache(maxsize=1)
def _legend_ocr():
    # RapidOCR constructs ONNX sessions for all three modules even when a
    # module is disabled. Keep one instance for all four directions and avoid
    # a CPU-sized thread pool per session inside the container.
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR(use_det=False, use_cls=False, use_rec=True,
                    intra_op_num_threads=1, inter_op_num_threads=1)


def _raw_runs(row: np.ndarray) -> list[tuple[int, int, tuple[int, int, int]]]:
    changes = np.flatnonzero(np.any(row[1:] != row[:-1], axis=1)) + 1
    edges = np.r_[0, changes, len(row)]
    return [(int(start), int(end), tuple(int(value) for value in row[start]))
            for start, end in zip(edges[:-1], edges[1:])]


def _runs(row: np.ndarray) -> list[tuple[int, int, tuple[int, int, int]]]:
    return [(start, end, rgb) for start, end, rgb in _raw_runs(row)
            if end - start >= 25 and max(rgb) - min(rgb) > 20 and min(rgb) < 240]


def _legend_bars(image: np.ndarray, aci_order: list[int]) -> tuple[int, list[tuple[int, int, tuple[int, int, int]]]]:
    candidates = ((y, _runs(image[y])) for y in range(4, min(80, len(image))))
    y, chromatic = max(candidates, key=lambda item: sum(end - start for start, end, _ in item[1]))
    if len(chromatic) < 2:
        raise ValueError("в верхней части PNG не найдена цветовая шкала ЛИРА")
    # ACI 9 is a real gray LIRA band. Its RGB is identical to the gray canvas,
    # so only an interior, framed segment of normal bar width can establish it.
    # DXF's ordered palette must independently confirm its colour and position.
    row = image[y]
    raw = _raw_runs(row)
    bars = [chromatic[0]]
    for right in chromatic[1:]:
        left = bars[-1]
        interior = [(start, end, rgb) for start, end, rgb in raw
                    if left[1] < start and end < right[0] and end - start >= 25
                    and max(rgb) - min(rgb) <= 20 and max(rgb) < 235]
        if len(interior) == 1 and len(bars) < len(aci_order):
            start, end, rgb = interior[0]
            width = end - start
            left_width, right_width = left[1] - left[0], right[1] - right[0]
            left_gap, right_gap = row[left[1]:start], row[end:right[0]]
            if (abs(width - left_width) <= max(5, left_width * 0.2)
                    and abs(width - right_width) <= max(5, right_width * 0.2)
                    and 1 <= len(left_gap) <= 5 and 1 <= len(right_gap) <= 5
                    and np.any(np.max(left_gap, axis=1) < 30)
                    and np.any(np.max(right_gap, axis=1) < 30)
                    and max(abs(actual - expected) for actual, expected
                            in zip(rgb, aci2rgb(aci_order[len(bars)]))) <= 55):
                bars.append((start, end, rgb))
        bars.append(right)
    return y, bars


def _read_text(ocr, crop: np.ndarray) -> tuple[str, float]:
    result, _elapsed = ocr(crop)
    if not result or len(result) != 1:
        return "", 0.0
    item = result[0]
    label, confidence = (item[0], item[1]) if len(item) == 2 else (item[1], item[2])
    return label.strip().replace(" ", ""), float(confidence)


def apply_png_legend(mosaic: Mosaic, png_path: str | Path) -> Mosaic:
    """Bind PNG recipes to a DXF mosaic, rejecting incomplete or mismatched scales."""
    if mosaic.legend or any(cell.band is not None for cell in mosaic.cells):
        raise ValueError("у DXF уже есть шкала армирования; PNG не должен её перезаписывать")
    path = Path(png_path)
    name = unicodedata.normalize("NFKC", path.stem).casefold()
    image_axis = re.search(r"оси[_\s-]*([xyху])", name)
    image_layer = "top" if "верх" in name else "bottom" if "ниж" in name else None
    if image_axis and image_layer:
        axis = "X" if image_axis.group(1) in {"x", "х"} else "Y"
        if axis != mosaic.direction.axis.value or image_layer != mosaic.direction.layer.value:
            raise ValueError("направление PNG не соответствует направлению DXF")
    with Image.open(path) as source:
        if source.format != "PNG":
            raise ValueError("файл шкалы не является PNG")
        # The bars and their labels are confined to the top 80 pixels. Full
        # LIRA screenshots are much larger and add no information to OCR.
        image = np.asarray(source.crop((0, 0, source.width, min(80, source.height))).convert("RGB"))
    aci_order = mosaic.meta.get("scale_aci_order", [])
    bounds = mosaic.meta.get("scale_bounds_as", [])
    y, bars = _legend_bars(image, aci_order)
    if len(bounds) != len(aci_order) + 1 or len(bars) > len(aci_order):
        raise ValueError("число полос PNG не согласуется со шкалой DXF")
    occupied = {cell.aci for cell in mosaic.cells}
    omitted = set(aci_order[len(bars):]) & occupied
    if omitted:
        raise ValueError(f"PNG не содержит используемые цвета КЭ: {sorted(omitted)}")

    # Raster palette is close to, but not always identical with, ezdxf's ACI RGB.
    for index, (_start, _end, colour) in enumerate(bars):
        reference = aci2rgb(aci_order[index])
        if max(abs(actual - expected) for actual, expected in zip(colour, reference)) > 55:
            raise ValueError(f"цвет полосы PNG {index + 1} не соответствует DXF")

    bands: list[Band] = []
    for index, (start, end, colour) in enumerate(bars):
        mid = (start + end) // 2
        top = y
        while top > 0 and tuple(image[top - 1, mid]) == colour:
            top -= 1
        if top < 8:
            raise ValueError("над цветовой шкалой PNG недостаточно места для подписей")
        # LIRA anchors each recipe near the right edge of its colour band;
        # long labels continue into the following band. Include that overhang
        # without the next band's label, which sits near its own right edge.
        wide = image[:top, max(0, end - 50):min(image.shape[1], end + 100)]
        narrow = image[max(0, top - 16):top, max(0, end - 80):min(image.shape[1], end + 80)]
        label, confidence = "", 0.0
        # Some screenshots rasterise a 10--15 px label so thinly that OCR
        # drops digits. Only resize the original pixels; never infer a recipe
        # from its colour or its neighbouring DXF band.
        for crop, scale in ((wide, 1), (narrow, 1), (narrow, 2), (wide, 2), (narrow, 3), (wide, 3)):
            sample = crop if scale == 1 else np.asarray(Image.fromarray(crop).resize(
                (crop.shape[1] * scale, crop.shape[0] * scale), Image.Resampling.LANCZOS))
            with _OCR_LOCK:
                label, confidence = _read_text(_legend_ocr(), sample)
            if confidence >= 0.75 and _LABEL.fullmatch(label):
                break
        else:
            raise ValueError(f"не удалось надёжно прочитать подпись полосы PNG {index + 1}: {label!r}")
        recipe = parse_recipe(label)
        bands.append(Band(
            index=index, aci=aci_order[index], label=label,
            threshold_as=float(bounds[index]), background=recipe.background,
            additional=recipe.additions[0] if len(recipe.additions) == 1 else None,
            recipe=recipe,
        ))

    by_aci = {band.aci: band for band in bands}
    cells = [Cell(poly=list(cell.poly), centroid=cell.centroid, aci=cell.aci, band=by_aci[cell.aci]) for cell in mosaic.cells]
    meta = dict(mosaic.meta)
    meta["png_legend_path"] = str(path)
    meta["legend_source"] = "png"
    meta["png_legend_labels"] = [band.label for band in bands]
    return Mosaic(direction=mosaic.direction, cells=cells, legend=bands, bbox=mosaic.bbox,
                  source_path=mosaic.source_path, meta=meta)
