"""Стабильные входные и выходные контракты оптимизации."""

from .front import (
    CandidateRejection,
    ComplexityAxis,
    ConstructabilityMetrics,
    DirectionCandidate,
    DirectionParetoFront,
    PlateCandidate,
    PlateParetoFront,
)
from .placement import (
    AxisPlacement,
    CompositeLayoutZone,
    CompositeZoneEvaluation,
    PatternedRebarSet,
    PeriodicAxisPattern,
    RecipePlacement,
    UniformBarRun,
)
from .problem import (
    BBox,
    DemandCell,
    DemandLevel,
    DemandMap,
    LayoutConstraints,
    LayoutProblem,
)
from .plate import (
    PLATE_DIRECTIONS,
    PlateDirectionSolution,
    PlateMetrics,
    PlateProblem,
    PlateSolution,
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
    "AxisPlacement",
    "CompositeLayoutZone",
    "CompositeZoneEvaluation",
    "PatternedRebarSet",
    "PeriodicAxisPattern",
    "RecipePlacement",
    "UniformBarRun",
    "AlgorithmRequest",
    "BBox",
    "CandidateRejection",
    "ComplexityAxis",
    "ConstructabilityMetrics",
    "DemandCell",
    "DemandLevel",
    "DemandMap",
    "DirectionCandidate",
    "DirectionParetoFront",
    "LayoutConstraints",
    "LayoutEvaluation",
    "LayoutMetrics",
    "LayoutProblem",
    "LayoutSolution",
    "LayoutZone",
    "ObjectiveWeights",
    "PLATE_DIRECTIONS",
    "PlateCandidate",
    "PlateDirectionSolution",
    "PlateMetrics",
    "PlateParetoFront",
    "PlateProblem",
    "PlateSolution",
    "SolutionStatus",
]
