"""Публичный API оптимизации; внутренняя структура скрыта за стабильными импортами."""

from .adapters import MissingRebarSpecificationError, build_demand_map, build_layout_problem
from .algorithms import (
    AgglomerativeOptimizer,
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
from .mappings import (
    PLATE_ZERO_D12,
    RebarBandMapping,
    RebarMapping,
    RebarMappingError,
    apply_rebar_mapping,
)
from .registry import OptimizerRegistry, built_in_optimizer_registry
from .services import build_zone, demanded_cells, evaluate_layout

__all__ = [
    "PLATE_ZERO_D12",
    "AgglomerativeOptimizer",
    "AlgorithmRequest",
    "BspOptimizer",
    "DemandCell",
    "DemandLevel",
    "DemandMap",
    "GreedyStripOptimizer",
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
    "RebarBandMapping",
    "RebarMapping",
    "RebarMappingError",
    "SolutionStatus",
    "StrongestBBoxOptimizer",
    "apply_rebar_mapping",
    "build_demand_map",
    "build_layout_problem",
    "build_zone",
    "built_in_optimizer_registry",
    "demanded_cells",
    "evaluate_layout",
]
