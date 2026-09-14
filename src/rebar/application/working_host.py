"""Full/Physical Plan Trial snapshot -> explicit, independently checked optimizer host.

No model/FE coordinate guessing and no replacement of an unsupported shape by its bbox.
The supported shape is one axis-aligned rectangular prism with rectangular through holes.
"""
from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
import json
import math

from rebar.optimization.contracts.host import RectangularHostEnvelope

from .composite_host_review import TOLERANCE, _close, _point, _rectangle, rectangular_host_from_floor
from .composite_layout_review import _reject, _unique

HOST_INPUT_SCHEMA = "qmonitoring-host-input/v1"
HOST_TRANSLATION_POLICY = "explicit-source-xy-plus-translation-equals-revit-xy/research-v1"
FULL_TRIAL_SCHEMA = "revit-full-plate-trial-report/v1"
PHYSICAL_TRIAL_SCHEMA = "revit-physical-bar-plan-trial-report/v1"


def load_working_host_json(raw: bytes, *, maximum_bytes: int = 8 * 1024 * 1024) -> dict:
    if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= 32 * 1024 * 1024:
        raise ValueError("Лимит JSON host должен быть задан в пределах 32 MiB")
    if not raw or len(raw) > maximum_bytes:
        raise ValueError("JSON host пуст или превышает заданный лимит байт")
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique, parse_constant=_reject)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Неверный UTF-8 JSON host") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON host должен содержать объект")
    return data


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 1e9:
        raise ValueError("Нужна конечная координата/величина без неявного преобразования")
    return value


def _expected_faces(host):
    """Descriptors: normal axis, sign, elevation, rectangles in the other two axes."""
    bottom, top = host.bottom_z_mm, host.top_z_mm
    xy = tuple(sorted((host.outer_mm, *host.openings_mm)))
    faces = [(2, -1, bottom, xy), (2, 1, top, xy)]
    for i, box in enumerate((host.outer_mm, *host.openings_mm)):
        x, y, right, upper = box
        sign = 1 if i else -1
        faces.extend(((0, sign, x, ((y, bottom, upper, top),)),
                      (0, -sign, right, ((y, bottom, upper, top),)),
                      (1, sign, y, ((x, bottom, right, top),)),
                      (1, -sign, upper, ((x, bottom, right, top),))))
    return faces


def validate_prism_solid(host: RectangularHostEnvelope, solid: dict) -> None:
    """Check ALL side faces, hole walls, top/bottom and volume, not just top loops."""
    expected = _expected_faces(host)
    if not isinstance(solid.get("faces"), list) or len(solid["faces"]) != len(expected):
        raise ValueError("Solid содержит дополнительные/отсутствующие грани; прямоугольная призма не подтверждена")
    for face in solid["faces"]:
        plane = face["plane"]
        if plane is None:
            raise ValueError("Криволинейная грань не заменяется прямоугольником")
        normal, origin = _point(plane["normal"]), _point(plane["origin_mm"])
        axes = [i for i in range(3) if abs(abs(normal[i]) - 1) < 1e-8
                and all(abs(normal[j]) < 1e-8 for j in range(3) if i != j)]
        if len(axes) != 1:
            raise ValueError("Наклонная грань Solid не поддерживается")
        axis = axes[0]
        others = [i for i in range(3) if i != axis]
        loops = face["edge_loops"]
        if not isinstance(loops, list) or not 1 <= len(loops) <= 129:
            raise ValueError("Неверные контуры грани Solid")
        rectangles = []
        for loop in loops:
            projected = []
            for edge in loop:
                coordinates = {}
                for key in ("start_mm", "end_mm"):
                    point = _point(edge[key])
                    coordinates[key] = [point[others[0]], point[others[1]], point[axis]]
                projected.append({**edge, **coordinates})
            rectangles.append(_rectangle(projected, origin[axis]))
        rectangles.sort()
        matches = [i for i, (a, sign, elevation, boxes) in enumerate(expected)
                   if a == axis and abs(normal[axis] - sign) < 1e-8 and abs(origin[axis] - elevation) <= TOLERANCE
                   and len(boxes) == len(rectangles) and all(_close(x, y) for x, y in zip(boxes, rectangles))]
        if len(matches) != 1:
            raise ValueError("Грани Solid не совпадают с полной призмой и стенками её проёмов")
        expected.pop(matches[0])
    def area(box):
        return (box[2] - box[0]) * (box[3] - box[1])
    volume = (area(host.outer_mm) - math.fsum(area(box) for box in host.openings_mm)) * (host.top_z_mm - host.bottom_z_mm)
    actual_volume = solid["volume_mm3"]
    if (isinstance(actual_volume, bool) or not isinstance(actual_volume, (float, int)) or not math.isfinite(actual_volume)
            or not math.isclose(actual_volume, volume, rel_tol=1e-8, abs_tol=1)):
        raise ValueError("Объём Solid не совпадает с призмой за вычетом сквозных проёмов")


