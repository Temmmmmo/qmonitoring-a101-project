"""Original FE and parametric zones for graphics, never a physical placement packet."""
from copy import deepcopy
import hashlib
import html
import json
import math
from pathlib import Path
import re

from ezdxf.colors import aci2rgb

from rebar.models import Rebar
from rebar.optimization.services.anchorage import FixedDiameterAnchoragePolicy
from rebar.reporting.serialization import to_jsonable

SOURCE_GRAPHICS_SCHEMA = "source-isofields-zones/v1"
SOURCE_STAGE = "original-parametric-zones-before-physical-normalization"
SOURCE_40D_TOLERANCE_MM = 1e-6


def _rgb(aci):
    return list(aci2rgb(aci)) if type(aci) is int and 1 <= aci <= 255 else [217, 228, 236]


def _basename(value):
    return Path(str(value).replace("\\", "/")).name


def _box(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in value)
            or value[0] > value[2] or value[1] > value[3]):
        raise ValueError("Source graphics require finite ordered original rectangles")
    return value


def source_zone_40d_certificate(source_graphics):
    """Check only published parametric envelopes, never host/body/3D coverage."""
    if (source_graphics.get("schema_version") != SOURCE_GRAPHICS_SCHEMA or source_graphics.get("source_stage") != SOURCE_STAGE
            or source_graphics.get("units") != "mm"):
        raise ValueError("Source 40d certificate requires the original source packet")
    directions, failures, count = source_graphics.get("directions", ()), [], 0
    keys = [(row.get("direction", {}).get("layer"), row.get("direction", {}).get("axis")) for row in directions]
    if len(directions) != 4 or len(set(keys)) != 4 or set(keys) != {("bottom", "X"), ("bottom", "Y"), ("top", "X"), ("top", "Y")}:
        raise ValueError("Source 40d certificate requires four unique plate directions")
    policy = FixedDiameterAnchoragePolicy()
    packet = json.dumps(source_graphics, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    base = {"schema_version": "source-zone-40d-certificate/v1", "source_stage": SOURCE_STAGE,
            "source_packet_sha256": hashlib.sha256(packet).hexdigest(), "tolerance_mm": SOURCE_40D_TOLERANCE_MM,
            "policy_id": policy.name, "factor_d": policy.multiplier}
    for row in directions:
        direction = row.get("direction", {})
        axis = direction.get("axis")
        if axis not in ("X", "Y"):
            raise ValueError("Source 40d certificate requires X/Y direction")
        for zone in row.get("zone_drafts", ()):
            demand = _box(zone.get("demand_bbox_mm"))
            if not zone.get("components"):
                return {**base, "status": "not_checked", "reason": "zone_without_components", "component_count": count,
                        "violation_count": len(failures), "logical_demand_coverage": "not_checked", "native_host_boundary": "not_checked"}
            for component in zone.get("components", ()):
                bounds = _box(component.get("bar_axis_bbox_mm"))
                diameter = component.get("diameter_mm")
                length = component.get("installed_length_mm")
                if (isinstance(diameter, bool) or not isinstance(diameter, (int, float)) or not math.isfinite(diameter)
                        or diameter <= 0 or isinstance(length, bool) or not isinstance(length, (int, float))
                        or not math.isfinite(length) or length <= 0):
                    raise ValueError("Source 40d certificate requires finite component geometry")
                lo, hi, dlo, dhi = (bounds[0], bounds[2], demand[0], demand[2]) if axis == "X" else (bounds[1], bounds[3], demand[1], demand[3])
                target, left, right = policy.extension_each_end_mm(Rebar(1, diameter)), dlo - lo, hi - dhi
                count += 1
                if abs((hi - lo) - length) > SOURCE_40D_TOLERANCE_MM or left + SOURCE_40D_TOLERANCE_MM < target or right + SOURCE_40D_TOLERANCE_MM < target:
                    failures.append({"direction": direction, "source_zone_id": zone.get("source_zone_id"),
                                     "component_index": component.get("component_index"), "diameter_mm": diameter,
                                     "target_each_end_mm": target, "left_extension_mm": left,
                                     "right_extension_mm": right, "installed_length_mm": length})
    if not count:
        return {**base, "status": "not_checked", "reason": "no_components", "component_count": 0, "violation_count": 0,
                "violations": [], "logical_demand_coverage": "not_checked", "native_host_boundary": "not_checked"}
    return {**base, "component_count": count, "violation_count": len(failures),
            "status": "pass" if not failures else "fail", "violations": failures,
            "logical_demand_coverage": "not_checked", "native_host_boundary": "not_checked",
            "note": "Проверены только осевые огибающие параметрических компонентов до физической обработки."}


def build_source_graphics(problem, report, *, candidate_index=None, source_files=()):
    return build_source_graphics_from_demands(
        tuple(item.demand for item in problem.direction_problems), report,
        case_id=problem.case_id, candidate_index=candidate_index, source_files=source_files)


def build_source_graphics_from_demands(demands, report, *, case_id, candidate_index=None, source_files=()):
    """Copy the selected PRE-normalization zones and unchanged original FE.

    Hashes are provenance metadata, not a new geometric or engineering check.
    Only basenames leave this presentation boundary; private local paths do not.
    """
    selected = report.get("selected_index") if candidate_index is None else candidate_index
    if report.get("case_id") != case_id:
        raise ValueError("Original source report case differs from the loaded FE problem")
    if type(selected) is not int or not 0 <= selected < len(report.get("front", ())):
        raise ValueError("A selected original source candidate is required")
    rows = report.get("directions", ())
    indexes = report["front"][selected]["direction_candidate_indexes"]
    if len(rows) != 4 or len(demands) != 4 or len(indexes) != 4:
        raise ValueError("Four complete original source directions required")
    directions, files = [], []
    for demand, row, index in zip(demands, rows, indexes):
        direction = to_jsonable(demand.direction)
        if row["direction"] != direction or type(index) is not int or not 0 <= index < len(row["candidates"]):
            raise ValueError("Original source candidate direction/index differs")
        candidate = row["candidates"][index]
        zones = deepcopy(candidate["zone_drafts"])
        if len(zones) != candidate["metrics"]["zone_count"]:
            raise ValueError("Source zone count differs from original parametric inventory")
        for zone in zones:
            if zone["direction"] != direction or zone["schema_version"] != "reinforcement-zone-revit/v2":
                raise ValueError("Original parametric zone schema/direction differs")
            _box(zone["demand_bbox_mm"])
        source = row.get("source", {})
        for role, digest in source.get("sha256", {}).items():
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                files.append({"role": role, "direction": direction,
                    "filename": _basename(source.get("filenames", {}).get(role) or role), "sha256": digest})
        directions.append({"direction": direction, "source_bbox_mm": list(_box(demand.bbox)),
            "cells": [{"cell_id": cell.id, "polygon_mm": to_jsonable(cell.poly),
                       "aci": cell.aci, "level_index": cell.level_index} for cell in demand.cells],
            "legend": [{"level_index": level.index, "aci": level.aci, "rgb": _rgb(level.aci),
                "label": level.label, "as_min_cm2_per_m": level.lower_as,
                "as_max_cm2_per_m": level.upper_as} for level in demand.levels],
            "zone_drafts": zones})
    if source_files:
        files = [{**{key: deepcopy(row[key]) for key in ("role", "sha256", "direction") if key in row},
                  "filename": _basename(row.get("filename") or row.get("path") or "source")}
                 for row in source_files]
    recorded = bool(files) and all(isinstance(row.get("sha256"), str)
        and re.fullmatch(r"[0-9a-fA-F]{64}", row["sha256"]) for row in files)
    return {"schema_version": SOURCE_GRAPHICS_SCHEMA, "units": "mm", "source_stage": SOURCE_STAGE,
        "case_id": case_id or "Своя плита", "placement_eligible": False, "engineering_approval": False,
        "source_files": files, "provenance_status": "sha256_recorded" if recorded else "unverified",
        "directions": directions,
        "label": "ИСХОДНЫЕ ИЗОПОЛЯ + ЗОНЫ / НЕ ФИЗИЧЕСКАЯ ВЕДОМОСТЬ / НЕ АРМАТУРА",
        "note": "Прямоугольник — исходный demand_bbox. После физической обработки число, длины и оси "
                "стержней могут отличаться. Контур плиты по bbox изополей не восстанавливается."}


def render_source_graphics_svg(direction):
    """Unclipped original polygons and zone rectangles, without physical bar axes."""
    zones = direction["zone_drafts"]
    boxes = [_box(direction["source_bbox_mm"]), *(_box(z["demand_bbox_mm"]) for z in zones)]
    boxes.extend(_box(component["bar_axis_bbox_mm"]) for zone in zones for component in zone["components"])
    # Include every FE vertex too: a stale bbox must not hide source geometry.
    for cell in direction["cells"]:
        xs, ys = zip(*cell["polygon_mm"])
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    xmin, ymin = min(b[0] for b in boxes), min(b[1] for b in boxes)
    xmax, ymax = max(b[2] for b in boxes), max(b[3] for b in boxes)
    width, height = max(1., xmax-xmin), max(1., ymax-ymin)
    padding = max(width, height)*.015
    cells, rectangles, envelopes = [], [], []
    for cell in direction["cells"]:
        points = " ".join(f"{x-xmin:.9f},{ymax-y:.9f}" for x, y in cell["polygon_mm"])
        color = "#"+"".join(f"{channel:02x}" for channel in _rgb(cell["aci"]))
        cells.append(f'<polygon data-cell-id="{html.escape(str(cell["cell_id"]), quote=True)}" '
            f'points="{points}" fill="{color}" fill-opacity=".55"><title>'
            f'КЭ {html.escape(str(cell["cell_id"]))}; уровень {cell["level_index"]}; '
            f'ACI {cell["aci"]}</title></polygon>')
    rectangles, envelopes = render_source_zone_layers(zones, xmin=xmin, ymax=ymax, span=max(width, height))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-padding} {-padding} '
        f'{width+2*padding} {height+2*padding}" role="img" aria-label="Исходные изополя и прямоугольные зоны">'
        f'<g class="cells">{"".join(cells)}</g><g class="source-component-envelopes">{envelopes}</g>'
        f'<g class="source-zones">{rectangles}</g></svg>')


