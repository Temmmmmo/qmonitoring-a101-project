"""Проверка явно заданных составных зон, не генератор и не команда Revit apply."""
from __future__ import annotations

import json
import math
from dataclasses import replace
from typing import Any

from rebar.models import Mosaic
from rebar.optimization.adapters.mosaic import build_demand_map
from rebar.optimization.contracts.composite_coverage import COMPOSITE_COVERAGE_POLICY
from rebar.optimization.contracts.problem import LayoutConstraints
from rebar.optimization.services.axis_patterns import a101_247_slab_recipe_placement
from rebar.optimization.services.composite_coverage import MAX_ZONES, evaluate_composite_coverage
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.reporting.serialization import to_jsonable

from .composite_revit_export import build_composite_zone_revit_export

LAYOUT_REVIEW_INPUT_SCHEMA = "composite-layout-review-input/v1"
MAX_INPUT_BYTES = 256 * 1024
MAX_TOTAL_BARS = 100000


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("повторяющийся ключ JSON: " + key)
        result[key] = value
    return result


def _reject(value):
    raise ValueError("нечисловая константа JSON: " + value)


def load_review_input(raw: bytes) -> dict[str, Any]:
    """Ограниченный JSON без повторных ключей/NaN; валидируется до детализации."""
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ValueError("вход JSON пуст или превышает 256 KiB")
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique, parse_constant=_reject)
    except (UnicodeError, RecursionError) as error:
        raise ValueError("не удалось прочитать UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError("JSON должен содержать объект")
    return value


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name}: нужно конечное число, не bool/строка/null")
    return float(value)


def prepare_composite_review(mosaic: Mosaic, data: dict[str, Any]):
    """Общая строгая граница конфигурации для review и составного поиска."""
    expected = {"schema_version", "mode", "units", "policy_id", "direction", "min_width_cells",
                "phase_source", "background_origin_mm", "additional_origins_by_level", "zones"}
    if not isinstance(data, dict) or set(data) != expected:
        raise ValueError("неверный набор полей входа composite-layout-review-input/v1")
    if (data["schema_version"] != LAYOUT_REVIEW_INPUT_SCHEMA or data["mode"] != "research-only"
            or data["units"] != "mm" or data["policy_id"] != COMPOSITE_COVERAGE_POLICY):
        raise ValueError("нужны mm, research-only и поддерживаемый профиль; это не Revit apply JSON")
    if data["direction"] != to_jsonable(mosaic.direction):
        raise ValueError("направление входа не совпадает с DXF")
    minimum = data["min_width_cells"]
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum not in (2, 3):
        raise ValueError("min_width_cells должен быть 2 или 3")
    if not isinstance(data["phase_source"], str) or not 1 <= len(data["phase_source"].strip()) <= 1000:
        raise ValueError("нужен явный источник экспериментальных фаз длиной 1..1000 символов")
    background_origin = _number(data["background_origin_mm"], "background_origin_mm")
    if abs(background_origin) > 1e9:
        raise ValueError("слишком большое начало сетки")
    origins = data["additional_origins_by_level"]
    if not isinstance(origins, dict):
        raise ValueError("additional_origins_by_level должен быть объектом")
    rows = data["zones"]
    if not isinstance(rows, list) or len(rows) > MAX_ZONES:
        raise ValueError(f"zones должен быть списком до {MAX_ZONES} зон")
    demand = build_demand_map(mosaic)
    constraints = LayoutConstraints(min_width_cells=minimum)
    placements = {}
    for key, values in origins.items():
        if not isinstance(key, str) or not key.isascii() or not key.isdecimal() or str(int(key)) != key:
            raise ValueError("ключи фаз — десятичные индексы уровней")
        level = demand.level(int(key))
        if level.recipe is None or not level.recipe.additions:
            raise ValueError("фазы добавок должны относиться к уровню с явными добавками")
        if not isinstance(values, list) or len(values) != len(level.recipe.additions):
            raise ValueError(f"уровень {key}: требуется начало КАЖДОЙ добавки")
        numbers = [_number(value, "начало добавки") for value in values]
        if any(abs(value) > 1e9 for value in numbers):
            raise ValueError("слишком большое начало добавки")
        placement = a101_247_slab_recipe_placement(level.recipe, background_origin_mm=background_origin)
        placements[int(key)] = replace(placement, source=data["phase_source"], additions=tuple(
            replace(part, origin_mm=number) for part, number in zip(placement.additions, numbers)))
    return demand, constraints, placements


def build_review_zones(mosaic: Mosaic, data: dict[str, Any]):
    """Общая десериализация зон; метрики пересчитываются, не читаются из JSON."""
    demand, constraints, placements = prepare_composite_review(mosaic, data)
    zones = []
    for row in data["zones"]:
        if not isinstance(row, dict) or set(row) != {"id", "level_index", "demand_bbox_mm"}:
            raise ValueError("зона должна содержать id, level_index и demand_bbox_mm")
        index, name, bbox = row["level_index"], row["id"], row["demand_bbox_mm"]
        if isinstance(index, bool) or not isinstance(index, int) or index not in placements:
            raise ValueError("уровень зоны неизвестен или не заданы все его фазы")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
            raise ValueError("нужен идентификатор зоны длиной 1..200 символов")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("demand_bbox_mm должен содержать четыре числа")
        box = tuple(_number(v, "bbox") for v in bbox)
        if any(abs(v) > 1e9 for v in box):
            raise ValueError("координаты превышают лимит research-профиля")
        zones.append(build_composite_zone(demand, box, index, name, placements[index], constraints=constraints))
        if sum(component.bar_count for zone in zones for component in zone.components) > MAX_TOTAL_BARS:
            raise ValueError("превышен суммарный лимит 100000 стержней; ведомость не обрезана")
    return demand, constraints, tuple(zones)


def review_composite_layout(mosaic: Mosaic, data: dict[str, Any]) -> dict[str, Any]:
    """Детализировать заданные bbox; research-only, не команда Revit apply."""
    demand, constraints, zones = build_review_zones(mosaic, data)
    evaluation = evaluate_composite_coverage(demand, zones, policy_id=data["policy_id"], constraints=constraints)
    return {
        "schema_version": "composite-layout-review/v1", "mode": "research-only", "units": "mm",
        "status": "research_checks_passed" if evaluation.status == "pass" else "needs_revision",
        "placement_eligible": False, "optimizer_executed": False,
        "direction": to_jsonable(demand.direction), "source_cell_count": len(demand.cells),
        "source_bbox_mm": list(demand.bbox), "phase_source": data["phase_source"],
        "phase_approval": "not_checked", "preprocessing": "not_applied_original_demand_retained",
        "input": data, "coverage": to_jsonable(evaluation),
        "zone_drafts": [build_composite_zone_revit_export(demand, zone, constraints=constraints) for zone in zones],
        "remaining_check_ids": list(evaluation.remaining_check_ids),
        "warning": "Проверены заданные зоны по research-модели. Это не поиск оптимума, "
                   "не проверка несущей способности, не привязка к Revit и не разрешение размещения.",
    }
