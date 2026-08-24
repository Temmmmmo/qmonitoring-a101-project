"""Взаимозаменяемые реализации алгоритмов раскладки."""

from .agglomerative import AgglomerativeOptimizer
from .base import LayoutCandidateGenerator, LayoutOptimizer
from .bbox import StrongestBBoxOptimizer
from .bsp import BspOptimizer
from .genetic_pareto import GeneticParetoOptimizer
from .greedy import GreedyStripOptimizer
from .greedy_priority import PriorityGreedyOptimizer
from .row_run_greedy import RowRunGreedyOptimizer
from .spatial_partition_greedy import SpatialPartitionGreedyOptimizer
from .strip_profile_dp import StripProfileDpOptimizer

__all__ = [
    "AgglomerativeOptimizer",
    "BspOptimizer",
    "GeneticParetoOptimizer",
    "GreedyStripOptimizer",
    "LayoutCandidateGenerator",
    "LayoutOptimizer",
    "PriorityGreedyOptimizer",
    "RowRunGreedyOptimizer",
    "SpatialPartitionGreedyOptimizer",
    "StripProfileDpOptimizer",
    "StrongestBBoxOptimizer",
]
