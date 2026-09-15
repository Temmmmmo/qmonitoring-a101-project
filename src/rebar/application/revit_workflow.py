"""Actual single-DXF calculation for the TZ 8.1 view-family workflow.

No artificial missing directions, no structural placement, no background override.
The ordinary GA proposes rectangles; shared STO detailing then recomputes the
actual source components and all source-FE coverage before presentation.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import re

from ezdxf.colors import aci2rgb

from rebar.application.analyze_composite_plate import CompositeDirectionSettings, _placements
from rebar.application.analyze_direction import load_direction_mosaic
from rebar.application.composite_layout_review import _reject, _unique
from rebar.application.composite_revit_export import build_composite_zone_revit_export
from rebar.optimization import (AlgorithmRequest, ComplexityAxis, LayoutCandidateGenerator,
    LayoutConstraints, build_layout_problem, built_in_optimizer_registry, measure_constructability)
from rebar.optimization.contracts.composite_coverage import MONOTONE_SINGLE_STO_COVERAGE_POLICY
from rebar.optimization.services.bar_schedule import build_bar_schedule, composite_schedule_groups
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.composite_windows import covering_composite_window
from rebar.optimization.services.cutting import PLATE_11700_CUT_LENGTHS_MM
from rebar.optimization.services.evaluation import evaluate_layout
from rebar.optimization.services.patterned_conflicts import check_patterned_same_plane_conflicts
from rebar.optimization.services.stock_cutting import check_stock_cutting
from rebar.reporting.serialization import to_jsonable

REQUEST_SCHEMA = "qmonitoring-workflow-calculation-request/v1"
RESULT_SCHEMA = "qmonitoring-workflow-analysis/v1"
ALGORITHMS = ("genetic-pareto", "bsp", "greedy-priority")
MAX_FILE_BYTES = 30*1024*1024


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    return value


def _number(value, lower, upper, name):
    if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f"{name}: expected finite number in {lower}..{upper}")
    return value


def parse_request(content):
    if not isinstance(content, bytes) or not 0 < len(content) <= 16384:
        raise ValueError("Request must be UTF-8 JSON up to 16 KiB")
    data = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique,
                      parse_constant=_reject, parse_float=_finite_float)
    fields = {"schema_version", "direction", "settings", "axis_profile", "mapping_id", "source_sha256"}
    if not isinstance(data, dict) or set(data) != fields or data["schema_version"] != REQUEST_SCHEMA:
        raise ValueError("Unsupported workflow calculation request")
    if data["direction"] not in ("bottom-X", "bottom-Y", "top-X", "top-Y"):
        raise ValueError("Select one of four directions")
    values = data["settings"]
    expected = {"background_diameter_mm", "background_step_mm", "anchorage_diameters",
                "minimum_zone_fe_count", "algorithm", "mass_preference"}
    if not isinstance(values, dict) or set(values) != expected:
        raise ValueError("Every TZ 8.1 calculation setting is required")
    _number(values["background_diameter_mm"], 1, 80, "background diameter")
    _number(values["background_step_mm"], 1, 2000, "background step")
    _number(values["anchorage_diameters"], 40, 200, "anchorage")
    _number(values["mass_preference"], 0, 1, "mass preference")
    if type(values["minimum_zone_fe_count"]) is not int or values["minimum_zone_fe_count"] not in (2, 3):
        raise ValueError("Minimum width must be 2 or 3 FE")
    if values["algorithm"] not in ALGORITHMS:
        raise ValueError("Unknown workflow algorithm")
    profile = data["axis_profile"]
    if not isinstance(profile, dict) or set(profile) != {"background_origin_mm", "first_300_offset_mm", "contact_side", "steel_class"}:
        raise ValueError("Explicit STO source-axis profile required")
    _number(profile["background_origin_mm"], -1e8, 1e8, "background origin")
    _number(profile["first_300_offset_mm"], 0, 300, "additional @300 phase")
    if profile["contact_side"] not in ("left", "right"):
        raise ValueError("Explicit coplanar @100 contact side required")
    if not isinstance(profile["steel_class"], str) or not 1 <= len(profile["steel_class"].strip()) <= 80:
        raise ValueError("Declare the steel class")
    hashes = data["source_sha256"]
    if not isinstance(hashes, dict) or set(hashes) not in ({"dxf"}, {"dxf", "shk"}):
        raise ValueError("Exact DXF and optional SHK hashes required")
    for digest in hashes.values():
        if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
            raise ValueError("Invalid source SHA256")
    if not isinstance(data["mapping_id"], str) or len(data["mapping_id"]) > 100:
        raise ValueError("Invalid source mapping")
    if ("shk" in hashes) != (data["mapping_id"] == "auto"):
        raise ValueError("Use either SHK with mapping auto or an explicit mapping without SHK")
    return data


def _read_hash(path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES+1)
    if not 0 < len(data) <= MAX_FILE_BYTES:
        raise ValueError("Empty or oversized source file")
    return hashlib.sha256(data).hexdigest()


def analyze_workflow(dxf_path, request_bytes, *, shk_path=None):
    request = parse_request(request_bytes)
    dxf_path = Path(dxf_path)
    if dxf_path.suffix.lower() != ".dxf" or (shk_path is not None and Path(shk_path).suffix.lower() != ".shk"):
        raise ValueError("This workflow reads actual DXF + SHK/mapping, not PNG")
    hashes = {"dxf": _read_hash(dxf_path)}
    if shk_path is not None:
        hashes["shk"] = _read_hash(shk_path)
    if hashes != request["source_sha256"]:
        raise ValueError("Uploaded source hashes differ from the confirmed request")
    mosaic = load_direction_mosaic(dxf_path, shk_path=shk_path, mapping_id=request["mapping_id"])
    if str(mosaic.direction) != request["direction"]:
        raise ValueError("DXF direction differs from the selected Revit direction")
    config = request["settings"]
    if not 1 <= len(mosaic.cells) <= 12000:
        raise ValueError("Source FE resource cap exceeded; no cells are dropped")
    for band in mosaic.legend:
        recipe = band.reinforcement_recipe
        if len(recipe.additions) > 1:
            raise ValueError("This workflow v1 supports one additional set per level; no addition was discarded")
        if (recipe.background.diameter != config["background_diameter_mm"]
                or recipe.background.step != config["background_step_mm"]):
            raise ValueError("Requested background D/step differs from the source scale; change the input or settings explicitly")
    constraints = LayoutConstraints(min_width_cells=config["minimum_zone_fe_count"],
        anchorage_diameters=config["anchorage_diameters"], allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM,
        cutting_profile="plate-11700")
    problem = build_layout_problem(mosaic, constraints)
    profile = request["axis_profile"]
    placement_config = CompositeDirectionSettings(mosaic.direction, profile["background_origin_mm"],
        profile["first_300_offset_mm"], 0, profile["steel_class"],
        "Explicit Workflow 8.1 user source-profile; not engineering approval", profile["contact_side"])
    placements = dict(_placements(problem.demand, placement_config))
    optimizer = built_in_optimizer_registry().create(config["algorithm"])
    algorithm_request = AlgorithmRequest(max_details=128, time_limit_s=60, params={
        "population_size": 8, "generations": 3, "random_seed": 42,
        "complexity_axis": ComplexityAxis.PHYSICAL_BAR_COUNT.value})
    solutions = (optimizer.solve_many(problem, algorithm_request) if isinstance(optimizer, LayoutCandidateGenerator)
                 else (optimizer.solve(problem, algorithm_request),))
    converted = []
    for solution in solutions:
        evaluation = evaluate_layout(problem, solution.zones, algorithm_request)
        if not evaluation.valid:
            continue
        zones = tuple(build_composite_zone(problem.demand,
            covering_composite_window(problem.demand, old.demand_bbox, old.level_index, placements[old.level_index], constraints=constraints),
            old.level_index, old.id, placements[old.level_index], constraints=constraints) for old in solution.zones)
        coverage = evaluate_composite_coverage(problem.demand, zones, policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY,
                                               constraints=constraints)
        if not coverage.geometry_and_patterns_valid:
            continue
        converted.append((zones, coverage, replace(solution, metrics=evaluation.metrics)))
    if not converted:
        raise ValueError("No independently valid source rectangles; no families may be generated from a partial candidate")
    usable = [item for item in converted if item[1].status == "pass"] or converted
    masses = [item[1].additional_mass_kg for item in usable]
    counts = [item[1].physical_bar_count for item in usable]
    weight = config["mass_preference"]
    def score(item):
        check = item[1]
        m = (check.additional_mass_kg-min(masses))/(max(masses)-min(masses)) if max(masses) > min(masses) else 0
        n = (check.physical_bar_count-min(counts))/(max(counts)-min(counts)) if max(counts) > min(counts) else 0
        return weight*m+(1-weight)*n, check.additional_mass_kg, check.physical_bar_count
    zones, coverage, prior = min(usable, key=score)
    schedule = build_bar_schedule(composite_schedule_groups(zones, steel_class=profile["steel_class"]))
    stock = check_stock_cutting(schedule, time_limit_s=5)
    conflicts = check_patterned_same_plane_conflicts(zones, assume_same_depth_per_direction=True)
    source = {"direction": to_jsonable(mosaic.direction), "source_bbox_mm": list(problem.demand.bbox),
        "cells": [{"cell_id": c.id, "polygon_mm": to_jsonable(c.poly), "aci": c.aci, "level_index": c.level_index} for c in problem.demand.cells],
        "legend": [{"level_index": level.index, "aci": level.aci, "rgb": list(aci2rgb(level.aci)),
                    "label": level.label} for level in problem.demand.levels],
        "zone_drafts": [build_composite_zone_revit_export(problem.demand, z, constraints=constraints) for z in zones]}
    return {"schema_version": RESULT_SCHEMA, "units": "mm", "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "request": request, "source_sha256": hashes, "case_id": dxf_path.stem,
        "source_stage": "original-parametric-zones-before-physical-normalization", "source": source,
        "calculation_performed": True, "placement_eligible": False, "engineering_approval": False,
        "source_demand_preserved": True, "source_cell_count": len(problem.demand.cells),
        "metrics": {"source_zone_count": len(zones), "physical_bar_count": coverage.physical_bar_count,
                    "additional_mass_kg": coverage.additional_mass_kg, "position_count": len(schedule)},
        "checks": {"coverage": to_jsonable(coverage), "stock_cutting": stock,
                   "same_plane_conflicts": to_jsonable(conflicts), "host_3d_revit": "not_checked"},
        "candidate_count": len(converted), "objective_scope": "User-weighted finite validated candidate pool, not a global Point 3",
        "prior_uniform_proposal_metrics": to_jsonable(measure_constructability(prior)),
        "limitations": ["single-addition levels only", "explicit source STO phases; Z and host not checked",
            "source-zone families are not normalized/trimmed physical bars", "PNG input unsupported"]}
