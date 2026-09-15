# -*- coding: utf-8 -*-
"""Lossless source FE/zone graphics; IronPython 2.7, no structural placement."""
from __future__ import division, unicode_literals

import copy
import datetime
import json

from qm_revit_probe import element_id, text_type
from qm_trial_input import number

SCHEMA = "source-isofields-zones/v1"
STAGE = "original-parametric-zones-before-physical-normalization"
LABEL = "ИСХОДНЫЕ ИЗОПОЛЯ + ЗОНЫ / НЕ ФИЗИЧЕСКАЯ ВЕДОМОСТЬ / НЕ АРМАТУРА"
DIRECTIONS = ("bottom-X", "bottom-Y", "top-X", "top-Y")
MAX_CELLS, MAX_ZONES, MAX_VERTICES = 12000, 1000, 80000


def _text(value, maximum=4000):
    if not isinstance(value, text_type) or not value or len(value) > maximum or "\x00" in value:
        raise ValueError("Expected bounded nonempty Unicode source label")
    return value


def _integer(value, minimum, maximum):
    number(value, minimum, maximum)
    if isinstance(value, bool) or int(value) != value:
        raise ValueError("Expected integer source count/index")
    return int(value)


def _finite_tree(value, depth=0, budget=None):
    # Also protect metadata not used for drawing; direct callers bypass JSON IO.
    if budget is None:
        budget = [800000]
    budget[0] -= 1
    if depth > 32 or budget[0] < 0:
        raise ValueError("Source metadata resource limit exceeded")
    if isinstance(value, dict):
        for key, item in value.items():
            _text(key, 4000)
            _finite_tree(item, depth+1, budget)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _finite_tree(item, depth+1, budget)
    elif isinstance(value, text_type):
        if len(value) > 20000 or "\x00" in value:
            raise ValueError("Oversized source metadata string")
    elif value is not None and not isinstance(value, bool):
        number(value, -1e100, 1e100)


