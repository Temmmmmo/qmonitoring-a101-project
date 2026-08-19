"""Взаимозаменяемые реализации алгоритмов раскладки."""

from .agglomerative import AgglomerativeOptimizer
from .base import LayoutOptimizer
from .bbox import StrongestBBoxOptimizer
from .bsp import BspOptimizer
from .greedy import GreedyStripOptimizer
from .greedy_priority import PriorityGreedyOptimizer

__all__ = [
    "AgglomerativeOptimizer",
    "BspOptimizer",
    "GreedyStripOptimizer",
    "LayoutOptimizer",
    "PriorityGreedyOptimizer",
    "StrongestBBoxOptimizer",
]
