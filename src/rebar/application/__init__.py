"""Прикладные сценарии поверх parser и алгоритмического ядра."""

from .analyze_direction import (
    DEFAULT_ALGORITHMS,
    DirectionAnalysis,
    analyze_direction,
    available_cutting_profile_ids,
    available_mapping_ids,
)
from .demo import (
    IRREGULAR_PLATE_DEMO,
    DemoCase,
    available_demo_cases,
    get_demo_case,
    write_demo_dxf,
)

__all__ = [
    "DEFAULT_ALGORITHMS",
    "IRREGULAR_PLATE_DEMO",
    "DemoCase",
    "DirectionAnalysis",
    "analyze_direction",
    "available_cutting_profile_ids",
    "available_demo_cases",
    "available_mapping_ids",
    "get_demo_case",
    "write_demo_dxf",
]
