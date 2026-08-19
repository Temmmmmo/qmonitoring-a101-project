"""Локальные отчёты, не влияющие на алгоритмическое ядро."""

from .comparison import generate_comparison_report
from .golden import generate_golden_report

__all__ = ["generate_comparison_report", "generate_golden_report"]
