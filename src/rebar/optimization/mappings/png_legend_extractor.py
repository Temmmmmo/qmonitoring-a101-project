"""Автоматическое извлечение шкалы армирования из PNG-легенды ЛИРА.

Алгоритм:
1. Найти цветные полосы легенды в верхней части PNG (горизонтальные сегменты).
2. Извлечь числовые пороги As под полосами.
3. Вернуть RebarMapping с порогами для последующей верификации.

Примечание: Автоматический OCR текстовых подписей (sNNNdNN) в LIRA-PNG ненадежен
из-за малого размера шрифта (~6px). Рекомендуется использовать извлеченные пороги
для верификации вручную созданной таблицы или как базовую информацию для создания
нового mapping.

Структура легенды:
- Цветные полосы идут горизонтально, каждая занимает свой x-диапазон.
- Числовые пороги находятся под полосами (y > band_y).
"""

from __future__ import annotations

import re
from pathlib import Path

import cv2
import numpy as np

from rebar.legend import parse_recipe
from rebar.standards import A101_242_PARKING_SLAB_T200_220

from .contracts import RebarBandMapping, RebarMapping

# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

_FONT_PATH = "/System/Library/Fonts/Supplemental/Verdana Bold.ttf"
_FONT_SIZE = 8

_templates_cache: dict[str, np.ndarray] | None = None


def _render_template(char: str) -> np.ndarray:
    """Render a single character as a binary numpy array."""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(_FONT_PATH, _FONT_SIZE)
    img = Image.new("L", (16, 14), 255)
    draw = ImageDraw.Draw(img)
    draw.text((1, 0), char, font=font, fill=0)
    arr = np.array(img)
    cols = np.where(arr.min(axis=0) < 128)[0]
    rows = np.where(arr.min(axis=1) < 128)[0]
    if len(cols) == 0 or len(rows) == 0:
        return arr
    return arr[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]


def _get_templates() -> dict[str, np.ndarray]:
    """Get or build the character template library."""
    global _templates_cache
    if _templates_cache is None:
        _templates_cache = {}
        for ch in "0123456789sd+":
            _templates_cache[ch] = _render_template(ch)
    return _templates_cache


def _match_template(query: np.ndarray, templates: dict[str, np.ndarray],
                    threshold: float = 0.55) -> str:
    """Match a query image against all templates, return best character."""
    query = query.astype(np.float32)
    best_score = -1.0
    best_char = ""

    for ch, tmpl in templates.items():
        t = tmpl.astype(np.float32)
        if query.shape[0] < t.shape[0] or query.shape[1] < t.shape[1]:
            continue
        res = cv2.matchTemplate(query, t, cv2.TM_CCOEFF_NORMED)
        score = float(res.max())
        if score > best_score:
            best_score = score
            best_char = ch

    if best_score >= threshold:
        return best_char
    return ""


# ---------------------------------------------------------------------------
# Band detection
# ---------------------------------------------------------------------------


