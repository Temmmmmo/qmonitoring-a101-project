"""Full original layout → explicit STO patterns → exact residual repair → batch.

The opt-in monotone single-addition policy is experimental. This service preserves
all original demand and produces a reviewable four-direction report, not engineering
approval, host fit, 3D collision clearance or permanent Revit placement.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from time import perf_counter

from rebar.optimization.algorithms.stock_length_balance import balance_stock_lengths
from rebar.optimization.contracts import PlateProblem, PlateSolution
from rebar.optimization.contracts.composite_coverage import MONOTONE_SINGLE_STO_COVERAGE_POLICY
from rebar.optimization.contracts.composite_search import CompositeSearchProblem
from rebar.optimization.contracts.placement import CompositeLayoutZone
from rebar.optimization.contracts.plate import _canonical_direction_items
from rebar.optimization.services.bar_schedule import build_bar_schedule, composite_schedule_groups
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.composite_windows import covering_composite_window
from rebar.optimization.services.cutting import PLATE_11700_BATCH_PROFILE, PLATE_11700_CUT_LENGTHS_MM
from rebar.optimization.services.geometry import cell_bbox
from rebar.optimization.services.patterned_conflicts import check_patterned_same_plane_conflicts
from rebar.optimization.services.source_revalidation import revalidate_source_demand
from rebar.optimization.services.stock_cutting import check_stock_cutting
from rebar.reporting.serialization import to_jsonable

from .analyze_composite_plate import (
    CompositeDirectionSettings, _direction_candidate, _placements, _same_mesh,
)


@dataclass(frozen=True)
class PatternedLayoutRecoveryResult:
    """Typed physical sets and existing composite report, always review-only."""

    report: dict
    direction_zones: tuple[tuple[CompositeLayoutZone, ...], ...]
    stock_balanced: bool


def recover_patterned_layout(
    problem: PlateProblem,
    solution: PlateSolution,
    settings: tuple[CompositeDirectionSettings, ...],
    *,
    maximum_patches_per_direction: int = 32,
    maximum_zones_per_direction: int = 128,
    maximum_batch_mass_increase_pct: float = 5.0,
    balance_time_limit_s: float = 20.0,
) -> PatternedLayoutRecoveryResult:
    """Convert all four directions without deleting any source FE or weak residual.

    Required settings explicitly name the background origin, @300 phase, contact
    side and class for every direction; there are no implicit engineering values.
    One exact-required recipe zone is added per remaining deficient FE, bounded by
    the patch/zone caps. Minimum width, original 40d and all current shared geometry
    rules are retained. The stock solver may only lengthen existing components,
    keeping counts and diameters; full coverage/schedule are rechecked afterwards.

    Time limit belongs to stock balancing; ``report.runtime_ms`` includes conversion,
    repair and independent rechecks too. Failure to balance returns a diagnostic
    report with no accepted front, not a silently relaxed limit or partial packet.
    """
    started = perf_counter()
    if not isinstance(problem, PlateProblem) or not isinstance(solution, PlateSolution):
        raise ValueError("typed complete PlateProblem and PlateSolution are required")
    for value, lower, upper, name in (
        (maximum_patches_per_direction, 0, 128, "maximum_patches_per_direction"),
        (maximum_zones_per_direction, 1, 128, "maximum_zones_per_direction"),
    ):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{name} must be an integer in {lower}..{upper}")
    for value, lower, upper, name in (
        (maximum_batch_mass_increase_pct, 0, 5, "maximum_batch_mass_increase_pct"),
        (balance_time_limit_s, 0.01, 60, "balance_time_limit_s"),
    ):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not lower <= value <= upper):
            raise ValueError(f"{name} must be finite in {lower}..{upper}")
    configs = _canonical_direction_items(settings, lambda config: config.direction,
                                         item_name="patterned recovery settings")
    if any(not config.steel_class.strip() for config in configs):
        raise ValueError("every direction requires an explicitly declared steel class")
    if any(p.constraints.cutting_profile != "plate-11700"
           or p.constraints.allowed_cut_lengths_mm != PLATE_11700_CUT_LENGTHS_MM
           for p in problem.direction_problems):
        raise ValueError("patterned stock recovery requires the explicit plate-11700 source profile")
    _same_mesh(tuple(p.demand for p in problem.direction_problems))
    searches, zone_groups, directions = [], [], []
    for original, config in zip(problem.direction_problems, configs):
        previous = solution.solution(original.demand.direction)
        checked = revalidate_source_demand(original, previous)
        if not checked.evaluation.valid:
            raise ValueError(f"source layout does not cover original demand: {original.demand.direction}")
        profile = dict(_placements(original.demand, config))
        # Reject multi-addition input before constructing any partial conversion.
        evaluate_composite_coverage(original.demand, (), policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY,
                                    constraints=original.constraints)
        zones = []
        for old in previous.zones:
            bounds = covering_composite_window(original.demand, old.demand_bbox, old.level_index,
                                               profile[old.level_index], constraints=original.constraints)
            zones.append(build_composite_zone(original.demand, bounds, old.level_index, old.id,
                                              profile[old.level_index], constraints=original.constraints))
        before = evaluate_composite_coverage(original.demand, zones, policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY,
                                             constraints=original.constraints)
        if not before.geometry_and_patterns_valid:
            raise ValueError("converted geometry or STO patterns failed independent validation")
        missing = tuple(row.cell_id for row in before.cells if not row.covered)
        if len(missing) > maximum_patches_per_direction or len(zones) + len(missing) > maximum_zones_per_direction:
            raise ValueError(f"{original.demand.direction}: full exact repair exceeds patch/zone cap; "
                             f"{len(missing)} residual cells retained, none discarded")
        cells = {cell.id: cell for cell in original.demand.cells}
        used_ids = {zone.id for zone in zones}
        patches = []
        for cell_id in missing:
            cell = cells[cell_id]
            bounds = covering_composite_window(original.demand, cell_bbox(cell), cell.level_index,
                                               profile[cell.level_index], constraints=original.constraints)
            identifier = f"exact-required-{cell_id}"
            while identifier in used_ids:
                identifier += "-new"
            used_ids.add(identifier)
            patch = build_composite_zone(original.demand, bounds, cell.level_index, identifier,
                                         profile[cell.level_index], constraints=original.constraints)
            zones.append(patch)
            patches.append({"cell_id": cell_id, "zone_id": identifier, "required_recipe": to_jsonable(patch.recipe)})
        search = CompositeSearchProblem(original.demand, tuple(profile.items()), original.constraints,
                                        MONOTONE_SINGLE_STO_COVERAGE_POLICY)
        option = _direction_candidate(search, tuple(zones), config, 0)
        option["source_recovery"] = {"coverage_before_exact_patches": to_jsonable(before),
                                     "residual_patches": patches, "source_cell_count": len(cells),
                                     "prior_diagnostics": previous.diagnostics}
        searches.append(search)
        zone_groups.append(tuple(zones))
        directions.append({"direction": to_jsonable(config.direction), "settings": to_jsonable(config),
                           "source": {"filenames": {"dxf": Path(original.demand.source_path).name}},
                           "source_cell_count": len(cells), "candidates": [option]})

    def schedule_for(groups):
        return build_bar_schedule(group for zones, config in zip(groups, configs)
                                  for group in composite_schedule_groups(zones, steel_class=config.steel_class))

    def point_for(groups, candidate_index, cutting):
        schedule = schedule_for(groups)
        return {"direction_candidate_indexes": [candidate_index] * 4,
                "zone_count": sum(len(zones) for zones in groups), "position_count": len(schedule),
                "physical_bar_count": sum(row.physical_bar_count for row in schedule),
                "additional_mass_kg": math.fsum(row.total_mass_kg for row in schedule),
                "bar_schedule": to_jsonable(schedule), "stock_cutting": cutting}

    groups = tuple(group for zones, config in zip(zone_groups, configs)
                   for group in composite_schedule_groups(zones, steel_class=config.steel_class))
    if not groups:
        raise ValueError("selected source has no additional bars to recover or balance")
    initial_stock = check_stock_cutting(schedule_for(zone_groups), time_limit_s=min(10, balance_time_limit_s))
    diagnostic_point = point_for(zone_groups, 0, initial_stock)
    balance = balance_stock_lengths(groups, maximum_mass_increase_pct=maximum_batch_mass_increase_pct,
                                    time_limit_s=balance_time_limit_s)
    final_groups = tuple(zone_groups)
    front = []
    if balance.status == "balanced":
        lengths = dict(balance.installed_lengths_mm)
        rebuilt = []
        for original, zones, config, search, row in zip(problem.direction_problems, zone_groups, configs, searches, directions):
            constraints = replace(original.constraints, cutting_profile=PLATE_11700_BATCH_PROFILE)
            changed = tuple(build_composite_zone(original.demand, zone.demand_bbox, zone.level_index, zone.id,
                zone.placement, constraints=constraints, installed_lengths_mm=tuple(
                    lengths[f"{zone.direction}:{zone.id}:{component.component_index}"] for component in zone.components))
                for zone in zones)
            if any(a.bar_count != b.bar_count or a.rebar != b.rebar
                   or b.installed_length_mm < a.installed_length_mm - 1e-6
                   for first, second in zip(zones, changed) for a, b in zip(first.components, second.components)):
                raise ValueError("balance changed counts/diameters or shortened a component")
            row["candidates"].append(_direction_candidate(replace(search, constraints=constraints), changed, config, 1))
            rebuilt.append(changed)
        final_groups = tuple(rebuilt)
        actual_stock = check_stock_cutting(schedule_for(final_groups), time_limit_s=min(10, balance_time_limit_s))
        point = point_for(final_groups, 1, actual_stock)
        if (point["physical_bar_count"] != diagnostic_point["physical_bar_count"]
                or point["additional_mass_kg"] > diagnostic_point["additional_mass_kg"] * (1 + maximum_batch_mass_increase_pct / 100) + 1e-6
                or actual_stock["status"] != "pass"):
            raise ValueError("full rebuilt stock inventory/count/mass failed independent validation")
        front.append(point)
    blocks = sorted({check for row in directions for check in row["candidates"][-1]["coverage"]["remaining_check_ids"]}
                    | {"stock-cutting-manufacturing-assumptions", "coplanar-background-contact-and-depths", "permanent-placement-not-authorized"})
    if not front:
        blocks.append("stock-cutting-zero-waste")
    conflicts = check_patterned_same_plane_conflicts(
        tuple(zone for group in final_groups for zone in group),
        assume_same_depth_per_direction=True,
    )
    if conflicts.body_intersection_count:
        blocks.append("same-plane-additional-bar-intersections")
    report = {"schema_version": "composite-plate-analysis/v1", "units": "mm", "case_id": problem.case_id,
              "status": "full_coverage_candidates_found" if front else "stock_balance_not_found",
              "placement_eligible": False, "source_demand_preserved": True, "averaging": "not_applied",
              "same_plane_conflicts": to_jsonable(conflicts),
              "physical_placement_status": "blocked_same_plane_intersections" if conflicts.body_intersection_count
                  else "same_plane_check_clear_host_and_3d_not_checked",
              "coverage_policy": MONOTONE_SINGLE_STO_COVERAGE_POLICY, "direction_count": 4,
              "directions": directions, "front": front, "selected_index": 0 if front else None,
              "diagnostic_front_before_cutting": [diagnostic_point], "length_balance_attempts": [to_jsonable(balance)],
              "maximum_cutting_overhead_pct": maximum_batch_mass_increase_pct,
              "host_envelope": None, "blocking_check_ids": blocks,
              "front_scope": "zero-waste-candidates-under-explicit-monotone-single-research-profile",
              "prior_metrics": to_jsonable(solution.metrics),
              "limits": {"maximum_patches_per_direction": maximum_patches_per_direction,
                         "maximum_zones_per_direction": maximum_zones_per_direction,
                         "balance_time_limit_s": balance_time_limit_s},
              "runtime_ms": (perf_counter() - started) * 1000}
    return PatternedLayoutRecoveryResult(report, final_groups, bool(front))
