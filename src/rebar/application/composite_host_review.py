"""Офлайн-предпроверка контура из Reference Probe, не доказательство полноты Solid."""
from __future__ import annotations

import math

from rebar.optimization.contracts.host import RectangularHostEnvelope
from rebar.optimization.services.composite_host import evaluate_composite_host, evaluate_host_demand_feasibility
from rebar.optimization.services.geometry import bboxes_overlap

from .composite_layout_review import build_review_zones

HOST_COORDINATE_POLICY = "explicit-source-xy-equals-reference-xy/research-v1"
TOLERANCE = 0.01


def _point(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value)):
        raise ValueError("ожидалась конечная 3D-точка")
    return tuple(value)


def _close(a, b):
    return len(a) == len(b) and all(abs(x - y) <= TOLERANCE for x, y in zip(a, b))


def _rectangle(loop, elevation):
    if not isinstance(loop, list) or len(loop) != 4:
        raise ValueError("поддерживаются только прямоугольные контуры из четырёх отрезков")
    edges = []
    for curve in loop:
        if curve["kind"] != "Line":
            raise ValueError("дуги/аппроксимация контура требуют инженерной проверки")
        start, end = _point(curve["start_mm"]), _point(curve["end_mm"])
        if abs(start[2] - elevation) > TOLERANCE or abs(end[2] - elevation) > TOLERANCE:
            raise ValueError("контур не лежит на плоскости грани")
        length = curve["length_mm"]
        if isinstance(length, bool) or not math.isfinite(length) or abs(math.dist(start, end) - length) > TOLERANCE:
            raise ValueError("длина ребра не соответствует концам")
        edges.append((start, end))
    points = [p for edge in edges for p in edge]
    box = min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)
    if box[2] - box[0] <= TOLERANCE or box[3] - box[1] <= TOLERANCE:
        raise ValueError("вырожденный прямоугольник")
    corners = [(box[0], box[1], elevation), (box[2], box[1], elevation),
               (box[2], box[3], elevation), (box[0], box[3], elevation)]
    expected = list(zip(corners, (*corners[1:], corners[0])))
    for start, end in edges:
        matched = [i for i, (a, b) in enumerate(expected) if (_close(start, a) and _close(end, b))
                   or (_close(start, b) and _close(end, a))]
        if len(matched) != 1:
            raise ValueError("рёбра не образуют один осевой прямоугольник; bbox-подмена запрещена")
        expected.pop(matched[0])
    return box


def rectangular_host_from_reference(report: dict) -> RectangularHostEnvelope:
    """Только совпадающие горизонтальные прямоугольные верхняя/нижняя грани.

    Порядок loops не определяет проёмы: наружный контур выбирается геометрически.
    Внутренние пустоты/боковые грани в этом probe отсутствуют и остаются not_checked.
    """
    if (report["schema_version"] != "revit-reference-probe/v1" or report["units"] != "mm"
            or report["coordinate_system"] != "revit-internal-origin-and-axes"
            or report["status"] != "collected" or report["issues"] or report["read_only"] is not True
            or report["placement_eligible"] is not False):
        raise ValueError("нужен полный read-only Reference Probe в Revit internal mm")
    return rectangular_host_from_floor(report["floor"], source="Reference Probe floor "
        + str(report["floor"]["element_id"]) + "; matching faces only, Solid not verified")


def rectangular_host_from_floor(floor: dict, *, source: str) -> RectangularHostEnvelope:
    """Shared geometry parser; the caller must separately validate snapshot provenance."""
    lo, hi = _point(floor["bbox_mm"]["min_mm"]), _point(floor["bbox_mm"]["max_mm"])
    sides = []
    for side, sign, elevation in (("top", 1, hi[2]), ("bottom", -1, lo[2])):
        faces = floor[side + "_faces"]
        if len(faces) != 1 or not faces[0]["plane"]:
            raise ValueError("требуется одна плоская грань сверху и снизу")
        face = faces[0]
        normal = _point(face["plane"]["normal"])
        if (any(abs(a - b) > 1e-8 for a, b in zip(normal, (0, 0, sign)))
                or abs(_point(face["plane"]["origin_mm"])[2] - elevation) > TOLERANCE):
            raise ValueError("наклонные грани или высота, отличная от bbox, не поддержаны")
        loops = face["edge_loops"]
        if not 1 <= len(loops) <= 129:
            raise ValueError("неподдерживаемое количество контуров")
        boxes = [_rectangle(loop, elevation) for loop in loops]
        outer = max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        if not _close(outer, (lo[0], lo[1], hi[0], hi[1])):
            raise ValueError("наружная грань не совпадает с габаритом host")
        holes = sorted(b for b in boxes if b is not outer)
        if any(bboxes_overlap(a, b) for i, a in enumerate(holes) for b in holes[i + 1:]):
            raise ValueError("перекрывающиеся/вложенные контуры не поддержаны")
        sides.append((outer, tuple(holes)))
    if len(sides[0][1]) != len(sides[1][1]) or any(not _close(a, b) for a, b in zip(
            (sides[0][0], *sides[0][1]), (sides[1][0], *sides[1][1]))):
        raise ValueError("верх/низ имеют разные контуры: призматичность не подтверждена")
    covers = floor["covers"]
    return RectangularHostEnvelope(sides[0][0], sides[0][1], lo[2], hi[2],
                                   covers["top"]["distance_mm"], covers["bottom"]["distance_mm"],
                                   covers["other"]["distance_mm"],
                                   source)


def review_composite_host(mosaic, layout, reference, *, coordinate_policy: str) -> dict:
    if coordinate_policy != HOST_COORDINATE_POLICY:
        raise ValueError("нужно явно подтвердить исследовательскую XY-систему; автопривязки нет")
    demand, constraints, zones = build_review_zones(mosaic, layout)
    host = rectangular_host_from_reference(reference)
    result = evaluate_composite_host(demand, zones, host, constraints=constraints)
    result["demand_feasibility"] = evaluate_host_demand_feasibility(demand, host, constraints=constraints)
    result.update(coordinate_policy=coordinate_policy, live_coordinate_binding="not_checked")
    return result