def _find_horizontal_bands(image: np.ndarray) -> list[tuple[int, int, int, int, tuple[int, int, int]]]:
    """Find horizontal color segments in the top region of the image.

    Returns list of (y_start, y_end, x_start, x_end, mean_rgb) tuples.
    The legend has multiple colored segments side by side on the same row.
    """
    h, w = image.shape[:2]
    scan_rows = min(h // 5, 60)

    colored_rows: list[int] = []
    for y in range(scan_rows):
        row = image[y, :, :]
        has_color = False
        for x in range(0, w, max(1, w // 100)):
            r, g, b = int(row[x, 0]), int(row[x, 1]), int(row[x, 2])
            max_c = max(r, g, b)
            min_c = min(r, g, b)
            is_white = r > 230 and g > 230 and b > 230
            is_black = r < 20 and g < 20 and b < 20
            is_gray = (max_c - min_c) < 25
            if not is_white and not is_black and not is_gray and max_c > 50:
                has_color = True
                break
        if has_color:
            colored_rows.append(y)

    if not colored_rows:
        return []

    y_groups: list[tuple[int, int]] = []
    current_start = colored_rows[0]
    current_end = colored_rows[0]
    for y in colored_rows[1:]:
        if y == current_end + 1:
            current_end = y
        else:
            if current_end - current_start >= 2:
                y_groups.append((current_start, current_end))
            current_start = y
            current_end = y
    if current_end - current_start >= 2:
        y_groups.append((current_start, current_end))

    if not y_groups:
        return []

    y_start, y_end = max(y_groups, key=lambda g: g[1] - g[0])

    bands: list[tuple[int, int, int, int, tuple[int, int, int]]] = []
    for y in range(y_start, y_end + 1):
        row = image[y, :, :]
        prev_color = None
        seg_start = 0
        for x in range(w):
            r, g, b = int(row[x, 0]), int(row[x, 1]), int(row[x, 2])
            max_c = max(r, g, b)
            min_c = min(r, g, b)
            is_white = r > 230 and g > 230 and b > 230
            is_black = r < 20 and g < 20 and b < 20
            is_gray = (max_c - min_c) < 25

            if is_white or is_black or is_gray:
                if prev_color is not None and x - seg_start > 20:
                    seg_pixels = image[y, seg_start:x, :]
                    mean = seg_pixels.mean(axis=0).astype(int)
                    bands.append((y_start, y_end + 1, seg_start, x,
                                  (int(mean[0]), int(mean[1]), int(mean[2]))))
                prev_color = None
                seg_start = x + 1
            else:
                if prev_color is None:
                    prev_color = (r, g, b)
                    seg_start = x

        if prev_color is not None and w - seg_start > 20:
            seg_pixels = image[y, seg_start:w, :]
            mean = seg_pixels.mean(axis=0).astype(int)
            bands.append((y_start, y_end + 1, seg_start, w,
                          (int(mean[0]), int(mean[1]), int(mean[2]))))

    unique_bands: list[tuple[int, int, int, int, tuple[int, int, int]]] = []
    seen_x_ranges: list[tuple[int, int]] = []
    for band in bands:
        _, _, x1, x2, _ = band
        is_duplicate = any(x1 < sx2 and x2 > sx1 for sx1, sx2 in seen_x_ranges)
        if not is_duplicate:
            unique_bands.append(band)
            seen_x_ranges.append((x1, x2))

    return unique_bands


# ---------------------------------------------------------------------------
# Label OCR via template matching
# ---------------------------------------------------------------------------


def _extract_labels(
    image: np.ndarray,
    bands: list[tuple[int, int, int, int, tuple[int, int, int]]],
) -> list[str]:
    """Extract text labels from the region above each color band."""
    labels: list[str] = []

    for y_start, y_end, x_start, x_end, _ in bands:
        label_y_end = y_start
        label_y_start = max(0, y_start - 30)
        if label_y_end - label_y_start < 2:
            labels.append("")
            continue

        x_margin = 15
        label_region = image[
            label_y_start:label_y_end,
            max(0, x_start - x_margin):min(image.shape[1], x_end + x_margin),
            :,
        ]
        label_text = _ocr_s_pattern(label_region)
        labels.append(label_text)

    return labels


def _ocr_s_pattern(region: np.ndarray) -> str:
    """Recognize sNNNdNNN[+sNNNdNNN]* via template matching on segmented chars."""
    if region.size == 0:
        return ""

    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 128, 255, cv2.THRESH_BINARY_INV)

    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return ""

    chars: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        if 3 < ch < 30 and 2 < cw < 25:
            chars.append((x, y, cw, ch))

    if not chars:
        return ""

    chars.sort(key=lambda c: c[0])

    # Group into tokens: gap > 8px means new token
    tokens: list[list[tuple[int, int, int, int]]] = []
    current_token: list[tuple[int, int, int, int]] = []
    prev_x_end = -100

    for (x, y, cw, ch) in chars:
        if current_token and x - prev_x_end > 8:
            tokens.append(current_token)
            current_token = []
        current_token.append((x, y, cw, ch))
        prev_x_end = x + cw
    if current_token:
        tokens.append(current_token)

    templates = _get_templates()

    recognized: list[str] = []
    for token in tokens:
        token_text = _recognize_token(thresh, token, templates)
        if token_text:
            recognized.append(token_text)

    full_text = "".join(recognized)
    matches = re.findall(r"s(\d+)d(\d+)", full_text, re.IGNORECASE)
    if matches:
        return "+".join(f"s{step}d{dia}" for step, dia in matches)

    full_text_spaced = " ".join(recognized)
    matches = re.findall(r"s(\d+)d(\d+)", full_text_spaced, re.IGNORECASE)
    if matches:
        return "+".join(f"s{step}d{dia}" for step, dia in matches)

    return ""


def _recognize_token(
    thresh: np.ndarray,
    chars: list[tuple[int, int, int, int]],
    templates: dict[str, np.ndarray],
) -> str:
    """Recognize a token by matching each character segment against templates."""
    token_text = ""
    for (x, y, cw, ch) in chars:
        char_img = thresh[y:y + ch, x:x + cw]
        if char_img.size == 0:
            continue
        ch_class = _match_template(char_img, templates)
        token_text += ch_class
    return token_text


# ---------------------------------------------------------------------------
# Threshold extraction
# ---------------------------------------------------------------------------


def _extract_thresholds(
    image: np.ndarray,
    bands: list[tuple[int, int, int, int, tuple[int, int, int]]],
) -> list[float]:
    """Extract numeric thresholds from the region below the color bands."""
    if not bands:
        return []

    h, w = image.shape[:2]
    thresholds: list[float] = []

    band_y_end = max(b[1] for b in bands)
    search_y_start = band_y_end
    search_y_end = min(h, band_y_end + 30)

    if search_y_end <= search_y_start:
        return thresholds

    threshold_region = image[search_y_start:search_y_end, :, :]
    if threshold_region.size == 0:
        return thresholds

    gray = cv2.cvtColor(threshold_region, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 128, 255, cv2.THRESH_BINARY_INV)

    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    char_boxes: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        if 4 < ch < 25 and 3 < cw < 15:
            char_boxes.append((x, y, cw, ch))

    if not char_boxes:
        return thresholds

    char_boxes.sort(key=lambda c: c[0])

    numbers: list[list[tuple[int, int, int, int]]] = []
    current_num: list[tuple[int, int, int, int]] = []
    prev_x_end = -100

    for (x, y, cw, ch) in char_boxes:
        if current_num and x - prev_x_end > 6:
            numbers.append(current_num)
            current_num = []
        current_num.append((x, y, cw, ch))
        prev_x_end = x + cw
    if current_num:
        numbers.append(current_num)

    templates = _get_templates()

    for chars in numbers:
        num_str = _recognize_number(thresh, chars, templates)
        if num_str:
            try:
                val = float(num_str)
                if 0 < val < 200:
                    thresholds.append(val)
            except ValueError:
                pass

    thresholds.sort()
    return thresholds


def _recognize_number(
    thresh: np.ndarray,
    chars: list[tuple[int, int, int, int]],
    templates: dict[str, np.ndarray],
) -> str:
    """Recognize a number from character bounding boxes."""
    num_str = ""
    for (x, y, cw, ch) in chars:
        char_img = thresh[y:y + ch, x:x + cw]
        if char_img.size == 0:
            continue

        if ch <= 6 and cw <= 6:
            dot_ratio = np.sum(char_img) / (char_img.size * 255)
            if dot_ratio > 0.5:
                num_str += "."
                continue

        digit = _match_template(char_img, templates)
        if digit.isdigit():
            num_str += digit

    return num_str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_legend_from_png(
    png_path: str | Path,
    *,
    mapping_id: str = "auto-png-legend",
    source_description: str = "",
) -> RebarMapping:
    """Extract a RebarMapping from a LIRA PNG legend.

    Args:
        png_path: Path to the PNG file containing the legend.
        mapping_id: ID for the generated mapping.
        source_description: Human-readable source description.

    Returns:
        A RebarMapping with bands extracted from the PNG.

    Raises:
        ValueError: If the legend cannot be parsed.
        FileNotFoundError: If the PNG file does not exist.
    """
    png_path = Path(png_path)
    if not png_path.exists():
        raise FileNotFoundError(f"PNG not found: {png_path}")

    image = cv2.imread(str(png_path))
    if image is None:
        raise ValueError(f"Could not read image: {png_path}")

    image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

    bands = _find_horizontal_bands(image_rgb)
    if len(bands) < 2:
        raise ValueError(
            f"Could not find enough legend bands in {png_path.name} "
            f"(found {len(bands)}, need at least 2)"
        )

    labels = _extract_labels(image_rgb, bands)
    thresholds = _extract_thresholds(image_rgb, bands)

    band_mappings: list[RebarBandMapping] = []
    valid_count = 0

    for i, (y_start, y_end, x_start, x_end, color) in enumerate(bands):
        label = labels[i] if i < len(labels) else ""
        if not label:
            continue

        try:
            recipe = parse_recipe(label)
        except (ValueError, Exception):
            continue

        if valid_count < len(thresholds):
            threshold = thresholds[valid_count]
        else:
            threshold = 10.0 + valid_count * 10.0

        background = recipe.background
        additional = recipe.additions[0] if len(recipe.additions) == 1 else None

        band_mappings.append(RebarBandMapping(
            label=label,
            threshold_as=threshold,
            background=background,
            additional=additional,
        ))
        valid_count += 1

    if not band_mappings:
        raise ValueError(f"No valid legend bands could be parsed from {png_path.name}")

    scale_bounds = tuple(bm.threshold_as for bm in band_mappings)

    return RebarMapping(
        id=mapping_id,
        source=source_description or f"Auto-extracted from {png_path.name}",
        status="auto_extracted_png",
        a101_profile_id=A101_242_PARKING_SLAB_T200_220.id,
        expected_scale_bounds_as=scale_bounds,
        bands=tuple(band_mappings),
    )
