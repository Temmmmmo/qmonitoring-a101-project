"""Checked orthogonal solids, including holes, side recesses and intermediate ledges.

The horizontal-face sweep reconstructs constant XY sections between every pair
of vertex heights. ALL vertical faces and the volume must independently match
that reconstruction. No bbox substitute, curved-edge tessellation or mesh repair.
Coordinates are rounded to 0.00001 mm only to remove serialization noise.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math

from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

GRID_MM = 0.00001
LENGTH_TOLERANCE_MM = 0.01
AREA_TOLERANCE_MM2 = 0.001


@dataclass(frozen=True)
class SolidHostSection:
    bottom_z_mm: float
    top_z_mm: float
    footprint: Polygon | MultiPolygon


@dataclass(frozen=True)
class OrthogonalSolidHost:
    sections: tuple[SolidHostSection, ...]
    top_cover_mm: float
    bottom_cover_mm: float
    side_cover_mm: float
    volume_mm3: float
    face_count: int


def _number(value, maximum=1e9):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or abs(value) > maximum):
        raise ValueError("Solid требует конечные числовые координаты")
    return float(value)


def _point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("Solid требует 3D-точки")
    return tuple(round(_number(v), 5) for v in value)


def _polygons(geometry):
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    raise ValueError("Сечение должно состоять из невырожденных полигонов")


def _same(a, b):
    return a.symmetric_difference(b).area <= AREA_TOLERANCE_MM2


def _ring(edges, axis, elevation):
    if not isinstance(edges, list) or not 4 <= len(edges) <= 4096:
        raise ValueError("Неверное количество рёбер грани")
    adjacency = defaultdict(list)
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("kind") != "Line":
            raise ValueError("Криволинейное ребро не заменяется аппроксимацией")
        start, end = _point(edge["start_mm"]), _point(edge["end_mm"])
        if start[axis] != elevation or end[axis] != elevation:
            raise ValueError("Ребро вне плоскости грани")
        if abs(math.dist(start, end) - _number(edge["length_mm"])) > LENGTH_TOLERANCE_MM:
            raise ValueError("Длина ребра не соответствует концам")
        if sum(a != b for a, b in zip(start, end)) != 1:
            raise ValueError("Нужно невырожденное ребро вдоль одной оси")
        adjacency[start].append(end)
        adjacency[end].append(start)
    if len(adjacency) != len(edges) or any(len(v) != 2 or v[0] == v[1] for v in adjacency.values()):
        raise ValueError("Рёбра не образуют простой замкнутый контур")
    first = min(adjacency)
    points, previous, current = [first], None, first
    for _ in range(len(edges)):
        following = next(v for v in adjacency[current] if v != previous)
        if following == first:
            break
        if following in points:
            raise ValueError("Повторная вершина контура")
        points.append(following)
        previous, current = current, following
    if len(points) != len(edges):
        raise ValueError("Несколько циклов вместо одного контура")
    other = [i for i in range(3) if i != axis]
    polygon = Polygon([(p[other[0]], p[other[1]]) for p in points])
    if not polygon.is_valid or polygon.is_empty or polygon.area <= AREA_TOLERANCE_MM2:
        raise ValueError("Самопересечение или вырожденная грань")
    return polygon


def _face(face):
    if not isinstance(face, dict):
        raise ValueError("Грань Solid должна быть объектом")
    plane = face.get("plane")
    if not isinstance(plane, dict):
        raise ValueError("Поддерживаются только плоские грани")
    normal = plane["normal"]
    if not isinstance(normal, (list, tuple)) or len(normal) != 3:
        raise ValueError("Неверная нормаль грани")
    normal = tuple(_number(v) for v in normal)
    axes = [i for i in range(3) if abs(abs(normal[i]) - 1) < 1e-8
            and all(abs(normal[j]) < 1e-8 for j in range(3) if j != i)]
    if len(axes) != 1:
        raise ValueError("Наклонные грани Solid пока не поддерживаются")
    axis = axes[0]
    elevation = _point(plane["origin_mm"])[axis]
    loops = face["edge_loops"]
    if not isinstance(loops, list) or not 1 <= len(loops) <= 129:
        raise ValueError("Неверное количество контуров грани")
    rings = [_ring(loop, axis, elevation) for loop in loops]
    # Revit may represent several disconnected coplanar pieces as ONE face.
    # Loop order and winding are not used to guess which loops are holes.
    parents = {}
    for i, ring in enumerate(rings):
        containing = []
        for j, other in enumerate(rings):
            if i == j:
                continue
            if ring.boundary.intersects(other.boundary):
                raise ValueError("Пересекающиеся или дублирующиеся контуры грани")
            if other.contains(ring):
                containing.append(j)
        parents[i] = min(containing, key=lambda j: rings[j].area) if containing else None
    polygons = []
    for i, ring in enumerate(rings):
        depth, parent = 0, parents[i]
        while parent is not None:
            depth, parent = depth + 1, parents[parent]
        if depth % 2 == 0:
            holes = [p.exterior.coords for j, p in enumerate(rings) if parents[j] == i]
            polygons.append(Polygon(ring.exterior.coords, holes))
    polygon = unary_union(polygons)
    if not polygon.is_valid:
        raise ValueError("Некорректная геометрия контуров грани")
    return axis, 1 if normal[axis] > 0 else -1, elevation, polygon


def _union_faces(polygons):
    merged = unary_union(polygons)
    if abs(sum(p.area for p in polygons) - merged.area) > AREA_TOLERANCE_MM2:
        raise ValueError("Дублирующиеся или перекрывающиеся грани Solid")
    return merged


def orthogonal_host_from_solid(floor: dict, solid: dict) -> OrthogonalSolidHost:
    """Validate every face, not just the selected floor's top/bottom outlines."""
    if not isinstance(floor, dict) or not isinstance(solid, dict):
        raise ValueError("Снимки плиты и Solid должны быть объектами")
    faces = solid.get("faces")
    if not isinstance(faces, list) or not 6 <= len(faces) <= 4096:
        raise ValueError("Неверное количество граней Solid")
    groups = defaultdict(list)
    for face in faces:
        axis, sign, elevation, polygon = _face(face)
        groups[axis, sign, elevation].append(polygon)
    actual = {key: _union_faces(values) for key, values in groups.items()}
    # All side-face vertex levels must be events too; hidden ledges cannot vanish.
    levels = sorted({_point(edge[key])[2] for face in faces for loop in face["edge_loops"]
                     for edge in loop for key in ("start_mm", "end_mm")})
    if not 2 <= len(levels) <= 128:
        raise ValueError("Превышен лимит высотных сечений Solid")
    current = GeometryCollection()
    sections = []
    for i, z in enumerate(levels):
        added = actual.get((2, -1, z), GeometryCollection())
        removed = actual.get((2, 1, z), GeometryCollection())
        if current.intersection(added).area > AREA_TOLERANCE_MM2 or removed.difference(current).area > AREA_TOLERANCE_MM2:
            raise ValueError("Горизонтальные грани не замыкают ориентированный Solid")
        if added.intersection(removed).area > AREA_TOLERANCE_MM2:
            raise ValueError("Совпадающие противоположные горизонтальные грани")
        current = current.difference(removed).union(added)
        if i == len(levels) - 1:
            if current.area > AREA_TOLERANCE_MM2:
                raise ValueError("Solid не закрыт сверху")
        else:
            _polygons(current)
            if not current.is_valid or current.is_empty:
                raise ValueError("Пустое или некорректное сечение внутри Solid")
            sections.append(SolidHostSection(z, levels[i+1], current))

    expected = defaultdict(list)
    for section in sections:
        for polygon in _polygons(section.footprint):
            polygon = orient(polygon, sign=1)
            for ring in (polygon.exterior, *polygon.interiors):
                points = list(ring.coords)
                for a, b in zip(points, points[1:]):
                    if a[0] == b[0]:
                        key, interval = (0, 1 if b[1] > a[1] else -1, a[0]), sorted((a[1], b[1]))
                    elif a[1] == b[1]:
                        key, interval = (1, -1 if b[0] > a[0] else 1, a[1]), sorted((a[0], b[0]))
                    else:
                        raise ValueError("Непрямоугольное ребро восстановленного сечения")
                    expected[key].append(box(interval[0], section.bottom_z_mm, interval[1], section.top_z_mm))
    walls = {key: value for key, value in actual.items() if key[0] != 2}
    if walls.keys() != expected.keys() or any(not _same(walls[key], unary_union(values)) for key, values in expected.items()):
        raise ValueError("Боковые грани/стенки проёмов не совпадают с восстановленным Solid")
    lo, hi = _point(floor["bbox_mm"]["min_mm"]), _point(floor["bbox_mm"]["max_mm"])
    footprint = unary_union([s.footprint for s in sections])
    if (levels[0], levels[-1]) != (lo[2], hi[2]) or any(abs(a-b) > LENGTH_TOLERANCE_MM for a, b in
            zip(footprint.bounds, (lo[0], lo[1], hi[0], hi[1]))):
        raise ValueError("Габарит Solid не совпадает со снимком плиты")
    for side, sign, z in (("bottom", -1, levels[0]), ("top", 1, levels[-1])):
        selected = [_face(face) for face in floor[side + "_faces"]]
        if not selected or any(value[:3] != (2, sign, z) for value in selected):
            raise ValueError("Снимок верхней/нижней грани не совпадает с Solid")
        if not _same(_union_faces([v[3] for v in selected]), actual[2, sign, z]):
            raise ValueError("Контуры снимка плиты не совпадают с Solid")
    volume = math.fsum(s.footprint.area * (s.top_z_mm-s.bottom_z_mm) for s in sections)
    if not math.isclose(_number(solid["volume_mm3"], 1e24), volume, rel_tol=1e-8, abs_tol=1):
        raise ValueError("Объём Solid не совпадает с восстановленными сечениями")
    covers = [_number(floor["covers"][key]["distance_mm"]) for key in ("top", "bottom", "other")]
    if min(covers) < 0 or covers[0] + covers[1] >= hi[2]-lo[2]:
        raise ValueError("Защитные слои не совместимы с толщиной плиты")
    return OrthogonalSolidHost(tuple(sections), *covers, volume, len(faces))


def outside_box_volume_mm3(host: OrthogonalSolidHost, lo, hi) -> float:
    """Exact volume outside the reconstructed solid for an axis-aligned envelope.

    Callers include cover in the box, matching the conservative native Revit
    trial. A rectangular envelope overestimates a round bar at its corners.
    """
    if len(lo) != 3 or len(hi) != 3:
        raise ValueError("Нужны два 3D-угла envelope")
    lo, hi = tuple(_number(v) for v in lo), tuple(_number(v) for v in hi)
    if any(a >= b for a, b in zip(lo, hi)):
        raise ValueError("Envelope должен иметь положительные размеры")
    footprint = box(lo[0], lo[1], hi[0], hi[1])
    inside = math.fsum(footprint.intersection(s.footprint).area
                      * max(0, min(hi[2], s.top_z_mm) - max(lo[2], s.bottom_z_mm)) for s in host.sections)
    return max(0.0, footprint.area * (hi[2]-lo[2]) - inside)
