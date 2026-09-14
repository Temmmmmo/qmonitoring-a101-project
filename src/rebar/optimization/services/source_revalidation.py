"""Audit an existing single-component layout against unchanged source demand.

This is deliberately not a repair or a new coverage policy. Only the two cached
coverage annotations may be refreshed; the old physical geometry is never replaced
by the reconstruction used to check it. The shared hard evaluator remains the
authority for coverage, geometry, mass and the current layout constraints.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import asdict, dataclass, replace

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from rebar.models import Point

from ..contracts import (
    BBox,
    DemandCell,
    LayoutEvaluation,
    LayoutProblem,
    LayoutSolution,
    LayoutZone,
    SolutionStatus,
)
from .detailing import build_zone_from_bbox, prepare_detailing
from .evaluation import evaluate_layout
from .geometry import (
    GEOMETRY_TOLERANCE_MM,
    cell_bbox,
    polygon_area,
    polygon_bbox_intersection_area,
    polygon_bboxes_union_intersection_area,
)

SOURCE_REVALIDATION_POLICY = "unchanged-layout-against-original-demand/v1"


@dataclass(frozen=True)
class ResidualPolygon:
    """One residual polygon in millimetres; rings have no repeated final vertex."""

    shell: tuple[Point, ...]
    holes: tuple[tuple[Point, ...], ...] = ()

    @property
    def bbox(self) -> BBox:
        xs, ys = zip(*self.shell)
        return min(xs), min(ys), max(xs), max(ys)


@dataclass(frozen=True)
class SourceDemandResidual:
    """An uncovered source FE, not an instruction to discard or lower its demand.

    ``uncovered_area_mm2`` uses the shared evaluator's union-area calculation and
    tolerance. Polygon rings are exact geometric locations for review/repair, not
    a second test of coverage; tiny gaps accepted by the shared tolerance are not
    reported as separate deficient cells.
    """

    cell_id: int
    required_level_index: int
    cell_bbox: BBox
    cell_polygon: tuple[Point, ...]
    cell_area_mm2: float
    covered_area_mm2: float
    uncovered_area_mm2: float
    coverage_tolerance_mm2: float
    sufficient_zone_ids: tuple[str, ...]
    residual_polygons: tuple[ResidualPolygon, ...]


@dataclass(frozen=True)
class SourceDemandRevalidation:
    """A same-geometry solution, its hard evaluation and exact deficient source IDs.

    A feasible status means valid in the existing single-component mathematical
    model. It does not approve physical placement, periodic @150 axes, host fit,
    stock cutting or engineering issue. Those checks remain separate.
    """

    solution: LayoutSolution
    evaluation: LayoutEvaluation
    uncovered_cells: tuple[SourceDemandResidual, ...]

    @property
    def uncovered_cell_ids(self) -> tuple[int, ...]:
        return tuple(cell.cell_id for cell in self.uncovered_cells)


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _source_cells(problem: LayoutProblem) -> dict[int, DemandCell]:
    """Reject missing mapping/IDs and known downgraded input, not guess the source."""

    for metadata in (problem.meta, problem.demand.meta):
        preprocessing = metadata.get("single_cell_preprocessing")
        if preprocessing is not None:
            if not isinstance(preprocessing, dict):
                raise ValueError("source demand: invalid single_cell_preprocessing metadata")
            if preprocessing.get("changed_count", 0) != 0 or preprocessing.get("changes"):
                raise ValueError("source demand is already modified by single-cell preprocessing")
    levels = problem.demand.levels
    if not levels or tuple(level.index for level in levels) != tuple(range(len(levels))):
        raise ValueError("source demand requires a complete ordered level mapping")
    for level in levels:
        if type(level.requires_extra) is not bool:
            raise ValueError(f"source level {level.index}: requires_extra is unknown")
        if level.requires_extra != (level.additional is not None):
            raise ValueError(f"source level {level.index}: reinforcement mapping is inconsistent")
        if level.additional is not None:
            for value in (level.additional.step, level.additional.diameter):
                if type(value) is not int or value <= 0:
                    raise ValueError(f"source level {level.index}: invalid reinforcement")

    cells: dict[int, DemandCell] = {}
    for cell in problem.demand.cells:
        if type(cell.id) is not int or cell.id in cells:
            raise ValueError("source demand requires unique integer cell IDs")
        if type(cell.level_index) is not int:
            raise ValueError(f"source cell {cell.id}: invalid level index")
        try:
            problem.demand.level(cell.level_index)
        except KeyError as error:
            raise ValueError(f"source cell {cell.id}: unknown level {cell.level_index}") from error
        if len(cell.poly) < 3 or any(
            len(point) != 2 or not all(_finite_number(value) for value in point)
            for point in cell.poly
        ):
            raise ValueError(f"source cell {cell.id}: invalid polygon coordinates")
        polygon = Polygon(cell.poly)
        if not polygon.is_valid or polygon_area(cell.poly) <= GEOMETRY_TOLERANCE_MM:
            raise ValueError(f"source cell {cell.id}: invalid or degenerate polygon")
        cells[cell.id] = cell
    return cells


def _check_source_ids(zone: LayoutZone, cells: dict[int, DemandCell]) -> None:
    # Seed IDs refer to the old design demand. Their original level can now be
    # higher than the zone's level; that is precisely what this audit must test.
    for name, identifiers in (
        ("covered_cell_ids", zone.covered_cell_ids),
        ("overcovered_cell_ids", zone.overcovered_cell_ids),
        ("seed_cell_ids", zone.meta.get("seed_cell_ids", ())),
    ):
        if not isinstance(identifiers, (tuple, list)):
            raise ValueError(f"zone {zone.id}: invalid {name}")
        if any(type(identifier) is not int or identifier not in cells for identifier in identifiers):
            raise ValueError(f"zone {zone.id}: {name} contains an unknown source cell ID")


def _check_unchanged_geometry(old: LayoutZone, rebuilt: LayoutZone) -> None:
    if old.rebar != rebuilt.rebar:
        raise ValueError(f"zone {old.id}: reinforcement does not match the source level")
    if type(old.bar_count) is not int or old.bar_count != rebuilt.bar_count:
        raise ValueError(f"zone {old.id}: bar_count differs from reconstructed geometry")
    pairs = [
        (name, getattr(old, name), getattr(rebuilt, name))
        for name in (
            "width_mm", "required_length_mm", "anchored_length_mm", "installed_length_mm",
            "first_bar_coordinate_mm", "mass_kg",
        )
    ]
    for name in ("bbox", "demand_bbox"):
        values = getattr(old, name)
        if len(values) != 4:
            raise ValueError(f"zone {old.id}: invalid {name}")
        pairs.extend((name, value, reference)
                     for value, reference in zip(values, getattr(rebuilt, name)))
    for name, value, reference in pairs:
        if not _finite_number(value) or not math.isclose(
            value, reference, rel_tol=0.0, abs_tol=GEOMETRY_TOLERANCE_MM,
        ):
            raise ValueError(f"zone {old.id}: {name} differs from reconstructed geometry")


def _residual_polygons(cell: DemandCell, rectangles: tuple[BBox, ...]) -> tuple[ResidualPolygon, ...]:
    polygon = Polygon(cell.poly)
    remaining = polygon.difference(unary_union([box(*bbox) for bbox in rectangles]))
    # A rectangle crossing a cell can leave several components or a hole. Keep
    # both; a single residual bbox would falsely mark covered geometry deficient.
    geometries = (remaining,) if remaining.geom_type == "Polygon" else remaining.geoms
    result = [
        ResidualPolygon(
            shell=tuple((float(x), float(y)) for x, y in geometry.exterior.coords[:-1]),
            holes=tuple(tuple((float(x), float(y)) for x, y in ring.coords[:-1])
                        for ring in geometry.interiors),
        )
        for geometry in geometries
        if geometry.geom_type == "Polygon" and not geometry.is_empty
    ]
    return tuple(sorted(result, key=lambda polygon: (polygon.bbox, polygon.shell)))


def _find_residuals(
    original: LayoutProblem, zones: tuple[LayoutZone, ...],
) -> tuple[SourceDemandResidual, ...]:
    result: list[SourceDemandResidual] = []
    for cell in original.demand.cells:
        if original.demand.level(cell.level_index).requires_extra is not True:
            continue
        sufficient = tuple(
            zone for zone in zones
            if zone.level_index >= cell.level_index
            and polygon_bbox_intersection_area(cell.poly, zone.demand_bbox) > GEOMETRY_TOLERANCE_MM
        )
        rectangles = tuple(zone.demand_bbox for zone in sufficient)
        area = polygon_area(cell.poly)
        covered = polygon_bboxes_union_intersection_area(cell.poly, rectangles)
        tolerance = max(GEOMETRY_TOLERANCE_MM, area * 1e-8)
        if covered + tolerance >= area:
            continue
        result.append(SourceDemandResidual(
            cell_id=cell.id,
            required_level_index=cell.level_index,
            cell_bbox=cell_bbox(cell),
            cell_polygon=cell.poly,
            cell_area_mm2=area,
            covered_area_mm2=covered,
            uncovered_area_mm2=area - covered,
            coverage_tolerance_mm2=tolerance,
            sufficient_zone_ids=tuple(zone.id for zone in sufficient),
            residual_polygons=_residual_polygons(cell, rectangles),
        ))
    return tuple(sorted(result, key=lambda residual: residual.cell_id))


def revalidate_source_demand(
    original: LayoutProblem,
    solution: LayoutSolution,
) -> SourceDemandRevalidation:
    """Check an existing layout against ORIGINAL, not single-cell-reduced demand.

    The caller must provide the same source geometry, ordered reinforcement
    mapping, and detailing constraints that produced the existing layout. Known
    reduced input, unknown IDs/mapping, changed reinforcement or inconsistent
    physical fields raise ``ValueError``: none are repaired or silently discarded.
    Expected stale coverage caches are refreshed only after rebuilding and checking
    every physical field. Seed IDs do not constrain this reconstruction, since an
    originally stronger FE is allowed to expose a real deficiency in the old zone.

    A new feasible status is never called optimal. An infeasible audited candidate
    is returned intact with residual IDs/polygons for review or an explicit repair
    algorithm. Source inputs, old solution and nested metadata are not mutated.
    No host, cutting, construction approval or Revit readiness is inferred.
    """

    cells = _source_cells(original)
    zone_ids = [zone.id for zone in solution.zones]
    if any(not isinstance(identifier, str) or not identifier for identifier in zone_ids):
        raise ValueError("source revalidation requires nonempty string zone IDs")
    if len(set(zone_ids)) != len(zone_ids):
        raise ValueError("source revalidation requires unique zone IDs")
    if solution.zones and not cells:
        raise ValueError("zones cannot be revalidated against an empty source map")

    context = prepare_detailing(original) if solution.zones else None
    refreshed: list[LayoutZone] = []
    for zone in solution.zones:
        _check_source_ids(zone, cells)
        if type(zone.level_index) is not int:
            raise ValueError(f"zone {zone.id}: invalid level index")
        try:
            rebuilt = build_zone_from_bbox(
                original, zone.demand_bbox, zone.level_index, zone.id,
                first_bar_coordinate_mm=zone.first_bar_coordinate_mm,
                context=context,
            )
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"zone {zone.id}: cannot reconstruct unchanged geometry: {error}") from error
        _check_unchanged_geometry(zone, rebuilt)
        refreshed.append(replace(
            zone,
            covered_cell_ids=rebuilt.covered_cell_ids,
            overcovered_cell_ids=rebuilt.overcovered_cell_ids,
            meta=deepcopy(zone.meta),
        ))

    zones = tuple(refreshed)
    evaluation = evaluate_layout(original, zones, solution.request)
    residuals = _find_residuals(original, zones)
    if len(residuals) != evaluation.metrics.under_reinforced_cell_count:
        raise RuntimeError("source residual audit disagrees with the shared hard evaluator")
    audit = {
        "policy": SOURCE_REVALIDATION_POLICY,
        "geometry_changed": False,
        "source_demand_modified": False,
        "coverage_annotations_refreshed": any(
            old.covered_cell_ids != new.covered_cell_ids
            or old.overcovered_cell_ids != new.overcovered_cell_ids
            for old, new in zip(solution.zones, zones)
        ),
        "prior_status": solution.status.value,
        "prior_metrics": asdict(solution.metrics),
        "prior_diagnostics": solution.diagnostics,
        "source_case_id": original.case_id,
        "source_path": original.demand.source_path,
        "source_direction": str(original.demand.direction),
        "source_problem_meta": deepcopy(original.meta),
        "source_demand_meta": deepcopy(original.demand.meta),
        "evaluation_valid": evaluation.valid,
        "uncovered_cell_ids": tuple(residual.cell_id for residual in residuals),
        "uncovered_area_mm2": sum(residual.uncovered_area_mm2 for residual in residuals),
        "placement_eligible": False,
        "scope": "existing single-component coverage and layout contract; not Revit approval",
    }
    audited = replace(
        solution,
        status=SolutionStatus.FEASIBLE if evaluation.valid else SolutionStatus.INFEASIBLE,
        zones=zones,
        metrics=evaluation.metrics,
        request=deepcopy(solution.request),
        diagnostics=evaluation.diagnostics,
        meta={**deepcopy(solution.meta), "source_revalidation": audit},
    )
    return SourceDemandRevalidation(audited, evaluation, residuals)