def render_source_zone_layers(zones, *, xmin, ymax, span):
    """Draw source rectangles in the caller's world frame, never reconstruct from bars."""
    rectangles, envelopes = [], []
    for index, zone in enumerate(zones, 1):
        for component in zone["components"]:
            bx1, by1, bx2, by2 = _box(component["bar_axis_bbox_mm"])
            note = html.escape(f'Z{index} / {component["component_index"]+1}; исходный набор после 40d/раскроя; '
                f'Ø{component["diameter_mm"]}; условный шаг {component["nominal_step_mm"]} мм; '
                f'L={component["installed_length_mm"]} мм; {component["bar_count"]} шт.; '
                'огибающая осей, не граница AreaReinforcement и не нормализованные стержни')
            envelopes.append(f'<rect data-component-index="{component["component_index"]}" '
                f'x="{bx1-xmin:.9f}" y="{ymax-by2:.9f}" width="{bx2-bx1:.9f}" height="{by2-by1:.9f}" '
                f'fill="none" stroke="#7b426f" stroke-dasharray="5 4" stroke-width="1" '
                f'vector-effect="non-scaling-stroke"><title>{note}</title></rect>')
        x1, y1, x2, y2 = _box(zone["demand_bbox_mm"])
        specs = "; ".join(f'Ø{c["diameter_mm"]}, условный шаг {c["nominal_step_mm"]} мм, '
            f'L={c["installed_length_mm"]} мм, {c["bar_count"]} шт.' for c in zone["components"])
        title = html.escape(f'Z{index} · {zone["source_zone_id"]}; demand bbox {x2-x1} × {y2-y1} мм; {specs}')
        rectangles.append(f'<g data-zone-id="{html.escape(zone["source_zone_id"], quote=True)}">'
            f'<rect x="{x1-xmin:.9f}" y="{ymax-y2:.9f}" width="{x2-x1:.9f}" height="{y2-y1:.9f}" '
            f'fill="none" stroke="#173f61" stroke-width="1.8" vector-effect="non-scaling-stroke">'
            f'<title>{title}</title></rect><text x="{x1-xmin:.9f}" y="{ymax-y2:.9f}" '
            f'font-size="{span*.009:.9f}" fill="#173f61" paint-order="stroke" '
            f'stroke="white" stroke-width="{span*.0015:.9f}">Z{index}</text></g>')
    return "".join(rectangles), "".join(envelopes)
