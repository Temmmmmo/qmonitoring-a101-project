"""DXF ingest: цветная КЭ-мозаика ЛИРА -> :class:`Mosaic`.

Внутренние координаты всегда миллиметры. В реальных выгрузках каждый КЭ
присутствует дважды: результат на слое ``KLEENKA`` и служебная копия ACI 5 на
``PLAST``. Ячейками мозаики являются только сущности ``KLEENKA``.
"""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from collections import Counter
from pathlib import Path

import ezdxf
from ezdxf import units

from .legend import build_legend, parse_shk
from .models import Axis, Cell, Direction, Layer, Mosaic, Point

M_TO_MM = 1000.0


def _normalise_name(name: str) -> str:
    return unicodedata.normalize("NFKC", Path(name).stem).casefold()


def direction_from_filename(name: str) -> Direction:
    """Определить слой и ось из русских/латинских вариантов имени файла."""
    text = _normalise_name(name)

    bottom = "нижн" in text or re.search(r"(?:^|[^a-zа-я])низ(?:$|[^a-zа-я])", text)
    top = "верх" in text
    if bool(bottom) == bool(top):
        raise ValueError(f"не удалось однозначно определить слой армирования: {name!r}")

    raw_axes = set(re.findall(r"(?<![a-zа-я0-9])([xyху])(?![a-zа-я0-9])", text))
    axes = {"x" if axis in {"x", "х"} else "y" for axis in raw_axes}
    if "буквен" in text:
        axes.add("x")
    if "цифров" in text:
        axes.add("y")
    if len(axes) != 1:
        raise ValueError(f"не удалось однозначно определить ось X/Y: {name!r}")

    return Direction(
        layer=Layer.BOTTOM if bottom else Layer.TOP,
        axis=Axis.X if axes.pop() == "x" else Axis.Y,
    )


def _scale_entities(doc) -> tuple[list, list[float]]:
    try:
        block = doc.blocks.get("KLEENKA")
    except Exception as exc:  # pragma: no cover - защита от повреждённого DXF
        raise ValueError("в DXF отсутствует блок KLEENKA") from exc
    if block is None:
        raise ValueError("в DXF отсутствует блок KLEENKA")

    solids = list(block.query("SOLID"))
    if not solids:
        raise ValueError("в блоке KLEENKA не найдены цветовые SOLID")

    # Шкала горизонтальная: слева минимальный уровень, справа максимальный.
    solids.sort(
        key=lambda entity: sum(float(getattr(entity.dxf, f"vtx{i}").x) for i in range(4))
        / 4.0
    )

    tagged_bounds: list[tuple[int, float]] = []
    for insert in doc.modelspace().query("INSERT"):
        if str(insert.dxf.name).casefold() != "kleenka":
            continue
        for attrib in insert.attribs:
            match = re.fullmatch(r"(\d+)S", str(attrib.dxf.tag), re.IGNORECASE)
            if not match:
                continue
            raw = str(attrib.dxf.text).strip().replace(",", ".")
            try:
                tagged_bounds.append((int(match.group(1)), float(raw)))
            except ValueError:
                continue
        break

    bounds = [value for _index, value in sorted(tagged_bounds)]
    return solids, bounds


def extract_scale_aci_order(doc) -> list[int]:
    """Вернуть ACI полос шкалы от минимального уровня к максимальному."""
    solids, _bounds = _scale_entities(doc)
    return [int(entity.dxf.color) for entity in solids]


def _face_points_raw(face) -> list[tuple[float, float, float]]:
    points: list[tuple[float, float, float]] = []
    for index in range(4):
        vertex = getattr(face.dxf, f"vtx{index}")
        point = (float(vertex.x), float(vertex.y), float(vertex.z))
        if point not in points:
            points.append(point)
    return points


def _unit_scale_to_mm(doc, faces: list) -> tuple[float, str, str]:
    """Определить единицы DXF и вернуть коэффициент перевода в миллиметры."""
    insert_units = doc.header.get("$INSUNITS")
    if insert_units not in (None, 0):
        try:
            factor = float(units.conversion_factor(int(insert_units), units.MM))
            name = units.unit_name(int(insert_units)) or str(insert_units)
            return factor, name, "dxf_header"
        except (TypeError, ValueError):
            pass

    edge_lengths: list[float] = []
    for face in faces:
        points = _face_points_raw(face)
        for first, second in zip(points, points[1:] + points[:1]):
            length = math.hypot(second[0] - first[0], second[1] - first[1])
            if length > 1e-9:
                edge_lengths.append(length)

    if not edge_lengths:
        raise ValueError("не удалось определить единицы: в DXF нет ненулевых рёбер")

    median_edge = statistics.median(edge_lengths)
    # Для плит характерный КЭ имеет размер порядка 0.1–1 м или 100–1000 мм.
    # Порог 10 устойчиво разделяет оба наблюдаемых представления и не зависит
    # от общего габарита плиты.
    if median_edge < 10.0:
        return M_TO_MM, "Meters (inferred)", "heuristic_edge_length"
    return 1.0, "Millimeters (inferred)", "heuristic_edge_length"


def detect_units_scale(doc, msp) -> float:
    """Вернуть множитель перевода координат modelspace в миллиметры.

    Публичный диагностический helper; подробности определения также записываются
    `read_mosaic()` в `Mosaic.meta`.
    """
    faces = [
        face for face in msp.query("3DFACE") if str(face.dxf.layer).casefold() == "kleenka"
    ]
    if not faces:
        faces = list(msp.query("3DFACE"))
    return _unit_scale_to_mm(doc, faces)[0]


