"""Стабильные входные и выходные контракты оптимизации."""

from .problem import (
    BBox,
    DemandCell,
    DemandLevel,
    DemandMap,
    LayoutConstraints,
    LayoutProblem,
)
from .result import (
    AlgorithmRequest,
    LayoutEvaluation,
    LayoutMetrics,
    LayoutSolution,
    LayoutZone,
    ObjectiveWeights,
    SolutionStatus,
)

__all__ = [
    "AlgorithmRequest",
    "BBox",
    "DemandCell",
    "DemandLevel",
    "DemandMap",
    "LayoutConstraints",
    "LayoutEvaluation",
    "LayoutMetrics",
    "LayoutProblem",
    "LayoutSolution",
    "LayoutZone",
    "ObjectiveWeights",
    "SolutionStatus",
]
