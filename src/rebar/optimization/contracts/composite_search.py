"""Отдельный контракт поиска ВСЕХ компонентов; старый LayoutProblem не меняется."""
from __future__ import annotations

from dataclasses import dataclass

from .composite_coverage import CompositeCoverageEvaluation
from .host import RectangularHostEnvelope
from .placement import CompositeLayoutZone, RecipePlacement
from .problem import DemandMap, LayoutConstraints


@dataclass(frozen=True)
class CompositeSearchProblem:
    demand: DemandMap
    placements: tuple[tuple[int, RecipePlacement], ...]
    constraints: LayoutConstraints
    policy_id: str
    host_envelope: RectangularHostEnvelope | None = None
    boundary_mode: str = "strict"


@dataclass(frozen=True)
class CompositeSearchPoint:
    zones: tuple[CompositeLayoutZone, ...]
    coverage: CompositeCoverageEvaluation


@dataclass(frozen=True)
class CompositeSearchResult:
    points: tuple[CompositeSearchPoint, ...]
    selected_index: int | None
    telemetry: dict
    placement_eligible: bool = False
