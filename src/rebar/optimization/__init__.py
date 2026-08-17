"""Публичный API оптимизации; внутренняя структура скрыта за стабильными импортами."""

from .adapters import MissingRebarSpecificationError, build_demand_map, build_layout_problem
from .algorithms import (
    BspOptimizer,
    GreedyStripOptimizer,
    LayoutOptimizer,
    PriorityGreedyOptimizer,
    StrongestBBoxOptimizer,
)
from .contracts import (
    AlgorithmRequest,
    DemandCell,
    DemandLevel,
    DemandMap,
    LayoutConstraints,
    LayoutEvaluation,
    LayoutMetrics,
    LayoutProblem,
    LayoutSolution,
    LayoutZone,
    ObjectiveWeights,
    SolutionStatus,
)
from .registry import OptimizerRegistry, built_in_optimizer_registry
from .services import build_zone, demanded_cells, evaluate_layout

__all__ = [
    "AlgorithmRequest",
    "BspOptimizer",
    "DemandCell",
    "DemandLevel",
    "DemandMap",
    "LayoutConstraints",
    "LayoutEvaluation",
    "LayoutMetrics",
    "LayoutOptimizer",
    "LayoutProblem",
    "LayoutSolution",
    "LayoutZone",
    "MissingRebarSpecificationError",
    "ObjectiveWeights",
    "OptimizerRegistry",
    "PriorityGreedyOptimizer",
    "SolutionStatus",
    "StrongestBBoxOptimizer",
    "GreedyStripOptimizer",
    "build_zone",
    "built_in_optimizer_registry",
    "build_demand_map",
    "build_layout_problem",
    "demanded_cells",
    "evaluate_layout",
]
