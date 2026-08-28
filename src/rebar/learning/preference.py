"""Прозрачная калибровка одного веса выбора точки на безопасном фронте."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FrontCandidateObservation:
    """Сопоставимые метрики одного уже проверенного кандидата."""

    id: str
    mass_kg: float
    physical_bar_count: int
    zone_count: int

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("идентификатор кандидата не может быть пустым")
        if not math.isfinite(self.mass_kg) or self.mass_kg <= 0:
            raise ValueError("масса кандидата должна быть положительной")
        if self.physical_bar_count <= 0 or self.zone_count <= 0:
            raise ValueError("число стержней и зон кандидата должно быть положительным")


@dataclass(frozen=True)
class FrontObservation:
    """Один проект: безопасный фронт и слабая plate-level метка инженера."""

    case_id: str
    title: str
    reference_mass_kg: float
    reference_bar_count: int
    reference_position_count: int
    candidates: tuple[FrontCandidateObservation, ...]
    source_kind: str = "verified_pdf_spec"

    def __post_init__(self) -> None:
        if not self.case_id.strip() or not self.title.strip():
            raise ValueError("наблюдение должно иметь непустые case_id и title")
        if not math.isfinite(self.reference_mass_kg) or self.reference_mass_kg <= 0:
            raise ValueError("эталонная масса должна быть положительной")
        if self.reference_bar_count <= 0 or self.reference_position_count <= 0:
            raise ValueError("эталонные числа стержней и позиций должны быть положительными")
        if len(self.candidates) < 2:
            raise ValueError("для калибровки нужны минимум два допустимых кандидата")
        candidate_ids = [candidate.id for candidate in self.candidates]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("идентификаторы кандидатов внутри наблюдения должны быть уникальны")


@dataclass(frozen=True)
class PreferenceModel:
    """Один вес между нормализованными массой и числом стержней."""

    mass_weight: float
    training_case_ids: tuple[str, ...]
    training_mean_regret: float
    grid_points: int


@dataclass(frozen=True)
class PreferenceFold:
    """Результат одного leave-one-project-out шага."""

    held_out_case_id: str
    training_case_ids: tuple[str, ...]
    learned_mass_weight: float
    selected_candidate_id: str
    weak_target_candidate_id: str
    selected_distance: float
    weak_target_distance: float
    regret: float
    top_1_hit: bool
    top_3_hit: bool
    selected_mass_kg: float
    selected_bar_count: int
    selected_zone_count: int
    mass_delta_pct: float
    mass_gate_met: bool
    bar_delta_pct: float
    balanced_regret: float
    minimum_mass_regret: float
    minimum_bars_regret: float


@dataclass(frozen=True)
class PreferenceCalibration:
    """Глобальная модель и честные out-of-project результаты."""

    model: PreferenceModel
    folds: tuple[PreferenceFold, ...]
    target_mass_weight: float

    @property
    def mean_regret(self) -> float:
        return sum(fold.regret for fold in self.folds) / len(self.folds)

    @property
    def top_1_accuracy(self) -> float:
        return sum(fold.top_1_hit for fold in self.folds) / len(self.folds)

    @property
    def top_3_accuracy(self) -> float:
        return sum(fold.top_3_hit for fold in self.folds) / len(self.folds)

    @property
    def mass_gate_success_rate(self) -> float:
        return sum(fold.mass_gate_met for fold in self.folds) / len(self.folds)


def _validate_weight(value: float, name: str) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} должен быть конечным числом от 0 до 1")


def reference_distance(
    observation: FrontObservation,
    candidate: FrontCandidateObservation,
    *,
    target_mass_weight: float = 0.5,
) -> float:
    """Расстояние до наблюдаемого масштаба инженерской спецификации."""

    _validate_weight(target_mass_weight, "target_mass_weight")
    mass_delta = abs(candidate.mass_kg - observation.reference_mass_kg) / (
        observation.reference_mass_kg
    )
    bars_delta = abs(candidate.physical_bar_count - observation.reference_bar_count) / (
        observation.reference_bar_count
    )
    return target_mass_weight * mass_delta + (1.0 - target_mass_weight) * bars_delta


def weak_target_candidate(
    observation: FrontObservation,
    *,
    target_mass_weight: float = 0.5,
) -> FrontCandidateObservation:
    """Найти доступную точку фронта, ближайшую к plate-level итогам инженера."""

    return min(
        observation.candidates,
        key=lambda candidate: (
            reference_distance(
                observation,
                candidate,
                target_mass_weight=target_mass_weight,
            ),
            candidate.mass_kg,
            candidate.physical_bar_count,
            candidate.id,
        ),
    )


def _normalized(value: float, values: tuple[float, ...]) -> float:
    lower = min(values)
    span = max(values) - lower
    return 0.0 if math.isclose(span, 0.0) else (value - lower) / span


def rank_candidates(
    observation: FrontObservation,
    mass_weight: float,
) -> tuple[FrontCandidateObservation, ...]:
    """Ранжировать фронт без доступа к инженерской метке проекта."""

    _validate_weight(mass_weight, "mass_weight")
    masses = tuple(candidate.mass_kg for candidate in observation.candidates)
    bars = tuple(float(candidate.physical_bar_count) for candidate in observation.candidates)

    def score(candidate: FrontCandidateObservation) -> tuple[float, float, int, str]:
        normalized_mass = _normalized(candidate.mass_kg, masses)
        normalized_bars = _normalized(float(candidate.physical_bar_count), bars)
        return (
            mass_weight * normalized_mass
            + (1.0 - mass_weight) * normalized_bars,
            candidate.mass_kg,
            candidate.physical_bar_count,
            candidate.id,
        )

    return tuple(sorted(observation.candidates, key=score))


def _mean_regret(
    observations: tuple[FrontObservation, ...],
    mass_weight: float,
    *,
    target_mass_weight: float,
) -> float:
    regrets = []
    for observation in observations:
        selected = rank_candidates(observation, mass_weight)[0]
        target = weak_target_candidate(
            observation,
            target_mass_weight=target_mass_weight,
        )
        regrets.append(
            reference_distance(
                observation,
                selected,
                target_mass_weight=target_mass_weight,
            )
            - reference_distance(
                observation,
                target,
                target_mass_weight=target_mass_weight,
            )
        )
    return sum(regrets) / len(regrets)


def fit_preference_model(
    observations: tuple[FrontObservation, ...],
    *,
    target_mass_weight: float = 0.5,
    grid_points: int = 101,
) -> PreferenceModel:
    """Подобрать один глобальный вес полным перебором прозрачной сетки."""

    if not observations:
        raise ValueError("для обучения нужно хотя бы одно наблюдение")
    _validate_weight(target_mass_weight, "target_mass_weight")
    if grid_points < 2:
        raise ValueError("grid_points должен быть не меньше 2")
    case_ids = tuple(observation.case_id for observation in observations)
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("case_id обучающих наблюдений не должны повторяться")

    choices = []
    for index in range(grid_points):
        mass_weight = index / (grid_points - 1)
        loss = _mean_regret(
            observations,
            mass_weight,
            target_mass_weight=target_mass_weight,
        )
        choices.append((loss, abs(mass_weight - 0.5), mass_weight))
    loss, _, mass_weight = min(choices)
    return PreferenceModel(
        mass_weight=mass_weight,
        training_case_ids=tuple(sorted(case_ids)),
        training_mean_regret=loss,
        grid_points=grid_points,
    )


def _candidate_regret(
    observation: FrontObservation,
    candidate: FrontCandidateObservation,
    *,
    target: FrontCandidateObservation,
    target_mass_weight: float,
) -> float:
    return reference_distance(
        observation,
        candidate,
        target_mass_weight=target_mass_weight,
    ) - reference_distance(
        observation,
        target,
        target_mass_weight=target_mass_weight,
    )


def leave_one_project_out(
    observations: tuple[FrontObservation, ...],
    *,
    target_mass_weight: float = 0.5,
    grid_points: int = 101,
) -> tuple[PreferenceFold, ...]:
    """Оценить селектор, ни разу не обучаясь на отложенной плите."""

    if len(observations) < 3:
        raise ValueError("для leave-one-project-out нужны минимум три проекта")
    folds = []
    for held_out_index, held_out in enumerate(observations):
        training = observations[:held_out_index] + observations[held_out_index + 1 :]
        model = fit_preference_model(
            training,
            target_mass_weight=target_mass_weight,
            grid_points=grid_points,
        )
        ranking = rank_candidates(held_out, model.mass_weight)
        selected = ranking[0]
        target = weak_target_candidate(
            held_out,
            target_mass_weight=target_mass_weight,
        )
        target_distance = reference_distance(
            held_out,
            target,
            target_mass_weight=target_mass_weight,
        )
        selected_distance = reference_distance(
            held_out,
            selected,
            target_mass_weight=target_mass_weight,
        )
        balanced = rank_candidates(held_out, 0.5)[0]
        minimum_mass = min(
            held_out.candidates,
            key=lambda candidate: (candidate.mass_kg, candidate.id),
        )
        minimum_bars = min(
            held_out.candidates,
            key=lambda candidate: (
                candidate.physical_bar_count,
                candidate.mass_kg,
                candidate.id,
            ),
        )
        folds.append(
            PreferenceFold(
                held_out_case_id=held_out.case_id,
                training_case_ids=model.training_case_ids,
                learned_mass_weight=model.mass_weight,
                selected_candidate_id=selected.id,
                weak_target_candidate_id=target.id,
                selected_distance=selected_distance,
                weak_target_distance=target_distance,
                regret=selected_distance - target_distance,
                top_1_hit=selected.id == target.id,
                top_3_hit=target.id in {candidate.id for candidate in ranking[:3]},
                selected_mass_kg=selected.mass_kg,
                selected_bar_count=selected.physical_bar_count,
                selected_zone_count=selected.zone_count,
                mass_delta_pct=(
                    selected.mass_kg - held_out.reference_mass_kg
                )
                / held_out.reference_mass_kg
                * 100.0,
                mass_gate_met=(
                    selected.mass_kg - held_out.reference_mass_kg
                )
                / held_out.reference_mass_kg
                * 100.0
                <= 15.0,
                bar_delta_pct=(
                    selected.physical_bar_count - held_out.reference_bar_count
                )
                / held_out.reference_bar_count
                * 100.0,
                balanced_regret=_candidate_regret(
                    held_out,
                    balanced,
                    target=target,
                    target_mass_weight=target_mass_weight,
                ),
                minimum_mass_regret=_candidate_regret(
                    held_out,
                    minimum_mass,
                    target=target,
                    target_mass_weight=target_mass_weight,
                ),
                minimum_bars_regret=_candidate_regret(
                    held_out,
                    minimum_bars,
                    target=target,
                    target_mass_weight=target_mass_weight,
                ),
            )
        )
    return tuple(folds)


def calibrate_preference(
    observations: tuple[FrontObservation, ...],
    *,
    target_mass_weight: float = 0.5,
    grid_points: int = 101,
) -> PreferenceCalibration:
    """Обучить финальный вес и отдельно посчитать LOPO без утечки проекта."""

    folds = leave_one_project_out(
        observations,
        target_mass_weight=target_mass_weight,
        grid_points=grid_points,
    )
    model = fit_preference_model(
        observations,
        target_mass_weight=target_mass_weight,
        grid_points=grid_points,
    )
    return PreferenceCalibration(
        model=model,
        folds=folds,
        target_mass_weight=target_mass_weight,
    )


def load_benchmark_observation(path: Path) -> FrontObservation:
    """Прочитать один JSON benchmark, оставив только hard-valid кандидатов."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    reference = payload.get("reference")
    if not isinstance(reference, dict):
        raise ValueError(f"benchmark {path} не содержит инженерскую reference-метку")
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ValueError(f"benchmark {path} не содержит список candidates")

    unique: dict[tuple[float, int, int], FrontCandidateObservation] = {}
    for index, raw in enumerate(raw_candidates):
        if not isinstance(raw, dict):
            continue
        if raw.get("valid") is not True or raw.get("under_reinforced_cell_count") != 0:
            continue
        mass_kg = float(raw["total_mass_kg"])
        bar_count = int(raw["physical_bar_count"])
        zone_count = int(raw["zone_count"])
        key = (round(mass_kg, 6), bar_count, zone_count)
        run_id = str(raw.get("run_id", "run"))
        candidate_id = str(raw.get("candidate_id", index))
        unique.setdefault(
            key,
            FrontCandidateObservation(
                id=f"{run_id}:{candidate_id}",
                mass_kg=mass_kg,
                physical_bar_count=bar_count,
                zone_count=zone_count,
            ),
        )
    candidates = tuple(
        sorted(
            unique.values(),
            key=lambda candidate: (
                candidate.mass_kg,
                candidate.physical_bar_count,
                candidate.zone_count,
                candidate.id,
            ),
        )
    )
    return FrontObservation(
        case_id=str(reference["id"]),
        title=str(reference["title"]),
        reference_mass_kg=float(reference["mass_kg"]),
        reference_bar_count=int(reference["physical_bar_count"]),
        reference_position_count=int(reference["position_count"]),
        candidates=candidates,
        source_kind=str(reference.get("source_kind", "strict_golden")),
    )