def _xy(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("Expected XY millimetre pair")
    for item in value:
        number(item, -100000000, 100000000)
    return list(value)


def _bbox(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("Expected source rectangle")
    _xy(value[:2])
    _xy(value[2:])
    if not value[0] < value[2] or not value[1] < value[3]:
        raise ValueError("Source rectangle must have positive dimensions")
    return list(value)


def _cross(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def _polygon(value):
    if not isinstance(value, (list, tuple)) or not 3 <= len(value) <= 8:
        raise ValueError("Source FE must have 3..8 original vertices; no truncation")
    points = []
    for item in value:
        point = _xy(item)
        if point in points:
            raise ValueError("Duplicate source polygon vertex")
        points.append(point)
    # FE input is straight-edged. Reject self intersections, including touching
    # non-adjacent edges, instead of asking Revit to silently repair the polygon.
    size, area = len(points), 0
    for i in range(size):
        a, b = points[i], points[(i+1) % size]
        area += _cross(points[0], a, b)
        for j in range(i+1, size):
            if j == i+1 or (i == 0 and j == size-1):
                continue
            c, d = points[j], points[(j+1) % size]
            if (_cross(a, b, c)*_cross(a, b, d) <= 0
                    and _cross(c, d, a)*_cross(c, d, b) <= 0
                    and max(min(a[0], b[0]), min(c[0], d[0])) <= min(max(a[0], b[0]), max(c[0], d[0]))
                    and max(min(a[1], b[1]), min(c[1], d[1])) <= min(max(a[1], b[1]), max(c[1], d[1]))):
                raise ValueError("Self-intersecting source FE polygon")
    if abs(area) <= 0.001:
        raise ValueError("Degenerate source FE polygon")
    return points


def _direction(value):
    if not isinstance(value, dict) or set(value) != set(("layer", "axis")):
        raise ValueError("Expected original direction object")
    result = text_type(value["layer"])+"-"+text_type(value["axis"])
    if result not in DIRECTIONS:
        raise ValueError("Unknown source direction")
    return result


def build_source_primitives(packet, offset_x_mm, offset_y_mm):
    """Validate graphics, not engineering; preserve every original FE and zone."""
    _finite_tree(packet)
    if (packet.get("schema_version") != SCHEMA or packet.get("units") != "mm"
            or packet.get("source_stage") != STAGE or packet.get("placement_eligible") is not False
            or packet.get("engineering_approval") is not False):
        raise ValueError("Expected unapproved original source graphics packet")
    _text(packet.get("case_id"))
    _xy([offset_x_mm, offset_y_mm])
    rows = packet.get("directions")
    if not isinstance(rows, list) or len(rows) != 4:
        raise ValueError("All four original source directions are required")
    seen, cell_count, zone_count, vertex_count = set(), 0, 0, 0
    for row in rows:
        key = _direction(row.get("direction"))
        if key in seen:
            raise ValueError("Duplicate source direction")
        seen.add(key)
        _bbox(row.get("source_bbox_mm"))  # This is NOT the floor outline.
        legend = row.get("legend")
        if not isinstance(legend, list) or not 1 <= len(legend) <= 256:
            raise ValueError("Missing/oversized original legend")
        levels = {}
        for band in legend:
            index = _integer(band.get("level_index"), 0, 10000)
            if index in levels:
                raise ValueError("Duplicate source legend level")
            rgb = band.get("rgb")
            if not isinstance(rgb, list) or len(rgb) != 3:
                raise ValueError("Explicit RGB legend is required; no ACI guessing")
            for channel in rgb:
                _integer(channel, 0, 255)
            _text(band.get("label"))
            levels[index] = band
        cells, zones = row.get("cells"), row.get("zone_drafts")
        if not isinstance(cells, list) or not isinstance(zones, list):
            raise ValueError("Expected complete source cells and zones arrays")
        cell_count += len(cells)
        zone_count += len(zones)
        if cell_count > MAX_CELLS or zone_count > MAX_ZONES:
            raise ValueError("Source drawing resource limit exceeded; no partial view")
        cell_ids, zone_ids = set(), set()
        for cell in cells:
            identifier = _text(text_type(cell.get("cell_id")))
            if cell.get("cell_id") is None or identifier in cell_ids:
                raise ValueError("Missing/duplicate original FE ID")
            cell_ids.add(identifier)
            points = _polygon(cell.get("polygon_mm"))
            vertex_count += len(points)
            if vertex_count > MAX_VERTICES:
                raise ValueError("Source vertex budget exceeded; no partial drawing")
            level = _integer(cell.get("level_index"), 0, 10000)
            if level not in levels or cell.get("aci") != levels[level].get("aci"):
                raise ValueError("Source FE level/ACI differs from its explicit legend")
        for zone in zones:
            identifier = _text(zone.get("source_zone_id"))
            if identifier in zone_ids or _direction(zone.get("direction")) != key:
                raise ValueError("Duplicate zone ID or mismatched source direction")
            zone_ids.add(identifier)
            if zone.get("schema_version") != "reinforcement-zone-revit/v2" or zone.get("units") != "mm":
                raise ValueError("Expected original parametric zone draft v2")
            if _integer(zone.get("level_index"), 0, 10000) not in levels:
                raise ValueError("Source zone level absent from its original legend")
            _bbox(zone.get("demand_bbox_mm"))
            parts = zone.get("components")
            if not isinstance(parts, list) or not 1 <= len(parts) <= 16:
                raise ValueError("Missing/oversized original zone components")
            part_ids = set()
            for part in parts:
                index = _integer(part.get("component_index"), 0, 1000)
                if index in part_ids:
                    raise ValueError("Duplicate source component index")
                part_ids.add(index)
                number(part.get("diameter_mm"), 1, 100)
                number(part.get("nominal_step_mm"), 1, 10000)
                number(part.get("installed_length_mm"), 0.001, 100000000)
                count = _integer(part.get("bar_count"), 1, 20000)
                axes = part.get("axis_coordinates_mm")
                if not isinstance(axes, list) or len(axes) != count:
                    raise ValueError("Source component count differs from original axes")
                prior = None
                for coordinate in axes:
                    number(coordinate, -100000000, 100000000)
                    if prior is not None and coordinate <= prior:
                        raise ValueError("Original source axes must increase strictly")
                    prior = coordinate
                window = _xy(part.get("axis_window_mm"))
                if window[0] > axes[0] or window[1] < axes[-1]:
                    raise ValueError("Source axes outside declared window")
                bounds = part.get("bar_axis_bbox_mm")
                if not isinstance(bounds, list) or len(bounds) != 4:
                    raise ValueError("Original component axis envelope is required")
                _xy(bounds[:2])
                _xy(bounds[2:])
                along = 0 if key.endswith("X") else 1
                if (abs(bounds[along+2]-bounds[along]-part["installed_length_mm"]) > 0.001
                        or abs(bounds[1-along]-axes[0]) > 0.001
                        or abs(bounds[3-along]-axes[-1]) > 0.001):
                    raise ValueError("Source component envelope differs from its original axes/length")
    provenance = packet.get("provenance_status")
    files = packet.get("source_files")
    if provenance not in ("sha256_recorded", "unverified") or not isinstance(files, list) or len(files) > 64:
        raise ValueError("Explicit source provenance status required")
    if provenance == "sha256_recorded" and not files:
        raise ValueError("SHA provenance has no source records")
    for record in files:
        digest = record.get("sha256")
        if digest is None and provenance == "unverified":
            continue
        if (not isinstance(digest, text_type) or len(digest) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in digest)):
            raise ValueError("Invalid recorded source SHA256")
    return {"units": "mm", "coordinate_system": "revit-internal-origin-and-axes",
        "placement_eligible": False, "engineering_approval": False, "input_schema": SCHEMA,
        "case_id": packet["case_id"], "offset_xy_mm": [offset_x_mm, offset_y_mm],
        "source_packet": copy.deepcopy(packet), "source_blockers": [],
        "summary": {"source_cell_count": cell_count, "source_zone_count": zone_count,
                    "direction_count": 4}, "bars": [], "source_frames": []}


def source_graphic_types(document, DB):
    """Choose existing types only. No new shared styles, parameters or patterns."""
    regions = list(DB.FilteredElementCollector(document).OfClass(DB.FilledRegionType))
    patterns = []
    for element in DB.FilteredElementCollector(document).OfClass(DB.FillPatternElement):
        pattern = element.GetFillPattern()
        try:
            if pattern.IsSolidFill:
                patterns.append(element)
        finally:
            pattern.Dispose()
    if not regions or not patterns:
        raise ValueError("Source graphics needs an EXISTING FilledRegion type and Solid fill pattern; no model type will be created or edited")
    regions.sort(key=lambda item: element_id(item.Id))
    patterns.sort(key=lambda item: element_id(item.Id))
    return regions[0], patterns[0]


def _vertices(bounds):
    return [[bounds[0], bounds[1]], [bounds[2], bounds[1]],
            [bounds[2], bounds[3]], [bounds[0], bounds[3]]]


def _edges(points):
    result = []
    for index in range(len(points)):
        result.append([points[index], points[(index+1) % len(points)]])
    return result


def _xyz(DB, xy, offset):
    return DB.XYZ((xy[0]+offset[0])/304.8, (xy[1]+offset[1])/304.8, 0)


def _zone_label(index, zone):
    bounds = zone["demand_bbox_mm"]
    lines = ["Z{0} | {1} | уровень {2}".format(index, zone["source_zone_id"], zone["level_index"]),
        "Пятно потребности: {0:g} x {1:g} мм (НЕ размер AreaReinforcement)".format(bounds[2]-bounds[0], bounds[3]-bounds[1])]
    for part in zone["components"]:
        window = part["axis_window_mm"]
        lines.append("Набор {0}: Ø{1:g}; условный шаг {2:g}; L={3:g}; окно W={4:g} мм; {5} шт.".format(
            part["component_index"], part["diameter_mm"], part["nominal_step_mm"],
            part["installed_length_mm"], window[1]-window[0], part["bar_count"]))
        lines.append("Фаза/схема: "+json.dumps(part.get("placement", {}), ensure_ascii=False, sort_keys=True))
    return "\n".join(lines)


def source_note_specs(row, provenance):
    """Pure source-derived annotation inventory, shared with independent readback."""
    key = _direction(row["direction"])
    names = {"bottom-X": "НИЗ X", "bottom-Y": "НИЗ Y", "top-X": "ВЕРХ X", "top-Y": "ВЕРХ Y"}
    bounds = list(row["source_bbox_mm"])
    for cell in row["cells"]:
        for point in cell["polygon_mm"]:
            bounds[0], bounds[1] = min(bounds[0], point[0]), min(bounds[1], point[1])
            bounds[2], bounds[3] = max(bounds[2], point[0]), max(bounds[3], point[1])
    for zone in row["zone_drafts"]:
        boxes = [zone["demand_bbox_mm"]]
        for part in zone["components"]:
            boxes.append(part["bar_axis_bbox_mm"])
        for box in boxes:
            bounds[0], bounds[1] = min(bounds[0], box[0]), min(bounds[1], box[1])
            bounds[2], bounds[3] = max(bounds[2], box[2]), max(bounds[3], box[3])
    result = [{"kind": "header", "xy_mm": [bounds[0], bounds[3]+2500], "text":
        LABEL+"\n"+names[key]+" | КЭ: {0}; исходных зон: {1}".format(len(row["cells"]), len(row["zone_drafts"]))+
        "\nСиний: пятно потребности demand_bbox. Фиолетовый: исходный набор после 40d/раскроя, НЕ граница Area."
        "\nНи один КЭ/набор не обрезан. Контур плиты, отверстия и cover здесь не проверяются."
        "\nИсточник: "+provenance+"; XY задан пользователем, Z не переносится."}]
    legend_y = bounds[3]+4500
    for band in row["legend"]:
        result.append({"kind": "legend", "xy_mm": [bounds[0], legend_y], "text":
            "Уровень {0} | ACI {1} | RGB {2} | {3}".format(band["level_index"], band["aci"], band["rgb"], band["label"])})
        legend_y += 400
    sidebar_y = bounds[3]
    for index, zone in enumerate(row["zone_drafts"], 1):
        box = zone["demand_bbox_mm"]
        result.append({"kind": "zone-key", "xy_mm": [box[0], box[3]], "text": "Z{0}".format(index)})
        description = _zone_label(index, zone)
        result.append({"kind": "zone-parameters", "xy_mm": [bounds[2]+1500, sidebar_y], "text": description})
        sidebar_y -= 400*(description.count("\n")+2)
    return result


def draw_source_views(document, DB, primitives, family_id, text_id, region_type, pattern,
                      loop_list_factory, report, progress):
    """Four independent views. Every FE polygon and BOTH source envelopes survive."""
    packet, offset = primitives["source_packet"], primitives["offset_xy_mm"]
    report["attempted_source_views"] = []
    report["source_cell_mapping"], report["source_curve_mapping"], report["source_note_mapping"] = [], [], []
    report["source_zone_records"] = []
    report["source_provenance"] = {"status": packet["provenance_status"], "files": copy.deepcopy(packet["source_files"]),
        "scope": "Input metadata only; no DXF/RVT identity or engineering approval is established"}
    report["source_stage"] = STAGE
    report["source_graphic_types"] = {"filled_region_type_id": element_id(region_type.Id),
                                      "solid_fill_pattern_id": element_id(pattern.Id)}
    total = primitives["summary"]["source_cell_count"]+primitives["summary"]["source_zone_count"]
    done = 0
    names = {"bottom-X": "НИЗ X", "bottom-Y": "НИЗ Y", "top-X": "ВЕРХ X", "top-Y": "ВЕРХ Y"}
    for row in packet["directions"]:
        key = _direction(row["direction"])
        view = DB.ViewDrafting.Create(document, family_id)
        view.ViewTemplateId = DB.ElementId.InvalidElementId
        view.Name = "QMonitoring SOURCE - {0} - НЕ АРМАТУРА - {1} - {2}".format(
            names[key], datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f"), element_id(view.Id))
        view.Scale = 100  # Coordinates still convert ONLY mm/304.8, never /Scale.
        report["attempted_source_views"].append({"direction": key, "view_id": element_id(view.Id), "name": text_type(view.Name)})
        colors = {}
        for band in row["legend"]:
            colors[band["level_index"]] = band["rgb"]
        for cell in row["cells"]:
            progress("source-isofields", done, total)
            loop = DB.CurveLoop()
            for edge in _edges(cell["polygon_mm"]):
                start, end = _xyz(DB, edge[0], offset), _xyz(DB, edge[1], offset)
                if start.DistanceTo(end) <= document.Application.ShortCurveTolerance:
                    raise ValueError("Original FE has a Revit-short edge; no polygon will be simplified or omitted")
                loop.Append(DB.Line.CreateBound(start, end))
            loops = loop_list_factory()
            if hasattr(loops, "Add"):
                loops.Add(loop)
            else:
                loops.append(loop)
            try:
                region = DB.FilledRegion.Create(document, region_type.Id, view.Id, loops)
            finally:
                loop.Dispose()
            if region.OwnerViewId != view.Id:
                raise ValueError("Source FE graphic belongs to another view")
            color = colors[cell["level_index"]]
            style = DB.OverrideGraphicSettings()
            try:
                style.SetSurfaceForegroundPatternId(pattern.Id)
                style.SetSurfaceForegroundPatternColor(DB.Color(*color))
                style.SetSurfaceForegroundPatternVisible(True)
                style.SetSurfaceBackgroundPatternVisible(False)
                style.SetProjectionLineColor(DB.Color(*color))
                style.SetProjectionLineWeight(1)
                view.SetElementOverrides(region.Id, style)
            finally:
                style.Dispose()
            report["created_annotation_ids"].append(element_id(region.Id))
            report["source_cell_mapping"].append({"direction": key, "cell_id": cell["cell_id"],
                "level_index": cell["level_index"], "aci": cell["aci"], "rgb": list(color),
                "polygon_mm": copy.deepcopy(cell["polygon_mm"]), "view_id": element_id(view.Id),
                "element_id": element_id(region.Id)})
            done += 1
        for index, zone in enumerate(row["zone_drafts"], 1):
            progress("source-zones", done, total)
            zid = zone["source_zone_id"]
            _rectangle(document, DB, view, zone["demand_bbox_mm"], offset, (25, 65, 110), 4,
                       key, zid, "demand_bbox", None, report)
            for part in zone["components"]:
                _rectangle(document, DB, view, part["bar_axis_bbox_mm"], offset, (125, 50, 165), 2,
                           key, zid, "source_component_axis_envelope", part["component_index"], report)
            report["source_zone_records"].append({"direction": key, "view_id": element_id(view.Id),
                "display_key": "Z{0}".format(index), "original_zone_draft": copy.deepcopy(zone)})
            done += 1
        for note in source_note_specs(row, packet["provenance_status"]):
            _note(document, DB, view, text_id, note["xy_mm"], offset, note["text"], report, note["kind"])
    progress("source-graphics-complete", total, total)


def _rectangle(document, DB, view, bounds, offset, color, weight, direction, zone_id, kind, part, report):
    seen = set()
    for edge in _edges(_vertices(bounds)):
        if edge[0] == edge[1]:
            continue  # Exact zero-width single-axis envelope is one line, not a rectangle.
        canonical = tuple(sorted((tuple(edge[0]), tuple(edge[1]))))
        if canonical in seen:
            continue
        seen.add(canonical)
        start, end = _xyz(DB, edge[0], offset), _xyz(DB, edge[1], offset)
        if start.DistanceTo(end) <= document.Application.ShortCurveTolerance:
            raise ValueError("Original zone has a Revit-short edge; no envelope truncation")
        line = document.Create.NewDetailCurve(view, DB.Line.CreateBound(start, end))
        style = DB.OverrideGraphicSettings()
        try:
            style.SetProjectionLineColor(DB.Color(*color))
            style.SetProjectionLineWeight(weight)
            view.SetElementOverrides(line.Id, style)
        finally:
            style.Dispose()
        report["created_annotation_ids"].append(element_id(line.Id))
        report["source_curve_mapping"].append({"direction": direction, "zone_id": zone_id,
            "kind": kind, "component_index": part, "view_id": element_id(view.Id),
            "element_id": element_id(line.Id), "endpoints_mm": copy.deepcopy(edge)})


def _note(document, DB, view, text_id, point, offset, content, report, kind):
    # Revit text paragraphs use CR; LF is not a supported FormattedText control.
    native_text = content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r")
    if len(native_text) > 29000:
        raise ValueError("Source parameter note exceeds Revit text budget; no truncation")
    element = DB.TextNote.Create(document, view.Id, _xyz(DB, point, offset), native_text, text_id)
    report["created_annotation_ids"].append(element_id(element.Id))
    report["source_note_mapping"].append({"view_id": element_id(view.Id), "element_id": element_id(element.Id),
        "kind": kind, "text": content})


def _actual_edge(curve):
    result = []
    for index in (0, 1):
        point = curve.GetEndPoint(index)
        result.append([float(point.X)*304.8, float(point.Y)*304.8, float(point.Z)*304.8])
    return result


def _expected_edge(edge, offset):
    result = []
    for point in edge:
        result.append([point[0]+offset[0], point[1]+offset[1], 0])
    return result


def _curve_key(direction, zone_id, kind, part, edge):
    return (direction, zone_id, kind, part, tuple(sorted((tuple(edge[0]), tuple(edge[1])))))


def _normal_text(value):
    return text_type(value).replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def readback_source_views(document, DB, primitives, report, endpoints_match):
    """Post-COMMIT check of ALL four views, every source FE/edge and parameter text."""
    views = {}
    for row in report["attempted_source_views"]:
        view = document.GetElement(DB.ElementId(row["view_id"]))
        if not isinstance(view, DB.ViewDrafting) or row["direction"] in views:
            raise ValueError("Source DraftingView missing or duplicated after commit")
        views[row["direction"]] = view
    if set(views) != set(DIRECTIONS):
        raise ValueError("Committed source preview must contain all four views")
    expected_cells, expected_curves, expected_zones = {}, set(), []
    expected_colors, expected_notes = {}, {}
    offset = primitives["offset_xy_mm"]
    for row in primitives["source_packet"]["directions"]:
        key = _direction(row["direction"])
        for band in row["legend"]:
            expected_colors[(key, band["level_index"])] = band["rgb"]
        for note in source_note_specs(row, primitives["source_packet"]["provenance_status"]):
            note_key = (element_id(views[key].Id), note["kind"], _normal_text(note["text"]))
            expected_notes[note_key] = expected_notes.get(note_key, 0)+1
        for cell in row["cells"]:
            expected_cells[(key, text_type(cell["cell_id"]))] = cell
        for index, zone in enumerate(row["zone_drafts"], 1):
            expected_zones.append({"direction": key, "view_id": element_id(views[key].Id),
                "display_key": "Z{0}".format(index), "original_zone_draft": zone})
            frames = [("demand_bbox", None, zone["demand_bbox_mm"])]
            for part in zone["components"]:
                frames.append(("source_component_axis_envelope", part["component_index"], part["bar_axis_bbox_mm"]))
            for kind, part, bbox in frames:
                for edge in _edges(_vertices(bbox)):
                    if edge[0] != edge[1]:
                        expected_curves.add(_curve_key(key, zone["source_zone_id"], kind, part, edge))
    if report["source_zone_records"] != expected_zones:
        raise ValueError("Original parametric source metadata was altered or omitted")
    checked_cells, checked_curves, checked_elements = set(), set(), set()
    for row in report["source_cell_mapping"]:
        key = (row["direction"], text_type(row["cell_id"]))
        if key not in expected_cells or key in checked_cells or row["element_id"] in checked_elements:
            raise ValueError("Duplicate/unknown committed source FE")
        checked_cells.add(key)
        checked_elements.add(row["element_id"])
        view = views[row["direction"]]
        element = document.GetElement(DB.ElementId(row["element_id"]))
        if not isinstance(element, DB.FilledRegion) or element.OwnerViewId != view.Id:
            raise ValueError("Committed source FE missing or belongs to another view")
        loops = list(element.GetBoundaries())
        try:
            if len(loops) != 1:
                raise ValueError("Source FE gained/removed boundary loops")
            actual = []
            for curve in loops[0]:
                if not isinstance(curve, DB.Line):
                    raise ValueError("Original straight source FE became curved")
                actual.append(_actual_edge(curve))
            expected = _edges(expected_cells[key]["polygon_mm"])
            if len(actual) != len(expected):
                raise ValueError("Source FE vertex count changed after commit")
            for edge in expected:
                match = None
                for index, candidate in enumerate(actual):
                    if endpoints_match(candidate, _expected_edge(edge, offset)):
                        match = index
                        break
                if match is None:
                    raise ValueError("Committed source FE boundary differs in millimetres")
                actual.pop(match)
        finally:
            for loop in loops:
                loop.Dispose()
        style = view.GetElementOverrides(element.Id)
        try:
            color = style.SurfaceForegroundPatternColor
            expected_color = expected_colors[(key[0], expected_cells[key]["level_index"])]
            if ([int(color.Red), int(color.Green), int(color.Blue)] != expected_color
                    or row["rgb"] != expected_color
                    or element_id(style.SurfaceForegroundPatternId) != report["source_graphic_types"]["solid_fill_pattern_id"]
                    or not style.IsSurfaceForegroundPatternVisible):
                raise ValueError("Committed source FE fill override differs")
        finally:
            style.Dispose()
    if checked_cells != set(expected_cells):
        raise ValueError("Source FE was omitted from committed views")
    for row in report["source_curve_mapping"]:
        key = _curve_key(row["direction"], row["zone_id"], row["kind"], row["component_index"], row["endpoints_mm"])
        if key not in expected_curves or key in checked_curves or row["element_id"] in checked_elements:
            raise ValueError("Duplicate/unknown committed source rectangle edge")
        checked_curves.add(key)
        checked_elements.add(row["element_id"])
        element = document.GetElement(DB.ElementId(row["element_id"]))
        if (not isinstance(element, DB.DetailCurve) or element.OwnerViewId != views[row["direction"]].Id
                or not isinstance(element.GeometryCurve, DB.Line)
                or not endpoints_match(_actual_edge(element.GeometryCurve), _expected_edge(row["endpoints_mm"], offset))):
            raise ValueError("Committed source rectangle differs from the original")
    if checked_curves != expected_curves:
        raise ValueError("Source envelope edge was omitted")
    for row in report["source_note_mapping"]:
        note_key = (row["view_id"], row["kind"], _normal_text(row["text"]))
        if expected_notes.get(note_key, 0) < 1:
            raise ValueError("Unknown/duplicate source label or parameter text")
        expected_notes[note_key] -= 1
        element = document.GetElement(DB.ElementId(row["element_id"]))
        if (element is None or row["element_id"] in checked_elements or element.OwnerViewId != DB.ElementId(row["view_id"])
                or _normal_text(element.Text) != _normal_text(row["text"])):
            raise ValueError("Committed source labels/parameters are missing or changed")
        checked_elements.add(row["element_id"])
    if any(expected_notes.values()):
        raise ValueError("Original source label or parameter note was omitted")
    if checked_elements != set(report["created_annotation_ids"]):
        raise ValueError("Source annotation readback did not cover the complete created inventory")
    return {"status": "matches", "checked_view_count": len(views), "checked_source_cell_count": len(checked_cells),
        "checked_source_zone_count": len(expected_zones), "checked_rectangle_edge_count": len(checked_curves),
        "checked_text_note_count": len(report["source_note_mapping"]), "endpoint_tolerance_mm": 0.01,
        "scope": "Original FE/zone annotation geometry and text only; not normalized bars or structural placement"}
