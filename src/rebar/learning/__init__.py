"""Малопараметрическое обучение выбора среди допустимых Парето-кандидатов."""

from .preference import (
    FrontCandidateObservation,
    FrontObservation,
    PreferenceCalibration,
    PreferenceFold,
    PreferenceModel,
    calibrate_preference,
    fit_preference_model,
    leave_one_project_out,
    load_benchmark_observation,
    rank_candidates,
    reference_distance,
    weak_target_candidate,
)

__all__ = [
    "FrontCandidateObservation",
    "FrontObservation",
    "PreferenceCalibration",
    "PreferenceFold",
    "PreferenceModel",
    "calibrate_preference",
    "fit_preference_model",
    "leave_one_project_out",
    "load_benchmark_observation",
    "rank_candidates",
    "reference_distance",
    "weak_target_candidate",
]