def _polygon_centroid(points: list[Point]) -> Point:
    twice_area = 0.0
    cx = 0.0
    cy = 0.0
    for first, second in zip(points, points[1:] + points[:1]):
        cross = first[0] * second[1] - second[0] * first[1]
        twice_area += cross
        cx += (first[0] + second[0]) * cross
        cy += (first[1] + second[1]) * cross

    if abs(twice_area) < 1e-9:
        return (
            sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points),
        )
    return cx / (3.0 * twice_area), cy / (3.0 * twice_area)


def _candidate_axis(path: Path) -> Axis | None:
    text = _normalise_name(path.name).replace("х", "x").replace("у", "y")
    if re.search(r"(?:^|[_\s-])в?x(?:$|[_\s-])", text):
        return Axis.X
    if re.search(r"(?:^|[_\s-])в?y(?:$|[_\s-])", text):
        return Axis.Y
    return None


def _nearby_shk_files(dxf_path: Path) -> list[Path]:
    parent = dxf_path.parent
    candidates = list(parent.glob("*.shk"))
    candidates.extend(parent.glob("*/*.shk"))
    candidates.extend(parent.glob("*/*/*.shk"))
    return sorted(set(candidates))


def _find_matching_shk(dxf_path: Path, aci_count: int, axis: Axis) -> Path | None:
    matching: list[Path] = []
    for candidate in _nearby_shk_files(dxf_path):
        try:
            if len(parse_shk(str(candidate))) == aci_count:
                matching.append(candidate)
        except (OSError, ValueError):
            continue

    if len(matching) == 1:
        return matching[0]
    by_axis = [candidate for candidate in matching if _candidate_axis(candidate) == axis]
    if len(by_axis) == 1:
        return by_axis[0]
    return None


def read_mosaic(dxf_path: str, shk_path: str | None = None, *, auto_shk: bool = True) -> Mosaic:
    """Прочитать DXF и, при наличии совместимого `.shk`, заполнить легенду."""
    source = Path(dxf_path)
    doc = ezdxf.readfile(source)
    direction = direction_from_filename(source.name)

    all_faces = list(doc.modelspace().query("3DFACE"))
    result_faces = [
        face for face in all_faces if str(face.dxf.layer).casefold() == "kleenka"
    ]
    if not result_faces:
        raise ValueError(f"в DXF нет 3DFACE слоя KLEENKA: {source}")

    scale_to_mm, source_units, unit_detection = _unit_scale_to_mm(doc, result_faces)
    solids, scale_bounds = _scale_entities(doc)
    aci_order = [int(entity.dxf.color) for entity in solids]

    selected_shk: Path | None
    if shk_path is not None:
        selected_shk = Path(shk_path)
    elif auto_shk:
        selected_shk = _find_matching_shk(source, len(aci_order), direction.axis)
    else:
        selected_shk = None

    legend = build_legend(str(selected_shk), aci_order) if selected_shk else []
    band_by_aci = {band.aci: band for band in legend if band.aci is not None}

    cells: list[Cell] = []
    vertex_counts: Counter[int] = Counter()
    z_values: list[float] = []
    for face in result_faces:
        raw_points = _face_points_raw(face)
        vertex_counts[len(raw_points)] += 1
        z_values.extend(point[2] * scale_to_mm for point in raw_points)
        poly = [(x * scale_to_mm, y * scale_to_mm) for x, y, _z in raw_points]
        aci = int(face.dxf.color)
        cells.append(
            Cell(
                poly=poly,
                centroid=_polygon_centroid(poly),
                aci=aci,
                band=band_by_aci.get(aci),
            )
        )

    xs = [point[0] for cell in cells for point in cell.poly]
    ys = [point[1] for cell in cells for point in cell.poly]
    bbox = (min(xs), min(ys), max(xs), max(ys))
    cell_colors = Counter(cell.aci for cell in cells)
    scale_intervals = []
    if len(scale_bounds) == len(aci_order) + 1:
        scale_intervals = [
            {
                "index": index,
                "aci": aci,
                "lower_as": scale_bounds[index],
                "upper_as": scale_bounds[index + 1],
            }
            for index, aci in enumerate(aci_order)
        ]

    meta = {
        "source_format": "dxf",
        "units": "mm",
        "source_units": source_units,
        "unit_scale_to_mm": scale_to_mm,
        "unit_detection": unit_detection,
        "dxf_version": doc.dxfversion,
        "raw_3dface_count": len(all_faces),
        "ignored_plast_count": sum(
            str(face.dxf.layer).casefold() == "plast" for face in all_faces
        ),
        "triangle_count": vertex_counts[3],
        "quad_count": vertex_counts[4],
        "z_range_mm": (min(z_values), max(z_values)),
        "scale_aci_order": aci_order,
        "scale_bounds_as": scale_bounds,
        "scale_intervals": scale_intervals,
        "shk_path": str(selected_shk) if selected_shk else None,
        "unmatched_aci_counts": {
            aci: count for aci, count in cell_colors.items() if aci not in set(aci_order)
        },
    }

    return Mosaic(
        direction=direction,
        cells=cells,
        legend=legend,
        bbox=bbox,
        source_path=str(source),
        meta=meta,
    )
