"""Recover missing original demand around an existing research layout.

This is a bounded geometric repair, not permission to average demand or place bars
in Revit. Existing coverage is preserved by replacing zones only with a containing
rectangle of an equal or stronger level. Final geometry uses the common constructor
and validator; unresolved physical detailing warnings remain visible.
"""

from __future__ import annotations

from dataclasses import replace
import math

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from ..contracts import LayoutProblem, LayoutSolution, SolutionStatus
from ..services import build_zone_from_bbox, prepare_detailing
from ..services.cutting import CutLengthInfeasibleError
from ..services.geometry import GEOMETRY_TOLERANCE_MM, bboxes_distance
from ..services.source_revalidation import revalidate_source_demand


def recover_source_demand(
    original: LayoutProblem,
    solution: LayoutSolution,
    *,
    bar_penalty_kg: float = 0.0,
    position_penalty_kg: float = 0.0,
    neighbor_count: int = 6,
) -> LayoutSolution:
    """Patch uncovered original cells without deleting demand or weakening rules.

    Penalties guide this repair only, not shared mass metrics. ``neighbor_count``
    bounds the local neighborhood; failure is not a proof of global infeasibility.
    The returned status is coverage feasibility, never full installation approval.
    """
    if (not all(not isinstance(v, bool) and isinstance(v, (int, float))
                and math.isfinite(v) and v >= 0 for v in (bar_penalty_kg, position_penalty_kg))
            or type(neighbor_count) is not int or not 1 <= neighbor_count <= 64):
        raise ValueError("repair penalties must be finite/nonnegative; neighbor_count must be 1..64")
    audit = revalidate_source_demand(original, solution)
    if not audit.uncovered_cells:
        return audit.solution
    context = prepare_detailing(original)
    zones = list(audit.solution.zones)
    residuals = {}
    for item in audit.uncovered_cells:
        cell = context.cells_by_id[item.cell_id]
        sufficient = [box(*z.demand_bbox) for z in zones if z.level_index >= cell.level_index]
        residuals[cell.id] = (cell.level_index, Polygon(cell.poly).difference(unary_union(sufficient)))
    original_missing_count = len(residuals)
    used_ids = {z.id for z in zones}
    trace = []

    def signature(zone):
        return zone.rebar.diameter, round(zone.installed_length_mm, 6)

    def contains(outer, inner):
        return (outer[0] <= inner[0] and outer[1] <= inner[1]
                and outer[2] >= inner[2] and outer[3] >= inner[3])

    for iteration in range(original_missing_count):
        if not residuals:
            break
        proposed = set()
        for cell_id, (level, _residual) in sorted(residuals.items()):
            cell = context.cells_by_id[cell_id]
            xs, ys = zip(*cell.poly)
            cell_box = (min(xs), min(ys), max(xs), max(ys))
            proposed.add((cell_box, level))
            neighbors = sorted(zones, key=lambda z: (bboxes_distance(cell_box, z.demand_bbox), z.id))[:neighbor_count]
            for zone in neighbors:
                b = zone.demand_bbox
                hull = (min(b[0], cell_box[0]), min(b[1], cell_box[1]),
                        max(b[2], cell_box[2]), max(b[3], cell_box[3]))
                proposed.add((hull, max(level, zone.level_index)))

        new_id = f"source-recovery-{iteration + 1}"
        while new_id in used_ids:
            new_id += "-new"
        current_types = {signature(z) for z in zones}
        best = None
        for bounds, level in sorted(proposed):
            try:
                candidate = build_zone_from_bbox(
                    original, bounds, level, new_id, context=context, collect_coverage=False,
                )
            except CutLengthInfeasibleError:
                continue
            shape = box(*bounds)
            gain = sum(geometry.intersection(shape).area for required, geometry in residuals.values()
                       if level >= required)
            if gain <= GEOMETRY_TOLERANCE_MM:
                continue
            removed = tuple(z for z in zones if z.level_index <= level and contains(bounds, z.demand_bbox))
            removed_ids = {z.id for z in removed}
            kept = [z for z in zones if z.id not in removed_ids]
            if (len(kept) + 1 > len(original.demand.cells)
                    or (solution.request.max_details is not None
                        and len(kept) + 1 > solution.request.max_details)):
                continue
            mass_delta = candidate.mass_kg - sum(z.mass_kg for z in removed)
            bars_delta = candidate.bar_count - sum(z.bar_count for z in removed)
            type_delta = len({signature(z) for z in (*kept, candidate)}) - len(current_types)
            cost = mass_delta + bar_penalty_kg * bars_delta + position_penalty_kg * type_delta
            rank = (cost / gain, mass_delta, bars_delta, bounds, level)
            if best is None or rank < best[0]:
                best = (rank, candidate, kept, removed_ids, gain, mass_delta, bars_delta)
        if best is None:
            break
        _rank, chosen, kept, removed_ids, gain, mass_delta, bars_delta = best
        zones = [*kept, chosen]
        used_ids.add(chosen.id)
        shape = box(*chosen.demand_bbox)
        for cell_id, (required, geometry) in tuple(residuals.items()):
            if chosen.level_index < required:
                continue
            remaining = geometry.difference(shape)
            cell_area = Polygon(context.cells_by_id[cell_id].poly).area
            if remaining.area <= max(GEOMETRY_TOLERANCE_MM, cell_area * 1e-8):
                del residuals[cell_id]
            else:
                residuals[cell_id] = (required, remaining)
        trace.append({
            "new_zone_id": chosen.id, "removed_zone_ids": tuple(sorted(removed_ids)),
            "covered_residual_area_mm2": gain, "mass_delta_kg": mass_delta,
            "physical_bar_delta": bars_delta, "remaining_cell_count": len(residuals),
        })

    # Candidates have no coverage caches until this independent final check.
    checked = revalidate_source_demand(original, replace(audit.solution, zones=tuple(zones))).solution
    return replace(
        checked, algorithm=f"{solution.algorithm}+source-recovery",
        status=SolutionStatus.FEASIBLE if checked.metrics.under_reinforced_cell_count == 0
        and checked.status is SolutionStatus.FEASIBLE else checked.status,
        meta={**checked.meta, "source_recovery": {
            "method": "bounded-containing-zone-repair/v1",
            "input_mass_kg": audit.solution.metrics.total_mass_kg,
            "input_physical_bar_count": audit.solution.metrics.physical_bar_count,
            "original_missing_cell_count": original_missing_count,
            "bar_penalty_kg": bar_penalty_kg, "position_penalty_kg": position_penalty_kg,
            "neighbor_count": neighbor_count, "moves": tuple(trace),
            "remaining_cell_count": checked.metrics.under_reinforced_cell_count,
            "installation_approved": False,
        }},
    )
