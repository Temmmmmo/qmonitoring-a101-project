"""Стадия A — разбор цветовой шкалы (легенды).

Источники:
  * .shk  — бинарь ЛИРА: пороги As (float32 LE) + подписи ("s300d18+s150d20"), по порядку.
  * DXF блок KLEENKA — ACI-цвета полос в порядке шкалы.
  * ezdxf.colors.aci2rgb — ACI -> RGB (для сверки с PNG).

Результат — list[Band] (см. models.Band), упорядоченный от фона к максимуму.

parse_label реализован (семантика фиксирована). parse_shk / build_legend — TODO для Codex.
См. docs/context.md §2.2, §3 и docs/stage-a-parser-spec.md.
"""

from __future__ import annotations

import math
import re
import struct
from itertools import pairwise
from pathlib import Path

from .models import Band, Rebar

_SPEC_RE = re.compile(r"s(\d+)d(\d+)", re.IGNORECASE)


def parse_spec(token: str) -> Rebar:
    """'s300d18' -> Rebar(step=300, diameter=18)."""
    m = _SPEC_RE.fullmatch(token.strip())
    if not m:
        raise ValueError(f"не распознан спецификатор арматуры: {token!r}")
    return Rebar(step=int(m.group(1)), diameter=int(m.group(2)))


def parse_label(label: str) -> tuple[Rebar, Rebar | None]:
    """Разобрать подпись полосы шкалы.

    's300d18'            -> (background=s300d18, additional=None)   # только фон
    's300d18+s150d20'    -> (background=s300d18, additional=s150d20)# фон + добавка

    Раскладываем ТОЛЬКО additional. Если частей больше двух — берём первую как фон,
    последнюю как добавку (уточнить на реальных данных, если встретится).
    """
    parts = [p for p in label.replace(" ", "").split("+") if p]
    if not parts:
        raise ValueError(f"пустая подпись: {label!r}")
    background = parse_spec(parts[0])
    additional = parse_spec(parts[-1]) if len(parts) > 1 else None
    return background, additional


def parse_shk(path: str) -> list[tuple[float, str]]:
    """Разобрать .shk -> упорядоченный список (порог_As, подпись).

    Формат (reverse-engineered, docs/context.md §2.2): пары float32 LE = пороги,
    ASCII-подписи через re.finditer(rb'[ -~]{5,}'). Подпись хранится дважды —
    дедуплицировать. Порядок = порядок полос шкалы.
    """
    data = Path(path).read_bytes()

    # Запись перехода шкалы имеет вид:
    #   float32 lower, float32 upper, uint16 index,
    #   uint8 current_label_len, current_label,
    #   [uint8 next_label_len, next_label]
    # Первый и последний служебные блоки немного отличаются, поэтому ищем
    # структурно валидные переходы, а не полагаемся на фиксированный offset.
    label_bytes_re = re.compile(rb"s\d+d\d+(?:\+s\d+d\d+)*", re.IGNORECASE)
    records: list[tuple[int, float, float, str, str | None]] = []

    for offset in range(max(0, len(data) - 10)):
        try:
            lower, upper, _record_index, label_size = struct.unpack_from(
                "<ffHB", data, offset
            )
        except struct.error:
            break

        if not (
            math.isfinite(lower)
            and math.isfinite(upper)
            and 0.0 < lower <= upper < 1_000_000.0
            and 0 < label_size <= 100
        ):
            continue

        label_start = offset + 11
        label_end = label_start + label_size
        current_raw = data[label_start:label_end]
        if not label_bytes_re.fullmatch(current_raw):
            continue

        next_label: str | None = None
        if label_end < len(data):
            next_size = data[label_end]
            next_start = label_end + 1
            next_end = next_start + next_size
            next_raw = data[next_start:next_end]
            if 0 < next_size <= 100 and label_bytes_re.fullmatch(next_raw):
                next_label = next_raw.decode("ascii")

        records.append(
            (
                offset,
                float(lower),
                float(upper),
                current_raw.decode("ascii"),
                next_label,
            )
        )

    if not records:
        raise ValueError(f"не удалось найти записи цветовой шкалы в .shk: {path}")

    # В каждом переходе повторяются текущая и следующая подписи. Словарь в
    # Python сохраняет порядок вставки, поэтому получаем ровно один Band на
    # подпись и одновременно восстанавливаем порог последней записи.
    parsed: dict[str, float] = {}
    for _offset, lower, upper, current, following in records:
        parsed.setdefault(current, lower)
        if following is not None:
            parsed.setdefault(following, upper)

    result = [(threshold, label) for label, threshold in parsed.items()]
    if any(a[0] >= b[0] for a, b in pairwise(result)):
        raise ValueError(f"пороги .shk не возрастают: {path}")
    return result


def build_legend(shk_path: str, aci_order: list[int] | None = None) -> list[Band]:
    """Собрать легенду: подписи+пороги из .shk, цвета (ACI) из DXF по порядку.

    aci_order — ACI-индексы полос в порядке шкалы (из dxf_ingest, блок KLEENKA).
    Если None — Band.aci остаётся None (напр. при разборе только .shk).

    ВАЛИДАЦИЯ: число полос в .shk должно согласовываться с числом цветов в DXF;
    при расхождении — понятная ошибка/лог (см. открытый вопрос §9.3 в context.md).
    """
    parsed = parse_shk(shk_path)
    if aci_order is not None and len(aci_order) != len(parsed):
        raise ValueError(
            "число полос не совпадает: "
            f"в {shk_path!r} найдено {len(parsed)}, в DXF — {len(aci_order)}"
        )

    bands: list[Band] = []
    for index, (threshold_as, label) in enumerate(parsed):
        background, additional = parse_label(label)
        bands.append(
            Band(
                index=index,
                aci=aci_order[index] if aci_order is not None else None,
                label=label,
                threshold_as=threshold_as,
                background=background,
                additional=additional,
            )
        )
    return bands
