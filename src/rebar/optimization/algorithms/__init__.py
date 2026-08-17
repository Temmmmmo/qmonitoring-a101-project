"""Взаимозаменяемые реализации алгоритмов раскладки."""

from .base import LayoutOptimizer
from .bbox import StrongestBBoxOptimizer
from .bsp import BspOptimizer

__all__ = ["BspOptimizer", "LayoutOptimizer", "StrongestBBoxOptimizer"]
