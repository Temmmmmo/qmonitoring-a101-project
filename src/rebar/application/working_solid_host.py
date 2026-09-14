"""Offline host preflight for the real slab; never changes or approves a layout."""
from __future__ import annotations

from functools import reduce
import math

from shapely.geometry import box, mapping
from shapely.ops import unary_union

from rebar.optimization.services.solid_host import (
    AREA_TOLERANCE_MM2, GRID_MM, LENGTH_TOLERANCE_MM, orthogonal_host_from_solid, outside_box_volume_mm3,
)

from .working_host import validate_working_snapshot

DIRECTIONS = ("bottom-X", "bottom-Y", "top-X", "top-Y")
WORKING_PROBE_SCHEMA = "revit-working-host-cad-probe/v1"
MAX_WORKING_REPORT_BYTES = 32 * 1024 * 1024  # host + two complete CAD readings


def _working_probe_floor(report):
    """Host-only read evidence; CAD diagnostics are retained, never called a binding."""
    document = report.get("document", {})
    if (report.get("read_only") is not True or report.get("placement_eligible") is not False
            or report.get("engineering_approval") is not False or report.get("units") != "mm"
            or report.get("coordinate_system") != "revit-internal-origin-and-axes"
            or report.get("status") not in ("collected", "partial") or report.get("host_read_issues") != []
            or not isinstance(report.get("read_issues"), list)
            or type(document.get("is_modified_before")) is not bool
            or document.get("is_modified_after") is not document["is_modified_before"]):
        raise ValueError("Нужен неизменённый read-only снимок рабочей плиты без ошибок чтения host")
    floor = report.get("host")
    if (not isinstance(floor, dict) or type(floor.get("element_id")) is not int or floor["element_id"] <= 0
            or type(report.get("host_id")) is not int or report["host_id"] != floor["element_id"]):
        raise ValueError("Идентификаторы рабочей плиты и снимка не совпадают")
    return floor


def inspect_working_solid(report: dict):
    floor = (_working_probe_floor(report) if report.get("schema_version") == WORKING_PROBE_SCHEMA
             else validate_working_snapshot(report))
    host = orthogonal_host_from_solid(floor, report["host_solid"])
    return host, {
        "schema_version": "qmonitoring-working-solid-check/v1", "units": "mm",
        "placement_eligible": False, "engineering_approval": False,
        "status": "geometry_checked_placement_not_checked", "host_id": floor["element_id"],
        "document": report.get("document"), "host_unique_id": floor.get("unique_id"),
        "source_snapshot_status": report["status"],
        "source_read_issues": report["read_issues"],
        "geometry": {"policy": "all-faces-orthogonal-section-reconstruction/v1",
            "face_count": host.face_count, "coordinate_rounding_grid_mm": GRID_MM,
            "tolerances": {"edge_length_mm": LENGTH_TOLERANCE_MM, "face_difference_area_mm2": AREA_TOLERANCE_MM2,
                           "solid_volume_abs_mm3": 1, "solid_volume_relative": 1e-8},
            "volume_mm3": host.volume_mm3,
            "covers_mm": {"top": host.top_cover_mm, "bottom": host.bottom_cover_mm, "side": host.side_cover_mm},
            "sections": [{"bottom_z_mm": s.bottom_z_mm, "top_z_mm": s.top_z_mm,
                "area_mm2": s.footprint.area, "footprint": mapping(s.footprint),
                "opening_count": sum(len(p.interiors) for p in
                    ([s.footprint] if s.footprint.geom_type == "Polygon" else s.footprint.geoms))}
                for s in host.sections]},
        "not_checked": ["live-RVT-state", "DXF-to-host-binding", "bar-placement",
                        "background-and-layer-order", "engineering-acceptance", "permanent-placement"],
    }


def _finite(value):
    if (isinstance(value, bool) or not isinstance(value, (float, int))
            or not math.isfinite(value) or abs(value) > 1e8):
        raise ValueError("Нужно явное конечное числовое значение")
    return value


