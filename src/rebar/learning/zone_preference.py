"""Исследовательская ridge-регрессия для выбора из готового проверенного фронта.

Эталон инженера нужен только для обучения и оценки. Прогноз принимает один front,
не знает PDF и никогда не создаёт новых зон или стержней.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math

import numpy as np

from rebar.optimization.services.zone_tradeoff import recommend_zone_knee


@dataclass(frozen=True)
class ZoneObservation:
    case_id: str
    report_id: str
    report_sha256: str
    front: tuple[tuple[float, int, int], ...]  # mass kg, physical bars, zones
    reference_mass_kg: float
    reference_bar_count: int
    reference_source: str = ""
    input_fingerprint: str = ""


def _positive(value, *, name: str, integer: bool = False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} должен быть конечным положительным числом")
    if integer and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValueError(f"{name} должен быть положительным целым числом")
    return value


def _nonnegative(value, *, name: str, integer: bool = False):
    if value == 0 and not isinstance(value, bool) and (not integer or isinstance(value, int)):
        return value
    return _positive(value, name=name, integer=integer)


def validated_front(report: dict) -> tuple[tuple[float, int, int], ...]:
    """Проверить индексы и hard coverage каждой выбранной точки всех направлений."""
    if report.get("schema_version") != "composite-plate-analysis/v1" or report.get("search_mode") != "zone-merge":
        raise ValueError("нужен полный отчёт отдельного zone-merge поиска")
    if report.get("direction_count") != 4 or len(report.get("directions", ())) != 4:
        raise ValueError("нужны четыре направления")
    expected_directions = {("bottom", "X"), ("bottom", "Y"), ("top", "X"), ("top", "Y")}
    try:
        actual_directions = [(d["direction"]["layer"], d["direction"]["axis"]) for d in report["directions"]]
    except (KeyError, TypeError) as error:
        raise ValueError("неверные направления отчёта") from error
    if len(set(actual_directions)) != 4 or set(actual_directions) != expected_directions:
        raise ValueError("нужны уникальные bottom/top × X/Y направления")
    if report.get("status") != "full_coverage_candidates_found" or not report.get("front"):
        raise ValueError("отчёт не содержит полного фронта")
    if report.get("placement_eligible") is not False or report.get("source_demand_preserved") is not True:
        raise ValueError("отчёт потерял статус исследовательской выдачи или исходный спрос")
    result = []
    for point in report["front"]:
        choices = point.get("direction_candidate_indexes")
        if not isinstance(choices, list) or len(choices) != 4:
            raise ValueError("точка не ссылается на четыре направления")
        parts = []
        for direction, index in zip(report["directions"], choices):
            candidates = direction.get("candidates", ())
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(candidates):
                raise ValueError("неверный индекс кандидата направления")
            candidate = candidates[index]
            coverage = candidate.get("coverage", {})
            if (coverage.get("status") != "pass" or coverage.get("uncovered_cell_count") != 0
                    or coverage.get("geometry_and_patterns_valid") is not True
                    or coverage.get("coverage_passed") is not True
                    or any(zone.get("geometry_and_pattern_valid") is not True for zone in coverage.get("zones", ()))):
                raise ValueError("точка содержит недоармирование или непроверенный coverage")
            metrics = candidate["metrics"]
            demanded = coverage.get("demanded_cell_count")
            if isinstance(demanded, bool) or not isinstance(demanded, int) or demanded < 0:
                raise ValueError("неверное число КЭ в coverage")
            direction_mass = float(_nonnegative(metrics["additional_mass_kg"], name="масса направления"))
            direction_bars = _nonnegative(metrics["physical_bar_count"], name="стержни направления", integer=True)
            direction_zones = _nonnegative(metrics["zone_count"], name="зоны направления", integer=True)
            coverage_mass = float(_nonnegative(coverage["additional_mass_kg"], name="масса coverage"))
            if (demanded == 0) != (direction_mass == direction_bars == direction_zones == 0):
                raise ValueError("нулевой спрос требует нулевые зоны, массу и стержни")
            if (abs(direction_mass - coverage_mass) > max(1e-5, direction_mass * 1e-8)
                    or direction_bars != coverage.get("physical_bar_count")
                    or direction_zones != coverage.get("zone_count")
                    or len(coverage.get("zones", ())) != direction_zones
                    or coverage.get("covered_cell_count") != coverage.get("demanded_cell_count")):
                raise ValueError("метрики направления не совпадают с независимым coverage")
            parts.append((direction_mass, direction_bars, direction_zones))
        mass = float(_positive(point["additional_mass_kg"], name="масса точки"))
        bars = _positive(point["physical_bar_count"], name="число стержней", integer=True)
        zones = _positive(point["zone_count"], name="число зон", integer=True)
        if (abs(mass - math.fsum(p[0] for p in parts)) > max(1e-5, mass * 1e-8)
                or bars != sum(p[1] for p in parts)
                or zones != sum(p[2] for p in parts)):
            raise ValueError("метрики точки не совпадают с четырьмя направлениями")
        result.append((mass, bars, zones))
    if len({(m, b, z) for m, b, z in result}) != len(result):
        raise ValueError("фронт содержит повторяющиеся точки")
    return tuple(result)


def input_fingerprint(report: dict) -> str:
    """Дайджест DXF и всех явных шкал, привязанный к каждому направлению."""
    roles = []
    for direction in report.get("directions", ()):
        identity = direction.get("direction", {})
        hashes = direction.get("source", {}).get("sha256", {})
        if not isinstance(hashes, dict) or "dxf" not in hashes or not set(hashes) <= {"dxf", "shk", "png"}:
            raise ValueError("нет проверенных SHA256 исходных DXF/шкал")
        if len(set(hashes) & {"shk", "png"}) != 1 or any(
                not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
                for value in hashes.values()):
            raise ValueError("неверные SHA256 исходных DXF/шкал")
        roles.append((identity.get("layer"), identity.get("axis"), sorted(hashes.items())))
    if len(roles) != 4 or len({(row[0], row[1]) for row in roles}) != 4:
        raise ValueError("нужны четыре уникальных направления для отпечатка")
    return hashlib.sha256(json.dumps(sorted(roles), separators=(",", ":")).encode()).hexdigest()


def weak_distance(point: tuple[float, int, int], reference_mass_kg: float, reference_bar_count: int) -> float:
    _positive(reference_mass_kg, name="эталонная масса")
    _positive(reference_bar_count, name="эталонное число стержней", integer=True)
    return 0.5 * abs(point[0] - reference_mass_kg) / reference_mass_kg + 0.5 * abs(point[1] - reference_bar_count) / reference_bar_count


def features(front: tuple[tuple[float, int, int], ...]) -> np.ndarray:
    if not front:
        raise ValueError("пустой фронт")
    masses, bars = np.array([(p[0], p[1]) for p in front], dtype=float).T
    if not np.all(np.isfinite(masses)) or not np.all(np.isfinite(bars)) or min(masses) <= 0 or min(bars) <= 0:
        raise ValueError("для регрессии нужны положительные масса и число стержней")
    return np.column_stack((np.ones(len(front)), masses / min(masses) - 1, bars / min(bars) - 1))


def fit(observations: tuple[ZoneObservation, ...], *, ridge_alpha: float = 0.01) -> dict:
    """Ridge с фиксированным alpha; каждый case имеет общий вес 1/N_cases."""
    if not observations or not math.isfinite(ridge_alpha) or ridge_alpha <= 0:
        raise ValueError("нужны наблюдения и положительный фиксированный ridge_alpha")
    cases = sorted({o.case_id for o in observations})
    if not all(cases) or any(len(o.front) < 2 for o in observations):
        raise ValueError("каждый case должен иметь имя и фронт минимум из двух точек")
    x_parts, y_parts, weights = [], [], []
    for obs in observations:
        x = features(obs.front)
        y = np.array([weak_distance(p, obs.reference_mass_kg, obs.reference_bar_count) for p in obs.front])
        reports_in_case = sum(o.case_id == obs.case_id for o in observations)
        x_parts.append(x)
        y_parts.append(y)
        weights.extend([1 / (len(cases) * reports_in_case * len(obs.front))] * len(obs.front))
    x, y, w = np.vstack(x_parts), np.concatenate(y_parts), np.array(weights)
    penalty = np.diag([0.0, ridge_alpha, ridge_alpha])  # intercept is not penalized
    beta = np.linalg.solve(x.T @ (w[:, None] * x) + penalty, x.T @ (w * y))
    return {"schema_version": "zone-preference-ridge/v1", "feature_names": ["intercept", "mass_over_front_min_minus_one", "bars_over_front_min_minus_one"],
            "coefficients": beta.tolist(), "ridge_alpha": ridge_alpha, "training_case_ids": cases,
            "training_report_sha256": sorted({o.report_sha256 for o in observations}),
            "training_case_count": len(cases), "training_report_count": len(observations),
            "warning": "Слабая цель по массе/стержням; инженерные зоны и Точка 3 не размечены. Не применять автоматически."}


def rank(front: tuple[tuple[float, int, int], ...], model: dict) -> tuple[int, ...]:
    """Inference без эталона: только индексы уже переданных проверенных точек."""
    if (model.get("schema_version") != "zone-preference-ridge/v1"
            or model.get("feature_names") != ["intercept", "mass_over_front_min_minus_one", "bars_over_front_min_minus_one"]
            or len(model.get("coefficients", ())) != 3):
        raise ValueError("неизвестная модель предпочтений")
    beta = np.asarray(model["coefficients"], dtype=float)
    if not np.all(np.isfinite(beta)):
        raise ValueError("коэффициенты модели должны быть конечными")
    scores = features(front) @ beta
    return tuple(sorted(range(len(front)), key=lambda i: (scores[i], front[i][0], front[i][1], i)))


def predict_report(report: dict, model: dict) -> dict:
    """Без эталона проверить отчёт и выбрать существующие top-1/top-3 индексы."""
    front = validated_front(report)
    ordered = rank(front, model)
    return {"selected_index": ordered[0], "top3_indexes": list(ordered[:3]),
            "candidate_count": len(front), "selection_scope": "hard_checked_input_front_only"}


def build_bundle(observations: tuple[ZoneObservation, ...], calibration: dict) -> dict:
    """Только малые параметры и provenance; никаких отчётов/референсных чисел."""
    cases = sorted({o.case_id for o in observations})
    fingerprints = {}
    for obs in observations:
        if not obs.input_fingerprint:
            raise ValueError("для bundle нужен отпечаток входных DXF/шкал")
        previous = fingerprints.setdefault(obs.input_fingerprint, obs.case_id)
        if previous != obs.case_id:
            raise ValueError("один исходный комплект привязан к разным инженерным случаям")
    held_out = {case: fit(tuple(o for o in observations if o.case_id != case)) for case in cases}
    return {"schema_version": "zone-preference-bundle/v1",
            "model_id": _bundle_model_id(calibration["model"], calibration.get("manifest_sha256")),
            "full_model": calibration["model"], "held_out_models": held_out,
            "input_case_fingerprints": fingerprints,
            "manifest_sha256": calibration.get("manifest_sha256"),
            "validation": {"independent_cases": calibration["independent_case_count"],
                           "mean_regret": calibration["mean_regret"],
                           "knee_regret": calibration["mean_geometric_knee_regret"],
                           "top1_rate": calibration["top1_rate"], "top3_rate": calibration["top3_rate"]},
            "warning": "Малая выборка и слабая цель по массе/стержням. Не инженерная Точка 3; выбор требует проверки."}


def _bundle_model_id(full_model: dict, manifest_sha256: str | None) -> str:
    digest_source = {"model": full_model, "manifest_sha256": manifest_sha256}
    digest = hashlib.sha256(json.dumps(digest_source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    return f"ridge-mass-bars-alpha-0.01/v1+{digest}"


def build_engineering_preference(report: dict, bundle: dict | None) -> dict:
    """Подсказка только по hard-valid фронту; любой сбой прозрачно делает её недоступной."""
    if bundle is None:
        return {"status": "unavailable", "reason": "модель инженерских предпочтений не поставлена"}
    if not report.get("front"):
        return {"status": "unavailable", "reason": "нет полного общеплитного фронта"}
    if len(report["front"]) < 2:
        return {"status": "unavailable", "reason": "одноточечный фронт не содержит компромисса для рекомендации"}
    try:
        if bundle.get("schema_version") != "zone-preference-bundle/v1":
            raise ValueError("неизвестный формат модели")
        if bundle.get("model_id") != _bundle_model_id(bundle["full_model"], bundle.get("manifest_sha256")):
            raise ValueError("идентификатор модели не совпадает с параметрами")
        fingerprint = input_fingerprint(report)
        case = bundle["input_case_fingerprints"].get(fingerprint)
        model = bundle["held_out_models"][case] if case is not None else bundle["full_model"]
        if case is not None and case in model.get("training_case_ids", ()):
            raise ValueError("модель известного инженерного случая обучена на этом же случае")
        prediction = predict_report(report, model)
        return {"status": "available", "recommended_index": prediction["selected_index"],
                "top_indexes": prediction["top3_indexes"], "model_id": bundle["model_id"],
                "training_case_ids": model["training_case_ids"], "validation": bundle["validation"],
                "prediction_scope": "held_out_engineering_case" if case is not None else "new_input",
                "warning": bundle["warning"]}
    except (ValueError, KeyError, TypeError, IndexError) as error:
        return {"status": "unavailable", "reason": f"модель/фронт не прошли проверку: {error}"}


def evaluate(observations: tuple[ZoneObservation, ...], *, ridge_alpha: float = 0.01) -> dict:
    """LOPO: все отчёты одного инженерного case удерживаются вместе."""
    cases = sorted({o.case_id for o in observations})
    if len(cases) < 2:
        raise ValueError("для LOPO нужны минимум два независимых инженерных case")
    folds = []
    for case in cases:
        train = tuple(o for o in observations if o.case_id != case)
        model = fit(train, ridge_alpha=ridge_alpha)
        for obs in (o for o in observations if o.case_id == case):
            front = obs.front
            distances = [weak_distance(p, obs.reference_mass_kg, obs.reference_bar_count) for p in front]
            target = min(range(len(front)), key=lambda i: (distances[i], front[i][0], i))
            ranked = rank(front, model)
            min_mass = min(range(len(front)), key=lambda i: (front[i][0], i))
            knee = recommend_zone_knee([(p[2], p[0]) for p in front])["index"]
            folds.append({"held_out_case_id": case, "report_id": obs.report_id,
                          "training_case_ids": model["training_case_ids"], "weak_target_index": target,
                          "selected_index": ranked[0], "top3_indexes": list(ranked[:3]),
                          "top1_hit": ranked[0] == target, "top3_hit": target in ranked[:3],
                          "regret": distances[ranked[0]] - distances[target],
                          "minimum_mass_regret": distances[min_mass] - distances[target],
                          "geometric_knee_regret": distances[knee] - distances[target],
                          "selected_distance": distances[ranked[0]], "weak_target_distance": distances[target]})
    def case_mean(key):
        return sum(sum(f[key] for f in folds if f["held_out_case_id"] == case)
                   / sum(f["held_out_case_id"] == case for f in folds) for case in cases) / len(cases)

    return {"schema_version": "zone-preference-calibration/v1", "model": fit(observations, ridge_alpha=ridge_alpha),
            "folds": folds, "independent_case_count": len(cases), "report_count": len(observations),
            "mean_regret": case_mean("regret"),
            "mean_minimum_mass_regret": case_mean("minimum_mass_regret"),
            "mean_geometric_knee_regret": case_mean("geometric_knee_regret"),
            "top1_rate": case_mean("top1_hit"),
            "top3_rate": case_mean("top3_hit"),
            "warning": "Мало независимых слабых меток; регрессия не доказывает инженерную Точку 3."}
