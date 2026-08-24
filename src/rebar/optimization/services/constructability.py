"""Общие proxy-метрики сложности комплектации и монтажа."""

from __future__ import annotations

from collections.abc import Iterable

from ..contracts import ConstructabilityMetrics, LayoutSolution, PlateSolution
from ..contracts.result import LayoutZone


def _diagnostic_counts(diagnostics: Iterable[str]) -> tuple[int, int]:
    messages = tuple(diagnostics)
    return (
        sum(message.startswith("WARNING:") for message in messages),
        sum(message.startswith("ERROR:") for message in messages),
    )


def _metrics(
    zones: Iterable[tuple[str, LayoutZone]],
    diagnostics: Iterable[str],
) -> ConstructabilityMetrics:
    normalized_zones = tuple(zones)
    warning_count, error_count = _diagnostic_counts(diagnostics)
    return ConstructabilityMetrics(
        zone_count=len(normalized_zones),
        physical_bar_count=sum(zone.bar_count for _, zone in normalized_zones),
        unique_diameter_count=len(
            {zone.rebar.diameter for _, zone in normalized_zones}
        ),
        unique_step_count=len({zone.rebar.step for _, zone in normalized_zones}),
        unique_installed_length_count=len(
            {round(zone.installed_length_mm, 6) for _, zone in normalized_zones}
        ),
        unique_layout_signature_count=len(
            {
                (
                    direction,
                    zone.rebar.diameter,
                    zone.rebar.step,
                    round(zone.installed_length_mm, 6),
                )
                for direction, zone in normalized_zones
            }
        ),
        warning_count=warning_count,
        error_count=error_count,
    )


def measure_constructability(solution: LayoutSolution) -> ConstructabilityMetrics:
    """Посчитать вектор сложности одного направления без свёртки в score."""

    return _metrics(
        (("direction", zone) for zone in solution.zones),
        solution.diagnostics,
    )


def measure_plate_constructability(solution: PlateSolution) -> ConstructabilityMetrics:
    """Посчитать общеплитный вектор с различением четырёх направлений."""

    return _metrics(
        (
            (str(item.direction), zone)
            for item in solution.direction_solutions
            for zone in item.solution.zones
        ),
        solution.diagnostics,
    )
