"""Локальные отчёты, не влияющие на алгоритмическое ядро."""

from .comparison import generate_comparison_report
from .engineer_dataset import generate_engineer_dataset_report
from .genetic_benchmark import generate_genetic_benchmark_report
from .golden import generate_golden_report
from .preference_calibration import generate_preference_calibration_report

__all__ = [
    "generate_comparison_report",
    "generate_engineer_dataset_report",
    "generate_genetic_benchmark_report",
    "generate_golden_report",
    "generate_preference_calibration_report",
]
