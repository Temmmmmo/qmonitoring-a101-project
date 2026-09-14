"""Bounded inspection of uploaded facts, never a live-RVT or engineering check."""
from __future__ import annotations

import hashlib
import json
import math

MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_JSON_NODES = 2_000_000
MAX_JSON_DEPTH = 64
MAX_STRING_LENGTH = 200_000
SCHEMAS = {"host": "revit-working-host-cad-probe/v1", "rebar": "revit-working-rebar-probe/v1"}


class SnapshotTooLargeError(ValueError):
    """The complete upload, not a truncated prefix, exceeds the read budget."""


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(label + ": нужен JSON-объект")
    return value


def _array(value, label, maximum=30_000):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(label + ": неверный массив или превышен лимит")
    return value


def _integer(value, label, maximum=2**63 - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(label + ": нужно неотрицательное целое число в пределах лимита")
    return value


def _text(value, label, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(label + ": нужна непустая строка длиной до 4096 символов")
    return value


def _number(value, label, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(label + ": неверное конечное число")
    return value


def _point(value, label):
    if len(_array(value, label, 3)) != 3:
        raise ValueError(label + ": нужны три координаты")
    return [_number(number, label) for number in value]


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON содержит повторяющиеся ключи")
        result[key] = value
    return result


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("JSON содержит бесконечное число")
    return result


def _constant(value):
    raise ValueError("JSON содержит NaN или Infinity")


def _load(content, kind):
    if not isinstance(content, bytes) or not content:
        raise ValueError("Нужен непустой JSON-снимок " + kind)
    if len(content) > MAX_SNAPSHOT_BYTES:
        raise SnapshotTooLargeError("Суммарный размер JSON превышает 32 MiB")
    try:
        data = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_pairs,
            parse_float=_float, parse_constant=_constant)
    except (UnicodeError, RecursionError, json.JSONDecodeError) as error:
        raise ValueError("Невалидный UTF-8 JSON-снимок " + kind) from error
    count, stack = 0, [(data, 0)]
    while stack:
        value, depth = stack.pop()
        count += 1
        if depth > MAX_JSON_DEPTH or count > MAX_JSON_NODES:
            raise ValueError("JSON превышает лимит вложенности или числа значений")
        if isinstance(value, dict):
            for key, item in value.items():
                stack.append((key, depth + 1))
                stack.append((item, depth + 1))
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            if len(value) > MAX_STRING_LENGTH or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
                raise ValueError("JSON содержит слишком длинную строку или непарный Unicode surrogate")
        elif type(value) is int and abs(value) > 2**63 - 1:
            raise ValueError("JSON содержит целое число вне 64-битного диапазона")
    _object(data, kind)
    if data.get("schema_version") != SCHEMAS[kind]:
        raise ValueError("Неверная схема для поля " + kind + ": нужна " + SCHEMAS[kind])
    if data.get("read_only") is not True or any(data.get(key) is not False
            for key in ("placement_eligible", "engineering_approval")):
        raise ValueError("Нужен read-only снимок без разрешения на размещение")
    if data.get("units") != "mm" or data.get("coordinate_system") != "revit-internal-origin-and-axes":
        raise ValueError("Нужны миллиметры и внутренние координаты Revit")
    if data.get("status") not in ("collected", "partial"):
        raise ValueError("Неизвестный статус снимка")
    return data


def _issues(data):
    issues = []
    seen = set()
    for key in ("read_issues", "host_read_issues", "issues"):
        for row in _array(data.get(key, []), key, 20_000):
            _object(row, key)
            section = _text(row.get("section", "report"), key)
            message = _text(row.get("message"), key)
            pair = (section, message)
            if pair not in seen:
                seen.add(pair)
                issues.append({"section": section[:160], "message": message[:600]})
    return issues


def _common(data, content):
    document = _object(data.get("document"), "document")
    title = _text(document.get("title"), "document.title")
    if type(document.get("is_workshared")) is not bool:
        raise ValueError("document.is_workshared: нужен boolean")
    host_id = _integer(data.get("host_id"), "host_id")
    host = data.get("host")
    uid = data.get("host_unique_id")
    host_summary = {"element_id": host_id, "unique_id": uid, "name": None, "mark": None,
        "bbox_mm": None, "cover_mm": {}}
    if host is not None:
        _object(host, "host")
        if host.get("element_id") != host_id:
            raise ValueError("host.element_id не совпадает с host_id")
        native_uid = _text(host.get("unique_id"), "host.unique_id")
        if uid is not None and uid != native_uid:
            raise ValueError("host_unique_id не совпадает с host.unique_id")
        uid = native_uid
        for key in ("name", "mark"):
            if host.get(key) is not None and not isinstance(host[key], str):
                raise ValueError("host." + key + ": нужна строка или null")
            host_summary[key] = host.get(key)
        bbox = host.get("bbox_mm")
        if bbox is not None:
            _object(bbox, "host.bbox_mm")
            lower, upper = _point(bbox.get("min_mm"), "bbox.min"), _point(bbox.get("max_mm"), "bbox.max")
            if any(lo > hi for lo, hi in zip(lower, upper)):
                raise ValueError("Перевёрнутый host bbox")
            host_summary["bbox_mm"] = {"min_mm": lower, "max_mm": upper}
        covers = _object(host.get("covers", {}), "host.covers")
        for key in ("top", "bottom", "other"):
            row = covers.get(key)
            distance = None if row is None else _object(row, "cover").get("distance_mm")
            if distance is not None and _number(distance, "cover") < 0:
                raise ValueError("Отрицательный защитный слой")
            host_summary["cover_mm"][key] = distance
    host_summary["unique_id"] = _text(uid, "host UID", optional=True)
    path = document.get("path")
    if path is not None and not isinstance(path, str):
        raise ValueError("document.path: нужна строка или null")
    project_uid = _text(document.get("project_information_unique_id"), "project UID", optional=True)
    issues = _issues(data)
    modification = document.get("modification_flag_unchanged")
    if modification not in (True, False, None) or (modification is not None and type(modification) is not bool):
        raise ValueError("Неверный modification_flag_unchanged")
    return {"schema_version": data["schema_version"], "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content), "probe_version": _text(data.get("probe_version"), "probe_version"),
        "created_utc": _text(data.get("created_utc"), "created_utc"), "reported_status": data["status"],
        "host": host_summary, "document": {"title": title, "is_workshared": document["is_workshared"],
            "project_information_unique_id": project_uid,
            "path_sha256": hashlib.sha256(path.encode("utf-8")).hexdigest() if path else None,
            "modification_flag_unchanged_as_reported": modification},
        "read_issue_count": len(issues), "read_issues": issues[:50],
        "read_issues_omitted_from_display": max(0, len(issues) - 50)}


def _host_summary(data, content):
    result = _common(data, content)
    solid = data.get("host_solid")
    face_count, volume = None, None
    if solid is not None:
        solid = _object(solid, "host_solid")
        face_count = len(_array(solid.get("faces"), "host_solid.faces", 50_000))
        volume = _number(solid.get("volume_mm3"), "host_solid.volume_mm3", positive=True)
    result["geometry"] = {"solid_face_record_count": face_count, "reported_volume_mm3": volume,
        "solid_topology_checked": False, "cad_binding_checked": False,
        "note": "Наличие записей Solid, не проверка границ, проёмов или привязки DXF."}
    result["worksharing"] = {"closed_worksets": None,
        "note": "Working Host Probe не перечисляет закрытые рабочие наборы."}
    return result


def _curve(curve):
    _object(curve, "curve")
    kind = _text(curve.get("kind"), "curve.kind")
    supported = kind in ("Line", "Arc")
    if curve.get("is_bound") is not True or curve.get("exact_geometry_supported") is not supported:
        raise ValueError("Несогласованный тип/флаг кривой")
    for key in ("start_mm", "end_mm", "mid_mm"):
        _point(curve.get(key), "curve." + key)
    _number(curve.get("length_mm"), "curve.length_mm", positive=True)
    if kind == "Arc":
        for key in ("center_mm", "normal", "x_direction", "y_direction"):
            _point(curve.get(key), "arc." + key)
        _number(curve.get("radius_mm"), "arc.radius_mm", positive=True)
        for key in ("start_parameter", "end_parameter"):
            _number(curve.get(key), "arc." + key)
        if curve.get("parameter_units") != "radians":
            raise ValueError("Arc: нужны параметры в радианах")
    for point in _array(curve.get("tessellated_points_mm"), "tessellation", 2048):
        _point(point, "tessellation point")
    return supported


def _rebar_summary(data, content):
    result = _common(data, content)
    bars = _array(data.get("bars"), "bars", 5000)
    declared = _object(data.get("summary"), "summary")
    seen_uids, seen_ids, bar_by_id = set(), set(), {}
    counts = {"read_existing_position_count": 0, "exact_geometry_position_count": 0,
        "excluded_position_count": 0, "unknown_position_count": 0, "curve_record_count": 0}
    all_complete, positions_total = True, 0
    for bar in bars:
        _object(bar, "bar")
        uid, eid = _text(bar.get("unique_id"), "bar UID"), _integer(bar.get("element_id"), "bar ID")
        if uid in seen_uids or eid in seen_ids:
            raise ValueError("Повторный элемент арматуры: UID/ElementId должен быть уникален")
        seen_uids.add(uid)
        seen_ids.add(eid)
        bar_by_id[eid] = bar
        if bar.get("host_id") != data["host_id"]:
            raise ValueError("Арматура относится к другой плите")
        if type(bar.get("readback_complete")) is not bool:
            raise ValueError("bar.readback_complete: нужен boolean")
        positions = _array(bar.get("positions"), "positions")
        total = bar.get("number_of_bar_positions")
        quantity = bar.get("quantity")
        if total is not None:
            _integer(total, "number_of_bar_positions", 250_000)
            _integer(quantity, "quantity", total)
            if len(positions) > total:
                raise ValueError("Положений больше NumberOfBarPositions")
        elif positions or bar["readback_complete"]:
            raise ValueError("Нет NumberOfBarPositions для прочитанных положений")
        included, exact, excluded = 0, 0, 0
        for index, position in enumerate(positions):
            _object(position, "position")
            if type(position.get("position_index")) is not int or position["position_index"] != index:
                raise ValueError("Неверный или повторный position_index")
            if position.get("position_key") != [uid, index]:
                raise ValueError("position_key не соответствует UID/index")
            exists = position.get("exists")
            status = position.get("status")
            if exists is not None and type(exists) is not bool:
                raise ValueError("position.exists: нужен boolean или null")
            if status not in ("collected", "excluded", "read_failed"):
                raise ValueError("Неизвестный статус положения")
            curves = _array(position.get("curves"), "curves", 30_000)
            if exists is not True and curves:
                raise ValueError("Не существующее положение содержит кривые")
            supported = [_curve(curve) for curve in curves]
            counts["curve_record_count"] += len(curves)
            if status == "collected" and (exists is not True or not curves or not all(supported)):
                raise ValueError("collected положение без поддержанных записей кривых")
            if exists is False and status != "excluded" or status == "excluded" and exists is not False:
                raise ValueError("Несогласованное исключённое положение")
            included += exists is True
            exact += status == "collected"
            excluded += exists is False
        positions_total += len(positions)
        if positions_total > 30_000 or counts["curve_record_count"] > 300_000:
            raise ValueError("Превышен суммарный лимит положений или кривых")
        if total is not None and len(positions) == total and all(p.get("exists") is not None for p in positions):
            if included != quantity:
                raise ValueError("Quantity не совпадает с прочитанными включёнными положениями")
        if bar["readback_complete"] and (total is None or len(positions) != total or included != quantity
                or exact != included or included + excluded != total or bar.get("physical_bar_count") != included):
            raise ValueError("Полнота bar readback не соответствует положениям")
        all_complete = all_complete and bar["readback_complete"]
        counts["read_existing_position_count"] += included
        counts["exact_geometry_position_count"] += exact
        counts["excluded_position_count"] += excluded
        counts["unknown_position_count"] += (total or len(positions)) - included - excluded
    parents = _array(data.get("parents"), "parents", 5000)
    parent_ids, parent_uids = set(), set()
    parent_links_complete = True
    for parent in parents:
        _object(parent, "parent")
        eid, uid = _integer(parent.get("element_id"), "parent ID"), _text(parent.get("unique_id"), "parent UID")
        if eid in parent_ids or uid in parent_uids or eid in seen_ids or uid in seen_uids:
            raise ValueError("Повторный parent или двойной учёт parent/bar")
        parent_ids.add(eid)
        parent_uids.add(uid)
        if parent.get("host_id") != data["host_id"]:
            raise ValueError("Area/Path относится к другой плите")
        children = parent.get("child_ids")
        if children is None:
            parent_links_complete = False
            continue
        children = _array(children, "child_ids", 5000)
        for child in children:
            _integer(child, "child ID")
        if len(set(children)) != len(children):
            raise ValueError("Повторный child ID")
        if not children or any(bar_by_id.get(child, {}).get("system_id") != eid for child in children):
            parent_links_complete = False
    for bar in bars:
        if "system_id" in bar and not any(parent["element_id"] == bar["system_id"]
                and bar["element_id"] in (parent.get("child_ids") or []) for parent in parents):
            parent_links_complete = False
    worksharing = data.get("worksharing")
    closed, worksets_known = [], worksharing is not None
    if worksharing is not None:
        _object(worksharing, "worksharing")
        worksets = _array(worksharing.get("user_worksets"), "user_worksets", 10_000)
        for row in worksets:
            _object(row, "workset")
            if type(row.get("is_open")) is not bool:
                raise ValueError("workset.is_open: нужен boolean")
            if not row["is_open"]:
                closed.append(_text(row.get("name"), "workset.name"))
        claimed_closed = _array(worksharing.get("closed_user_worksets"), "closed_user_worksets", 10_000)
        if claimed_closed != [row for row in worksets if not row["is_open"]]:
            raise ValueError("Список закрытых worksets не соответствует user_worksets")
    scope = _object(data.get("scope"), "scope")
    unsupported = _array(scope.get("unsupported_host_elements"), "unsupported_host_elements", 5000)
    links = _array(scope.get("links"), "links", 1000)
    complete = (all_complete and parent_links_complete and worksets_known and not closed and not unsupported
        and not result["read_issue_count"] and data["status"] == "collected"
        and result["document"]["modification_flag_unchanged_as_reported"] is True)
    expected = {key: counts[key] for key in ("read_existing_position_count", "exact_geometry_position_count", "excluded_position_count")}
    expected["host_reinforcement_element_count_read"] = len(bars)
    for key, value in expected.items():
        if type(declared.get(key)) is not int or declared[key] != value:
            raise ValueError("Сводка не совпадает с прочитанными записями: " + key)
    if type(declared.get("native_host_inventory_complete")) is not bool:
        raise ValueError("native_host_inventory_complete: нужен boolean")
    if declared["native_host_inventory_complete"] and not complete:
        raise ValueError("Заявлена полнота native inventory при пробелах чтения")
    complete = complete and declared["native_host_inventory_complete"]
    expected_physical = counts["read_existing_position_count"] if complete else None
    if declared.get("physical_bar_count") != expected_physical or (expected_physical is not None
            and type(declared.get("physical_bar_count")) is not int):
        raise ValueError("physical_bar_count не соответствует полноте снимка")
    result["inventory"] = {**counts, "host_reinforcement_element_count_read": len(bars),
        "parent_system_count": len(parents), "physical_bar_count_as_reported": expected_physical,
        "status": "complete_as_reported" if complete else "partial",
        "parent_links_consistent": parent_links_complete, "unsupported_host_element_count": len(unsupported),
        "linked_model_count": len(links), "engineering_roles_classified": False,
        "geometry_truth_checked": False, "whole_model_collision_inventory_complete": False}
    result["worksharing"] = {"closed_worksets": closed if worksets_known else None,
        "note": "Закрытые worksets не открываются; арматура других hosts и связей не проверяется."}
    return result


def _binding(host, rebar):
    result = {"status": "not_compared", "host_uid_matches": None, "host_element_id_matches": None,
        "document_project_uid_matches": None, "document_path_hash_matches": None,
        "live_model_checked": False,
        "note": "Сравниваются только переданные файлы. Копии RVT могут сохранять UID; актуальность модели не проверена."}
    if host is None or rebar is None:
        return result
    left, right = host["host"], rebar["host"]
    if left["unique_id"] is not None and right["unique_id"] is not None:
        result["host_uid_matches"] = left["unique_id"] == right["unique_id"]
    result["host_element_id_matches"] = left["element_id"] == right["element_id"]
    for key, output in (("project_information_unique_id", "document_project_uid_matches"),
            ("path_sha256", "document_path_hash_matches")):
        left, right = host["document"][key], rebar["document"][key]
        if left is not None and right is not None:
            result[output] = left == right
    if any(result[key] is False for key in ("host_uid_matches", "host_element_id_matches", "document_project_uid_matches")):
        result["status"] = "mismatch"
    elif result["host_uid_matches"] is True:
        result["status"] = "recorded_identity_matches" if result["document_project_uid_matches"] is True else "host_matches_document_unverified"
    else:
        result["status"] = "insufficient_identity"
    return result


def inspect_revit_snapshots(*, host: bytes | None = None, rebar: bytes | None = None) -> dict:
    """Inspect complete uploaded byte strings, without files, state or engine calls."""
    if host is None and rebar is None:
        raise ValueError("Выберите JSON геометрии плиты или существующей арматуры")
    if sum(len(value) for value in (host, rebar) if value is not None) > MAX_SNAPSHOT_BYTES:
        raise SnapshotTooLargeError("Суммарный размер JSON превышает 32 MiB")
    summaries = {}
    for kind, content, handler in (("host", host, _host_summary), ("rebar", rebar, _rebar_summary)):
        summaries[kind] = None if content is None else handler(_load(content, kind), content)
    return {"schema_version": "qmonitoring-revit-snapshot-inspection/v1", "mode": "uploaded_facts_only",
        "placement_eligible": False, "engineering_approval": False, "live_model_checked": False,
        "source_inputs_retained": False, "connected_to_calculation": False,
        "binding": _binding(summaries["host"], summaries["rebar"]), "snapshots": summaries,
        "not_checked": ["Актуальность или подлинность файлов и текущего RVT", "Топология Solid и привязка DXF",
            "Фон/дополнительная/краевая роль, фазы и рабочие высоты", "Потребность КЭ, анкеровка и коллизии",
            "Связанные модели и арматура других hosts", "Инженерная приёмка и разрешение на размещение"]}
