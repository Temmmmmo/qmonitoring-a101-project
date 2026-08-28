"""Прикладные сценарии поверх parser и алгоритмического ядра."""

from .analyze_direction import (
    DEFAULT_ALGORITHMS,
    DirectionAnalysis,
    analyze_direction,
    available_cutting_profile_ids,
    available_mapping_ids,
)
from .analyze_plate import (
    PlateAnalysis,
    PlateDirectionSource,
    analyze_plate,
)
from .demo import (
    IRREGULAR_PLATE_DEMO,
    DemoCase,
    available_demo_cases,
    get_demo_case,
    write_demo_dxf,
)
from .gate_assessment import (
    GateAssessment,
    GateDeviation,
    assess_layout_gates,
    assess_plate_gates,
)
from .genetic_benchmark import (
    GeneticRunConfig,
    GeneticRunResult,
    run_genetic_benchmark,
)

__all__ = [
    "DEFAULT_ALGORITHMS",
    "IRREGULAR_PLATE_DEMO",
    "DemoCase",
    "DirectionAnalysis",
    "GateAssessment",
    "GateDeviation",
    "GeneticRunConfig",
    "GeneticRunResult",
    "PlateAnalysis",
    "PlateDirectionSource",
    "analyze_direction",
    "analyze_plate",
    "assess_layout_gates",
    "assess_plate_gates",
    "available_cutting_profile_ids",
    "available_demo_cases",
    "available_mapping_ids",
    "get_demo_case",
    "run_genetic_benchmark",
    "write_demo_dxf",
]