def review_working_solid_bars(report: dict, packet: dict, *, offset_x_mm: float,
                             offset_y_mm: float, binding_source: str,
                             axis_depths_mm: dict | None = None) -> dict:
    """Check geometry of ALL runs in a physical packet with explicit assumptions.

    Without depths, the XY union is a necessary condition, NOT proof of 3D fit.
    With depths, square bar bodies + cover are checked against every Z section.
    The CLI separately validates the full packet's source certificates using the
    independent pyRevit validator. This function does not certify those sources.
    """
    host, result = inspect_working_solid(report)
    dx, dy = _finite(offset_x_mm), _finite(offset_y_mm)
    if not isinstance(binding_source, str) or not binding_source.strip() or len(binding_source) > 2048:
        raise ValueError("Укажи источник привязки или явно назови её исследовательской гипотезой")
    if (packet.get("schema_version") != "physical-bar-plan-trial/v1" or packet.get("units") != "mm"
            or packet.get("placement_eligible") is not False):
        raise ValueError("Нужен физический диагностический пакет, не разрешение на размещение")
    directions = packet["directions"]
    if len(directions) != 4 or sorted(d["direction"] for d in directions) != sorted(DIRECTIONS):
        raise ValueError("Нужны все четыре направления без повторов")
    if axis_depths_mm is not None:
        if not isinstance(axis_depths_mm, dict) or set(axis_depths_mm) != set(DIRECTIONS):
            raise ValueError("Нужны четыре явные глубины осей от соответствующих граней")
        if any(not 1 <= _finite(value) <= 10000 for value in axis_depths_mm.values()):
            raise ValueError("Глубина оси должна быть в диапазоне 1..10000 мм")
    union = unary_union([s.footprint for s in host.sections])
    common = reduce(lambda a, b: a.intersection(b), (s.footprint for s in host.sections))
    rows, seen = [], set()
    for direction in directions:
        name = direction["direction"]
        axis = 0 if name.endswith("X") else 1
        for run in direction["runs"]:
            count, diameter, step = run["bar_count"], _finite(run["diameter_mm"]), _finite(run["spacing_mm"])
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 5000:
                raise ValueError("Неверное количество физических стержней")
            if not 6 <= diameter <= 40 or step < 0 or (count > 1 and step <= 0):
                raise ValueError("Неверные диаметр или шаг")
            a, b = run["start_xy_mm"], run["end_xy_mm"]
            if len(a) != 2 or len(b) != 2:
                raise ValueError("Нужны две XY-точки стержня")
            a, b = [_finite(v) for v in a], [_finite(v) for v in b]
            if abs(a[1-axis] - b[1-axis]) > 1e-6 or b[axis] <= a[axis]:
                raise ValueError("Стержень не направлен вдоль заявленной оси")
            for index in range(count):
                identity = (name, run["id"], index)
                if identity in seen or len(rows) >= 5000:
                    raise ValueError("Дублированный стержень или превышен лимит; усечение запрещено")
                seen.add(identity)
                lower = [a[0]+dx, a[1]+dy]
                upper = [b[0]+dx, b[1]+dy]
                for point in (lower, upper):
                    point[1-axis] += index * step
                radius = diameter / 2
                for i in range(2):
                    margin = host.side_cover_mm + (radius if i != axis else 0)
                    lower[i] -= margin
                    upper[i] += margin
                shape = box(*lower, *upper)
                projection_outside = shape.difference(union).area
                common_outside = shape.difference(common).area
                row = {"direction": name, "run_id": run["id"], "position_index": index,
                    "envelope_xy_mm": [*lower, *upper], "outside_projection_area_mm2": projection_outside,
                    "outside_full_height_common_area_mm2": common_outside,
                    "outside_even_in_xy_projection": projection_outside > AREA_TOLERANCE_MM2,
                    "outside_full_height_common_footprint": common_outside > AREA_TOLERANCE_MM2}
                if axis_depths_mm is not None:
                    z = (host.sections[0].bottom_z_mm + axis_depths_mm[name] if name.startswith("bottom")
                         else host.sections[-1].top_z_mm - axis_depths_mm[name])
                    lower.append(z - radius - host.bottom_cover_mm)
                    upper.append(z + radius + host.top_cover_mm)
                    outside = outside_box_volume_mm3(host, lower, upper)
                    row.update(axis_z_mm=z, envelope_mm={"min_mm": lower, "max_mm": upper},
                               outside_volume_mm3=outside, outside_solid_with_cover=outside > 0.1)
                rows.append(row)
    if len(rows) != packet["expected"]["physical_bar_count"] or not rows:
        raise ValueError("Количество стержней не совпадает с полным пакетом")
    checks = {}
    for name in DIRECTIONS:
        group = [r for r in rows if r["direction"] == name]
        checks[name] = {"physical_bar_count": len(group),
            "outside_even_in_xy_projection": sum(r["outside_even_in_xy_projection"] for r in group),
            "outside_full_height_common_footprint": sum(r["outside_full_height_common_footprint"] for r in group),
            "outside_solid_with_cover": (sum(r["outside_solid_with_cover"] for r in group)
                                         if axis_depths_mm is not None else None)}
    total = {key: (sum(v[key] for v in checks.values()) if checks[DIRECTIONS[0]][key] is not None else None)
             for key in checks[DIRECTIONS[0]]}
    blocked = total["outside_even_in_xy_projection"] or total["outside_solid_with_cover"]
    result.update(status="blocked_host_under_explicit_binding" if blocked else
                  "geometry_contained_under_explicit_profile" if axis_depths_mm is not None else "axis_depths_required",
        binding={"source_to_revit_xy_mm": [dx, dy], "source": binding_source,
                 "live_binding_verified": False, "axis_depths_mm": axis_depths_mm},
        bar_check={"totals": total, "directions": checks, "bars": rows,
                   "policy": "square-body-with-cover/v1", "packet_source_certificates_checked": False,
                   "outside_area_tolerance_mm2": AREA_TOLERANCE_MM2, "outside_volume_tolerance_mm3": 0.1},
        source_packet_expected=packet["expected"], source_blockers=packet.get("source_blockers", []))
    result["not_checked"] = ["live-RVT-state", "measured-DXF-to-host-binding", "background-and-layer-order",
                             "source-demand-and-anchorage", "engineering-acceptance", "permanent-placement"]
    if axis_depths_mm is None:
        result["not_checked"].append("actual-3d-bar-placement")
    return result