def validate_working_snapshot(report):
    """Validate read/rollback evidence independently of the supported host shape."""
    schema = report.get("schema_version")
    if (schema not in (FULL_TRIAL_SCHEMA, PHYSICAL_TRIAL_SCHEMA) or report.get("units") != "mm"
            or report.get("coordinate_system") != "revit-internal-origin-and-axes"
            or report.get("placement_eligible") is not False or report.get("read_issues") != []):
        raise ValueError("Нужен полный снимок выбранной плиты из Full/Physical Plan Trial, без замечаний чтения")
    status = report.get("status")
    if status not in ("blocked_setup", "blocked_preflight", "passed_rolled_back", "failed_rolled_back"):
        raise ValueError("Состояние модели/отката не подтверждено")
    evidence = report
    if schema == PHYSICAL_TRIAL_SCHEMA:
        if report.get("engineering_approval") is not False:
            raise ValueError("Физический trial не является инженерским разрешением")
        if "execution" in report:
            evidence = report["execution"]
            if (not isinstance(evidence, dict) or evidence.get("schema_version") != FULL_TRIAL_SCHEMA
                    or evidence.get("status") != status or evidence.get("placement_eligible") is not False
                    or evidence.get("read_issues") != []
                    or evidence.get("host", report["host"]) != report["host"]):
                raise ValueError("Вложенный отчёт исполнения не совпадает со снимком рабочей плиты")
        elif status != "blocked_setup":
            raise ValueError("Нет отчёта исполнения физического trial")
    if status.endswith("rolled_back") and evidence.get("restoration", {}).get("verified") is not True:
        raise ValueError("Нет подтверждения восстановления модели после пробного создания")
    floor = report["host"]
    host_id = floor["element_id"]
    if isinstance(host_id, bool) or not isinstance(host_id, int) or host_id <= 0:
        raise ValueError("Неверный идентификатор host")
    if "host_id" in report and (type(report["host_id"]) is not int or report["host_id"] != host_id):
        raise ValueError("Идентификаторы выбранной плиты и снимка не совпадают")
    return floor


def _snapshot_host(report):
    floor = validate_working_snapshot(report)
    host_id = floor["element_id"]
    schema = report["schema_version"]
    trial_name = "Physical Plan Trial" if schema == PHYSICAL_TRIAL_SCHEMA else "Full Plate Trial"
    host = rectangular_host_from_floor(floor, source=f"{trial_name} floor {host_id}; full serialized prism checked; live model not checked")
    validate_prism_solid(host, report["host_solid"])
    return host


def prepare_working_host(report: dict, *, offset_x_mm: float, offset_y_mm: float, source_report_sha256: str) -> dict:
    """XY convention is Revit = source + offset. No input geometry is mutated."""
    _snapshot_host(report)
    _number(offset_x_mm)
    _number(offset_y_mm)
    if (not isinstance(source_report_sha256, str) or len(source_report_sha256) != 64
            or any(c not in "0123456789abcdef" for c in source_report_sha256)):
        raise ValueError("Нужен SHA256 исходного JSON")
    keys = ("schema_version", "units", "coordinate_system", "placement_eligible", "read_issues", "status", "host", "host_solid")
    snapshot = {key: report[key] for key in keys}
    if "restoration" in report:
        snapshot["restoration"] = report["restoration"]
    if report["schema_version"] == PHYSICAL_TRIAL_SCHEMA:
        snapshot["engineering_approval"] = report["engineering_approval"]
        if "execution" in report:
            # Retain native restoration evidence, not the entire reinforcement/readback payload.
            snapshot["execution"] = {key: value for key, value in report["execution"].items()
                                     if key in ("schema_version", "status", "placement_eligible",
                                                "read_issues", "host", "restoration")}
    return {"schema_version": HOST_INPUT_SCHEMA, "units": "mm", "placement_eligible": False,
        "coordinate_policy": HOST_TRANSLATION_POLICY, "source_to_revit_xy_mm": [offset_x_mm, offset_y_mm],
        "source_report_sha256": source_report_sha256, "snapshot": deepcopy(snapshot),
        "warning": "Привязка задана пользователем, не измерена по DXF. Проверена сериализованная призма, не актуальная RVT-модель."}


def host_from_working_input(data: dict) -> RectangularHostEnvelope:
    if (data.get("schema_version") != HOST_INPUT_SCHEMA or data.get("coordinate_policy") != HOST_TRANSLATION_POLICY
            or data.get("units") != "mm" or data.get("placement_eligible") is not False):
        raise ValueError("Нужен подготовленный host с явной XY-трансляцией")
    offsets = data["source_to_revit_xy_mm"]
    if not isinstance(offsets, list) or len(offsets) != 2:
        raise ValueError("Нужны два явных сдвига XY")
    prepare_working_host(data["snapshot"], offset_x_mm=offsets[0], offset_y_mm=offsets[1],
                         source_report_sha256=data["source_report_sha256"])
    host = _snapshot_host(data["snapshot"])
    dx, dy = offsets

    def source_box(box):
        return box[0] - dx, box[1] - dy, box[2] - dx, box[3] - dy

    return replace(host, outer_mm=source_box(host.outer_mm), openings_mm=tuple(source_box(b) for b in host.openings_mm),
                   source=host.source + f"; source XY = Revit XY - ({dx}, {dy}); report SHA256={data['source_report_sha256']}")
