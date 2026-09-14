"""Lossless ordinary-layout adapter to the existing full-plate rollback trial.

The old solver's uniform axes are retained, including uniform @150 and @100.
They are NOT reinterpreted as the A101 periodic/contact patterns. The companion
review records that difference and independent demand checks; no placement
authorization, host/profile choice or geometry repair is performed here.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import math
from typing import Any

from rebar.application.gate_assessment import assess_plate_gates
from rebar.application.plate_revit_trial import DIRECTIONS, _same
from rebar.models import Axis
from rebar.optimization.contracts import PlateDirectionSolution, PlateProblem, PlateSolution
from rebar.optimization.services.bar_geometry import (
    bar_coordinates, longitudinal_interval, transverse_interval,
)
from rebar.optimization.services.detailing import rebar_mass_kg
from rebar.optimization.services.evaluation import evaluate_layout
from rebar.optimization.services.plate import build_plate_solution
from rebar.reporting.serialization import to_jsonable

_ALWAYS_UNCHECKED = (
    "research-only-uniform-layoutzone-axes", "source-DXF-to-host-calibration",
    "host-boundary-cover-openings", "background-phase-compatibility",
    "xy-layer-order", "background-and-additions-3d-collisions",
    "anchorage-engineering-acceptance", "stock-cutting", "revit-readback",
    "permanent-placement-not-authorized",
)


def _label(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError(f"{field} must be a nonempty label of at most 1024 characters")
    return value


def _integer(value: int, lower: int, upper: int, field: str) -> None:
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{field} must be an integer in {lower}..{upper}")


def _changes(problem: Any) -> dict:
    """Retain both metadata locations; never erase an older lowering report."""
    return {
        "problem": copy.deepcopy(problem.meta.get("single_cell_preprocessing")),
        "demand": copy.deepcopy(problem.demand.meta.get("single_cell_preprocessing")),
    }


def _has_changes(reports: dict) -> bool:
    return any(
        item and (item.get("changed_count", 0) or item.get("changes"))
        for item in reports.values()
    )


def _demand_digest(problem: Any) -> str:
    content = json.dumps(to_jsonable(problem.demand), ensure_ascii=False,
                         sort_keys=True, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _check_original(problem: PlateProblem, original: PlateProblem) -> None:
    """Only levels assigned to the same original cells may have changed."""
    for current in problem.direction_problems:
        source = original.problem(current.demand.direction)
        if _has_changes(_changes(source)):
            raise ValueError("original_problem still contains recorded demand lowering")
        if (current.demand.levels != source.demand.levels
                or current.demand.bbox != source.demand.bbox):
            raise ValueError("Original and analyzed demand have different legend or bounds")
        first = {c.id: (c.poly, c.centroid, c.aci) for c in current.demand.cells}
        second = {c.id: (c.poly, c.centroid, c.aci) for c in source.demand.cells}
        if (len(first) != len(current.demand.cells) or len(second) != len(source.demand.cells)
                or first != second):
            raise ValueError("Original and analyzed demand must contain the same complete cells")


def build_layout_plate_trial_bundle(
    problem: PlateProblem,
    solution: PlateSolution,
    *,
    source_sha256: str,
    steel_class: str,
    original_problem: PlateProblem | None = None,
) -> dict[str, Any]:
    """Return ``packet`` plus a separate, inspectable ``review``.

    The packet uses qmonitoring-full-plate-trial/v1 unchanged. Engineering
    warnings/undercoverage are retained for review, never silently filtered;
    inconsistent axes, quantities, mass or unsupported trial lengths are errors.
    ``original_problem`` permits independent checking of a legacy candidate on
    the unlowered map. Without it, recorded lowering makes original coverage
    explicitly unchecked. Source object graphs are not mutated.
    """
    _label(steel_class, "steel_class")
    if (not isinstance(source_sha256, str) or len(source_sha256) != 64
            or any(c not in "0123456789abcdef" for c in source_sha256)):
        raise ValueError("source_sha256 must be a lowercase SHA256 digest")
    if not isinstance(problem, PlateProblem) or not isinstance(solution, PlateSolution):
        raise ValueError("Typed complete PlateProblem and PlateSolution are required")
    if original_problem is not None:
        _check_original(problem, original_problem)

    blockers = set(_ALWAYS_UNCHECKED)
    directions, direction_reviews, reevaluated = [], [], []
    all_mass, all_length, all_count, all_zones = [], [], 0, 0
    original_undercovered = 0
    original_checked = True
    for direction_problem, name in zip(problem.direction_problems, DIRECTIONS):
        direction = direction_problem.demand.direction
        if str(direction) != name:
            raise ValueError("The full plate must contain all four canonical directions")
        direction_solution = solution.solution(direction)
        declared_class = direction_solution.meta.get("steel_class", "")
        if declared_class and declared_class != steel_class:
            raise ValueError("Explicit steel_class differs from the source solution")
        axis = 0 if direction.axis is Axis.X else 1
        runs, masses, lengths, count, seen = [], [], [], 0, set()
        patterns = set()
        for zone in direction_solution.zones:
            _label(zone.id, "zone_id")
            if zone.id in seen:
                raise ValueError("Repeated zone ID within one direction")
            seen.add(zone.id)
            _integer(zone.bar_count, 1, 1000, "bar_count")
            _integer(zone.rebar.diameter, 6, 40, "diameter_mm")
            step = zone.rebar.step
            if (isinstance(step, bool) or not isinstance(step, (int, float))
                    or not math.isfinite(step) or not 25 <= step <= 1000):
                raise ValueError("Uniform step outside the existing trial range")
            if len(zone.bbox) != 4 or any(
                isinstance(v, bool) or not isinstance(v, (int, float))
                or not math.isfinite(v) or abs(v) > 100_000_000 for v in zone.bbox
            ):
                raise ValueError("Invalid finite axis envelope")
            start, end = longitudinal_interval(direction.axis, zone.bbox)
            length = end - start
            if not 100 <= length <= 11700.000001:
                raise ValueError("Whole trial rejected: installed bar length must be 100..11700 mm")
            _same(length, zone.installed_length_mm, "installed length")
            coordinates = bar_coordinates(zone)
            low, high = transverse_interval(direction.axis, zone.bbox)
            _same(coordinates[0], low, "first axis")
            _same(coordinates[-1], high, "last axis")
            _same(high - low, zone.width_mm, "axis envelope width")
            mass = rebar_mass_kg(zone.rebar.diameter, length, zone.bar_count)
            _same(mass, zone.mass_kg, "zone mass")
            a, b = [0.0, 0.0], [0.0, 0.0]
            a[axis], b[axis] = start, end
            a[1-axis] = b[1-axis] = coordinates[0]
            run_id = _label(f"{name}:{zone.id}:0:0", "run_id")
            runs.append({"id": run_id, "zone_id": zone.id, "component_index": 0,
                "steel_class": steel_class, "diameter_mm": zone.rebar.diameter,
                "start_xy_mm": a, "end_xy_mm": b, "bar_count": zone.bar_count,
                "spacing_mm": step})
            masses.append(mass)
            lengths.append(length * zone.bar_count)
            count += zone.bar_count
            if step == 150:
                patterns.add("legacy-uniform-150-not-a101-100-200")
            elif step == 100:
                patterns.add("legacy-uniform-100-not-sto-2.7.9-contact")
        _same(math.fsum(masses), direction_solution.metrics.total_mass_kg, "direction mass")
        _same(math.fsum(lengths), direction_solution.metrics.total_bar_length_mm, "direction total length")
        _same(count, direction_solution.metrics.physical_bar_count, "direction count")
        _same(len(runs), direction_solution.metrics.detail_count, "direction zones")
        blockers.update(patterns)
        evaluation = evaluate_layout(direction_problem, direction_solution.zones, direction_solution.request)
        reevaluated.append(PlateDirectionSolution(direction, replace(direction_solution,
            metrics=evaluation.metrics,
            diagnostics=tuple(dict.fromkeys((*direction_solution.diagnostics, *evaluation.diagnostics))))))
        if not evaluation.valid:
            blockers.add("layout-independent-validation")
        preprocessing = _changes(direction_problem)
        if _has_changes(preprocessing):
            blockers.add("source-demand-lowered-in-analysis")
        source = (original_problem.problem(direction) if original_problem is not None
                  else direction_problem if not _has_changes(preprocessing) else None)
        original_evaluation = None
        if source is None:
            original_checked = False
            blockers.add("original-demand-coverage-not-checked")
        else:
            original_evaluation = evaluate_layout(source, direction_solution.zones, direction_solution.request)
            original_undercovered += original_evaluation.metrics.under_reinforced_cell_count
            if original_evaluation.metrics.under_reinforced_cell_count:
                blockers.add("original-demand-undercoverage")
        directions.append({"direction": name,
            "source": _label(direction_problem.demand.source_path or name, "source"), "runs": runs})
        direction_reviews.append({"direction": name,
            "axis_semantics": "unchanged-legacy-uniform-layoutzone",
            "axis_pattern_issues": sorted(patterns), "preprocessing": preprocessing,
            "source_demand_sha256": _demand_digest(direction_problem),
            "source_problem_metadata": to_jsonable(direction_problem.meta),
            "source_solution_metadata": to_jsonable(direction_solution.meta),
            "source_metrics": to_jsonable(direction_solution.metrics),
            "evaluation": to_jsonable(evaluation),
            "original_demand_sha256": None if source is None else _demand_digest(source),
            "original_evaluation": to_jsonable(original_evaluation)})
        all_mass.extend(masses)
        all_length.extend(lengths)
        all_count += count
        all_zones += len(runs)

    if not 1 <= all_count <= 5000 or all_zones > 512:
        raise ValueError("Full packet exceeds the existing trial limits; no truncation allowed")
    _same(math.fsum(all_mass), solution.metrics.total_mass_kg, "plate mass")
    _same(math.fsum(all_length), solution.metrics.total_bar_length_mm, "plate total length")
    _same(all_count, solution.metrics.physical_bar_count, "plate count")
    _same(all_zones, solution.metrics.zone_count, "plate zones")
    _same(4, solution.metrics.direction_count, "plate directions")
    assessment = assess_plate_gates(problem, build_plate_solution(reevaluated))
    blockers.update(item.id for item in assessment.items if item.status != "pass")
    if not solution.valid:
        blockers.add("source-plate-solution-invalid")
    packet = {"schema_version": "qmonitoring-full-plate-trial/v1",
        "mode": "commit-readback-rollback", "units": "mm", "placement_eligible": False,
        "case_id": _label(problem.case_id or "Full ordinary plate", "case_id"),
        "source_report_sha256": source_sha256, "source_blockers": sorted(blockers),
        "directions": directions, "expected": {"zone_count": all_zones, "run_count": all_zones,
            "physical_bar_count": all_count, "additional_mass_kg": math.fsum(all_mass)}}
    review = {"schema_version": "qmonitoring-layout-plate-review/v1", "units": "mm",
        "placement_eligible": False, "scope": "complete unchanged candidate for engineer review; rollback trial only",
        "source_report_sha256": source_sha256, "source_status": solution.status.value,
        "source_metrics": to_jsonable(solution.metrics), "source_diagnostics": list(solution.diagnostics),
        "source_metadata": to_jsonable(solution.meta), "source_problem_metadata": to_jsonable(problem.meta),
        "original_demand_coverage_checked": original_checked,
        "original_under_reinforced_cell_count": original_undercovered if original_checked else None,
        "original_coverage_scope": "uniform LayoutZone coverage model, not actual A101 periodic axes or host",
        "engineering_review_items": to_jsonable(assessment.items),
        "source_blockers": packet["source_blockers"][:], "directions": direction_reviews,
        "not_changed": ["axes", "diameters", "lengths", "quantities", "mass", "source demand"],
        "not_performed": list(_ALWAYS_UNCHECKED)}
    if len(json.dumps(packet, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 8 * 1024 * 1024:
        raise ValueError("Packet exceeds the existing 8 MiB limit")
    return copy.deepcopy({"packet": packet, "review": review})


def build_layout_plate_trial(
    problem: PlateProblem,
    solution: PlateSolution,
    *,
    source_sha256: str,
    steel_class: str,
    original_problem: PlateProblem | None = None,
) -> dict[str, Any]:
    """Return only the unchanged existing packet schema; use bundle for review."""
    return build_layout_plate_trial_bundle(problem, solution, source_sha256=source_sha256,
        steel_class=steel_class, original_problem=original_problem)["packet"]
