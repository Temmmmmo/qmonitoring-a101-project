"""Предпроверка фактических стержней после 40d/раскроя. Ничего не подрезает.

Доказательство ограничено явно переданной прямоугольной призмой с прямоугольными
сквозными проёмами. Не подтверждает полноту Revit Solid и не читает существующий фон.
"""
from __future__ import annotations

from itertools import combinations
import math

from rebar.models import Axis, Layer

from ..contracts.host import RectangularHostEnvelope
from ..contracts.placement import CompositeLayoutZone
from ..contracts.problem import DemandMap, LayoutConstraints
from .axis_patterns import pattern_coordinates
from .bar_geometry import axis_envelope_to_body_bbox
from .composite_detailing import evaluate_composite_zone
from .composite_coverage import validate_composite_demand
from .geometry import bboxes_overlap, polygon_area, polygon_bbox_intersection_area

TOLERANCE_MM = 0.01
MAX_BARS = 10000
MAX_PAIR_CHECKS = 2000000


def evaluate_composite_host(
    demand: DemandMap, zones: tuple[CompositeLayoutZone, ...], host: RectangularHostEnvelope,
    *, constraints: LayoutConstraints | None = None,
) -> dict:
    """Проверить полные оси, каждую добавку, cover и наложения данного направления.

    Неизвестная высота не заменяется ближайшим слоем: XY проверяется, Z остаётся
    not_checked. Ошибки возвращают ID зоны/компонента/оси, а не исправленную геометрию.
    """
    constraints = constraints or LayoutConstraints()
    if not math.isfinite(constraints.minimum_clear_spacing_mm):
        raise ValueError("зазор должен быть конечным")
    if len(zones) > 512 or len({zone.id for zone in zones}) != len(zones):
        raise ValueError("превышен лимит зон или повторяются идентификаторы")
    bars, invalid, unknown_depth = [], [], []
    for zone in zones:
        check = evaluate_composite_zone(demand, zone, constraints=constraints)
        if not check.geometry_valid:
            raise ValueError("невалидная детализация " + zone.id + ": " + "; ".join(check.diagnostics))
        for component in zone.components:
            if len(bars) + component.bar_count > MAX_BARS:
                raise ValueError("host preflight превышает лимит 10000 стержней; проверка не обрезана")
            coordinates = pattern_coordinates(component.placement, component.axis_window_mm)
            start, end = component.longitudinal_interval_mm
            radius = component.rebar.diameter / 2
            depth = component.placement.axis_depth_from_face_mm
            z = None if depth is None else (host.top_z_mm - depth if zone.direction.layer is Layer.TOP
                                            else host.bottom_z_mm + depth)
            if z is None:
                unknown_depth.append({"zone_id": zone.id, "component_index": component.component_index})
            for index, coordinate in enumerate(coordinates):
                axis_box = (start, coordinate, end, coordinate) if zone.direction.axis is Axis.X else (coordinate, start, coordinate, end)
                body = axis_envelope_to_body_bbox(zone.direction.axis, axis_box, component.rebar.diameter)
                row = {"zone_id": zone.id, "component_index": component.component_index, "axis_index": index,
                       "coordinate_mm": coordinate, "body_bbox_mm": body, "z_mm": z, "radius_mm": radius}
                errors = []
                outer, cover = host.outer_mm, host.side_cover_mm
                margins = (body[0] - outer[0], body[1] - outer[1], outer[2] - body[2], outer[3] - body[3])
                if min(margins) < cover - TOLERANCE_MM:
                    errors.append("host-side-cover")
                for hole in host.openings_mm:
                    reserved = (hole[0] - cover, hole[1] - cover, hole[2] + cover, hole[3] + cover)
                    if bboxes_overlap(body, reserved):
                        errors.append("opening-cover")
                        break
                if z is not None and (z - radius < host.bottom_z_mm + host.bottom_cover_mm - TOLERANCE_MM
                                      or z + radius > host.top_z_mm - host.top_cover_mm + TOLERANCE_MM):
                    errors.append("host-top-bottom-cover")
                if errors:
                    invalid.append({**row, "check_ids": errors})
                bars.append(row)
    planar_errors = sum(any(key != "host-top-bottom-cover" for key in row["check_ids"]) for row in invalid)
    vertical_errors = sum("host-top-bottom-cover" in row["check_ids"] for row in invalid)
    overlaps = [{"first_zone_id": a.id, "second_zone_id": b.id} for a, b in combinations(zones, 2)
                if bboxes_overlap(a.demand_bbox, b.demand_bbox)]
    # This checker intentionally accepts one direction. Distances between parallel
    # axes are exact transversely/in Z; an endpoint gap uses a conservative capsule.
    conflicts, pair_count = [], len(bars) * (len(bars) - 1) // 2
    projected_count, projected_examples = 0, []
    if pair_count > MAX_PAIR_CHECKS:
        collision_status = "not_checked"
    else:
        for a, b in combinations(bars, 2):
            if (a["z_mm"] is None or b["z_mm"] is None) and a["zone_id"] == b["zone_id"]:
                continue
            axis = demand.direction.axis
            along = (0, 2) if axis is Axis.X else (1, 3)
            gap = max(a["body_bbox_mm"][along[0]] - b["body_bbox_mm"][along[1]],
                      b["body_bbox_mm"][along[0]] - a["body_bbox_mm"][along[1]], 0)
            if a["z_mm"] is None or b["z_mm"] is None:
                # Disjoint demand rectangles can still have collinear 40d tails or
                # duplicate boundary axes. Unknown Z is NOT a collision-free layout.
                projected_distance = math.hypot(gap, a["coordinate_mm"] - b["coordinate_mm"])
                if a["zone_id"] != b["zone_id"] and projected_distance < (
                        a["radius_mm"] + b["radius_mm"] + constraints.minimum_clear_spacing_mm - TOLERANCE_MM):
                    projected_count += 1
                    if len(projected_examples) < 30:
                        projected_examples.append({"first_zone_id": a["zone_id"], "second_zone_id": b["zone_id"],
                            "first_component_index": a["component_index"], "second_component_index": b["component_index"],
                            "projected_axis_distance_mm": projected_distance,
                            "longitudinal_overlap_mm": max(0, min(a["body_bbox_mm"][along[1]], b["body_bbox_mm"][along[1]])
                                - max(a["body_bbox_mm"][along[0]], b["body_bbox_mm"][along[0]]))})
                continue
            distance = math.sqrt(gap ** 2 + (a["coordinate_mm"] - b["coordinate_mm"]) ** 2 + (a["z_mm"] - b["z_mm"]) ** 2)
            if distance < a["radius_mm"] + b["radius_mm"] + constraints.minimum_clear_spacing_mm - TOLERANCE_MM:
                conflicts.append({"first": {k: a[k] for k in ("zone_id", "component_index", "axis_index")},
                                  "second": {k: b[k] for k in ("zone_id", "component_index", "axis_index")},
                                  "axis_distance_mm": distance})
        collision_status = "fail" if conflicts else "not_checked" if unknown_depth else "pass"
    checks = {"planar_host_and_openings": "fail" if planar_errors else "pass",
              "top_bottom_cover": "fail" if vertical_errors else "not_checked" if unknown_depth else "pass",
              "same_direction_zone_overlap": "fail" if overlaps else "pass",
              "additional_bar_collisions": collision_status,
              "background_and_other_directions": "not_checked", "live_host_geometry": "not_checked"}
    return {"schema_version": "composite-host-preflight/v1", "units": "mm", "placement_eligible": False,
            "status": "blocked" if "fail" in checks.values() else "needs_external_checks",
            "scope": "explicit_rectangular_prism_and_rectangular_through_openings_one_direction",
            "host_source": host.source, "checks": checks, "physical_bar_count": len(bars),
            "invalid_bar_count": len(invalid), "invalid_bars": invalid, "unknown_depth_components": unknown_depth,
            "same_direction_overlaps": overlaps, "additional_bar_conflicts": conflicts,
            "unknown_depth_projected_interzone_conflicts": {
                "status": "not_checked" if pair_count > MAX_PAIR_CHECKS else "requires_joint_detailing" if projected_count else "none_detected",
                "pair_count": None if pair_count > MAX_PAIR_CHECKS else projected_count,
                "examples": projected_examples, "examples_truncated": projected_count > len(projected_examples),
                "warning": "Пересечения в XY при неизвестной высоте; не разрешённые нахлёсты и не доказанные 3D-коллизии."},
            "collision_pair_count": pair_count, "collision_pair_limit": MAX_PAIR_CHECKS,
            "boundary_policy": "reject_or_engineering_review_never_clip",
            "warning": "Проверка заданного envelope не доказывает полноту Revit Solid; "
                       "фон, другие направления и актуальная модель не проверены."}


