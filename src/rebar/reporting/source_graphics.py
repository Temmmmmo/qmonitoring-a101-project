"""Original FE and parametric zones for graphics, never a physical placement packet."""
from copy import deepcopy
import html
import math
from pathlib import Path
import re

from ezdxf.colors import aci2rgb

from rebar.reporting.serialization import to_jsonable

SOURCE_GRAPHICS_SCHEMA = "source-isofields-zones/v1"
SOURCE_STAGE = "original-parametric-zones-before-physical-normalization"


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


def build_source_graphics(problem, report, *, candidate_index=None, source_files=()):
    """Copy the selected PRE-normalization zones and unchanged original FE.

    Hashes are provenance metadata, not a new geometric or engineering check.
    Only basenames leave this presentation boundary; private local paths do not.
    """
    selected = report.get("selected_index") if candidate_index is None else candidate_index
    if report.get("case_id") != problem.case_id:
        raise ValueError("Original source report case differs from the loaded FE problem")
    if type(selected) is not int or not 0 <= selected < len(report.get("front", ())):
        raise ValueError("A selected original source candidate is required")
    rows = report.get("directions", ())
    indexes = report["front"][selected]["direction_candidate_indexes"]
    if len(rows) != 4 or len(problem.direction_problems) != 4 or len(indexes) != 4:
        raise ValueError("Four complete original source directions required")
    directions, files = [], []
    for original, row, index in zip(problem.direction_problems, rows, indexes):
        demand = original.demand
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
        "case_id": problem.case_id, "placement_eligible": False, "engineering_approval": False,
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
    for index, zone in enumerate(zones, 1):
        for component in zone["components"]:
            bx1, by1, bx2, by2 = component["bar_axis_bbox_mm"]
            note = html.escape(f'Z{index} / {component["component_index"]+1}; исходный набор после 40d/раскроя; '
                f'Ø{component["diameter_mm"]}; условный шаг {component["nominal_step_mm"]} мм; '
                f'L={component["installed_length_mm"]} мм; {component["bar_count"]} шт.; '
                'огибающая осей, не граница AreaReinforcement и не нормализованные стержни')
            envelopes.append(f'<rect data-component-index="{component["component_index"]}" '
                f'x="{bx1-xmin:.9f}" y="{ymax-by2:.9f}" width="{bx2-bx1:.9f}" height="{by2-by1:.9f}" '
                f'fill="none" stroke="#7b426f" stroke-dasharray="5 4" stroke-width="1" '
                f'vector-effect="non-scaling-stroke"><title>{note}</title></rect>')
        x1, y1, x2, y2 = zone["demand_bbox_mm"]
        specs = "; ".join(f'Ø{c["diameter_mm"]}, условный шаг {c["nominal_step_mm"]} мм, '
            f'L={c["installed_length_mm"]} мм, {c["bar_count"]} шт.' for c in zone["components"])
        title = html.escape(f'Z{index} · {zone["source_zone_id"]}; demand bbox {x2-x1} × {y2-y1} мм; {specs}')
        rectangles.append(f'<g data-zone-id="{html.escape(zone["source_zone_id"], quote=True)}">'
            f'<rect x="{x1-xmin:.9f}" y="{ymax-y2:.9f}" width="{x2-x1:.9f}" height="{y2-y1:.9f}" '
            f'fill="none" stroke="#173f61" stroke-width="1.8" vector-effect="non-scaling-stroke">'
            f'<title>{title}</title></rect><text x="{x1-xmin:.9f}" y="{ymax-y2:.9f}" '
            f'font-size="{max(width, height)*.009:.9f}" fill="#173f61" paint-order="stroke" '
            f'stroke="white" stroke-width="{max(width, height)*.0015:.9f}">Z{index}</text></g>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-padding} {-padding} '
        f'{width+2*padding} {height+2*padding}" role="img" aria-label="Исходные изополя и прямоугольные зоны">'
        f'<g class="cells">{"".join(cells)}</g><g class="source-component-envelopes">{"".join(envelopes)}</g>'
        f'<g class="source-zones">{"".join(rectangles)}</g></svg>')