def evaluate_host_demand_feasibility(
    demand: DemandMap, host: RectangularHostEnvelope, *, constraints: LayoutConstraints | None = None,
) -> dict:
    """Необходимое условие, независимое от пула и фаз: есть ли место для прямых 40d.

    В research-профиле покрытия анкеровка не покрывает спрос. Следовательно,
    demand_bbox начинается не ближе cover+40d от продольной грани. Все обязательные
    добавки одной зоны должны поместиться; берётся максимальный их диаметр.
    Раскрой может лишь удлинить стержни, поэтому это нижняя граница ограничения.
    Не доказывает общую допустимость при отсутствии найденного противоречия.
    """
    validate_composite_demand(demand)
    constraints = constraints or LayoutConstraints()
    if not math.isfinite(constraints.anchorage_diameters):
        raise ValueError("анкеровка должна быть конечной")
    axis = demand.direction.axis
    along = (0, 2) if axis is Axis.X else (1, 3)
    impossible = []
    for cell in demand.cells:
        recipe = demand.level(cell.level_index).recipe
        if not recipe.additions:
            continue
        extension = max(spec.diameter for spec in recipe.additions) * constraints.anchorage_diameters
        lower = host.outer_mm[along[0]] + host.side_cover_mm + extension
        upper = host.outer_mm[along[1]] - host.side_cover_mm - extension
        # Transversely use the cell's exact bounds: this proof is longitudinal only.
        if axis is Axis.X:
            window = (lower, min(p[1] for p in cell.poly), upper, max(p[1] for p in cell.poly))
        else:
            window = (min(p[0] for p in cell.poly), lower, max(p[0] for p in cell.poly), upper)
        area = polygon_area(cell.poly)
        remaining = polygon_bbox_intersection_area(cell.poly, window) if lower < upper else 0
        missing = max(0.0, area - remaining)
        if missing > max(1e-6, area * 1e-9):
            impossible.append({"cell_id": cell.id, "level_index": cell.level_index,
                               "minimum_anchorage_each_end_mm": extension,
                               "admissible_longitudinal_demand_interval_mm": [lower, upper],
                               "unavoidable_uncovered_area_mm2": missing})
    return {"status": "incompatible_with_straight_anchorage_model" if impossible else "necessary_condition_passed",
            "scope": "longitudinal_boundary_only_current_ordered_recipe_and_demand_bbox_coverage_model",
            "placement_eligible": False, "cell_count": len(impossible), "cells": impossible,
            "unavoidable_uncovered_area_mm2": math.fsum(c["unavoidable_uncovered_area_mm2"] for c in impossible),
            "requires_engineering_decision": bool(impossible),
            "warning": "Это противоречие текущей модели прямых стержней и 40d, не доказательство "
                       "невозможности реального инженерного решения. Правило у края нельзя менять автоматически."}
