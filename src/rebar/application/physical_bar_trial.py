"""Lossless physical-bar diagnostic transport, separate from parameterized zones.

The caller supplies freshly parsed original demand and exact input-file hashes.
Source zones are reconstructed and independently revalidated; normalized bars may
share several source owners, but never replace the LayoutZone contract. The only
permitted execution mode is a whole-plan, mandatory-rollback Revit experiment.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path

from rebar.optimization.contracts.plate import PLATE_DIRECTIONS, PlateProblem
from rebar.optimization.services.bar_schedule import BarScheduleGroup, build_bar_schedule
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.contracts.composite_coverage import MONOTONE_SINGLE_STO_COVERAGE_POLICY
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.cutting import PLATE_11700_BATCH_PROFILE, PLATE_11700_CUT_LENGTHS_MM
from rebar.optimization.services.detailing import rebar_mass_kg
from rebar.optimization.services.stock_cutting import check_stock_cutting
from rebar.reporting.serialization import to_jsonable

from .analyze_composite_plate import CompositeDirectionSettings, _placements, _same_mesh
from .composite_revit_export import build_composite_zone_revit_export
from .plate_revit_trial import build_full_plate_trial

SCHEMA = "physical-bar-plan-trial/v1"
TOLERANCE_MM = 1e-6
MAX_BARS = 5000
MAX_PAIRS = 200000
_DIRECTIONS = tuple(map(str, PLATE_DIRECTIONS))


@dataclass(frozen=True)
class PhysicalBarTrialResult:
    """New public packet and its hash-bound review; no legacy packet is exposed."""

    packet: dict
    review: dict


def _number(value, label, lower=-1e8, upper=1e8, *, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not lower <= value <= upper
            or (integer and not isinstance(value, int))):
        raise ValueError(f"Invalid {label}")
    return value


def _label(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError(f"Invalid {label}")
    return value


def _digest(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("Expected an actual input SHA256")
    return value


def _equivalent(actual, expected, path="source zone"):
    """Check every exported field, tolerating only normal geometric roundoff."""
    if isinstance(expected, bool) or expected is None or isinstance(expected, str):
        same = type(actual) is type(expected) and actual == expected
    elif isinstance(expected, (int, float)):
        same = (not isinstance(actual, bool) and isinstance(actual, (int, float))
                and math.isfinite(actual) and abs(actual - expected) <= TOLERANCE_MM)
    elif isinstance(expected, dict):
        same = isinstance(actual, dict) and actual.keys() == expected.keys()
        if same:
            for key in expected:
                _equivalent(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, (list, tuple)):
        same = isinstance(actual, (list, tuple)) and len(actual) == len(expected)
        if same:
            for index, value in enumerate(expected):
                _equivalent(actual[index], value, f"{path}[{index}]")
    else:
        same = False
    if not same:
        raise ValueError(f"Source geometry/metadata does not reproduce: {path}")


def _revalidate_sources(report, problem, candidate_index, source_hash):
    # Existing exporter checks every selected source count/axis/mass and all four
    # directions. Its return value is deliberately NOT our physical transport.
    legacy_source = build_full_plate_trial(report, candidate_index, source_hash)
    certificates, source_refs, checks, retained = _revalidate_source_geometry(report, problem, candidate_index)
    return certificates, source_refs, checks, retained, legacy_source["expected"]


def _revalidate_source_geometry(report, problem, candidate_index):
    """Original FE/zone/axis proof only; does NOT authorise a stock or Revit packet."""
    if (type(candidate_index) is not int or not 0 <= candidate_index < len(report.get("front", ()))
            or len(report.get("directions", ())) != 4
            or len(report["front"][candidate_index].get("direction_candidate_indexes", ())) != 4):
        raise ValueError("Exactly four selected source directions required")
    if report.get("coverage_policy") != MONOTONE_SINGLE_STO_COVERAGE_POLICY:
        raise ValueError("Explicit monotone-single STO source policy required")
    if report.get("averaging") != "not_applied" or report.get("case_id") != problem.case_id:
        raise ValueError("Source case or original-demand preservation differs")
    _same_mesh(tuple(p.demand for p in problem.direction_problems))
    point = report["front"][candidate_index]
    certificates, source_refs, checks, retained = [], {}, [], []
    for original, source, selected in zip(problem.direction_problems, report["directions"],
                                          point["direction_candidate_indexes"]):
        direction = original.demand.direction
        if to_jsonable(direction) != source["direction"]:
            raise ValueError("Fresh original demand direction differs")
        if (original.constraints.anchorage_diameters != 40
                or original.constraints.allowed_cut_lengths_mm != PLATE_11700_CUT_LENGTHS_MM):
            raise ValueError("Diagnostic normalization requires original 40d and plate-11700 catalog")
        preprocessing = original.demand.meta.get("single_cell_preprocessing", {})
        if preprocessing.get("changed_count", 0) != 0:
            raise ValueError("Reduced source demand cannot authorize physical normalization")
        if Path(original.demand.source_path).name != source["source"]["filenames"]["dxf"]:
            raise ValueError("Source DXF filename differs from freshly loaded problem")
        config = CompositeDirectionSettings(**{**source["settings"], "direction": direction})
        _label(config.steel_class, "declared source steel class")
        placements = dict(_placements(original.demand, config))
        constraints = replace(original.constraints, cutting_profile=PLATE_11700_BATCH_PROFILE)
        candidate = next(c for c in source["candidates"] if c["candidate_index"] == selected)
        zones = []
        for draft in candidate["zone_drafts"]:
            level = _number(draft["level_index"], "source level", 0, 1000, integer=True)
            zone = build_composite_zone(original.demand, tuple(draft["demand_bbox_mm"]), level,
                draft["source_zone_id"], placements[level], constraints=constraints,
                installed_lengths_mm=tuple(c["installed_length_mm"] for c in draft["components"]))
            # The source detailing checker permits asymmetric surplus, not less
            # than full 40d at either end. Preserve its exact installed interval.
            along = 0 if str(direction).endswith("X") else 1
            zone = replace(zone, components=tuple(replace(c, longitudinal_interval_mm=(
                raw["bar_axis_bbox_mm"][along], raw["bar_axis_bbox_mm"][along + 2]))
                for c, raw in zip(zone.components, draft["components"])))
            fresh = build_composite_zone_revit_export(original.demand, zone, constraints=constraints)
            _equivalent(draft, fresh)
            if (zone.recipe.background.step != 300 or zone.placement.background.pattern.period_mm != 300
                    or zone.placement.background.pattern.offsets_mm != (0.0,)):
                raise ValueError("Physical trial v1 supports only explicit uniform background @300")
            zones.append(zone)
            certificate = {"direction": str(direction), "zone_id": zone.id, "components": []}
            required = [zone.demand_bbox[along], zone.demand_bbox[along + 2]]
            for component, raw in zip(zone.components, fresh["components"]):
                row = {"component_index": component.component_index,
                    "source_bar_count": component.bar_count, "diameter_mm": component.rebar.diameter,
                    "required_interval_mm": list(required), "axis_coordinates_mm": raw["axis_coordinates_mm"],
                    "steel_class": config.steel_class, "background_diameter_mm": zone.recipe.background.diameter,
                    "background_origin_mm": zone.placement.background.origin_mm}
                certificate["components"].append(row)
                for index, coordinate in enumerate(row["axis_coordinates_mm"]):
                    key = (str(direction), f"{zone.id}/{component.component_index}/{index}")
                    if key in source_refs:
                        raise ValueError("Ambiguous source bar identifier")
                    source_refs[key] = {"certificate": row, "coordinate": coordinate,
                        "ref": {"zone_id": zone.id, "component_index": component.component_index, "bar_index": index}}
            certificates.append(certificate)
        check = evaluate_composite_coverage(original.demand, tuple(zones),
            policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY, constraints=constraints)
        if check.status != "pass":
            raise ValueError(f"Fresh original source coverage failed: {direction}")
        _equivalent(candidate["coverage"], to_jsonable(check), "source coverage")
        checks.append({"direction": str(direction), "status": check.status,
                       "demanded_cell_count": check.demanded_cell_count,
                       "uncovered_cell_count": check.uncovered_cell_count})
        retained.append({"direction": str(direction), "zone_drafts": deepcopy(candidate["zone_drafts"])})
    if not 1 <= len(source_refs) <= MAX_BARS or len(certificates) > 512:
        raise ValueError("Source certificate exceeds complete-plan budget")
    return certificates, source_refs, checks, retained


def _validate_bars(raw_by_direction, source_refs):
    if not isinstance(raw_by_direction, dict) or set(raw_by_direction) != set(_DIRECTIONS):
        raise ValueError("Exactly four complete physical directions required")
    if any(not isinstance(v, (list, tuple)) for v in raw_by_direction.values()):
        raise ValueError("Physical directions require bar arrays")
    if not 1 <= sum(map(len, raw_by_direction.values())) <= MAX_BARS:
        raise ValueError("Physical bar budget exceeded; no truncation allowed")
    seen_sources, result, contact_count = set(), {}, 0
    for direction in _DIRECTIONS:
        bars, identifiers = [], set()
        for raw in raw_by_direction[direction]:
            if not isinstance(raw, dict) or set(raw) != {
                    "id", "steel_class", "diameter_mm", "coordinate_mm", "longitudinal_mm", "source_bar_ids"}:
                raise ValueError("Unexpected physical bar fields")
            identifier = _label(raw["id"], "physical bar ID")
            if identifier in identifiers:
                raise ValueError("Duplicate physical bar ID inside direction")
            identifiers.add(identifier)
            steel = _label(raw["steel_class"], "physical steel class")
            diameter = _number(raw["diameter_mm"], "physical diameter", 6, 40, integer=True)
            coordinate = _number(raw["coordinate_mm"], "physical axis")
            interval = raw["longitudinal_mm"]
            if not isinstance(interval, (list, tuple)) or len(interval) != 2:
                raise ValueError("Physical bar needs two finite longitudinal endpoints")
            start, end = (_number(v, "physical endpoint") for v in interval)
            length = _number(end - start, "physical length", 100, 11700 + TOLERANCE_MM)
            if min(abs(length - value) for value in PLATE_11700_CUT_LENGTHS_MM) > TOLERANCE_MM:
                raise ValueError("Physical length outside original plate-11700 catalog")
            owners = raw["source_bar_ids"]
            if not isinstance(owners, (list, tuple)) or not 1 <= len(owners) <= MAX_BARS:
                raise ValueError("Every physical bar needs bounded source owners")
            refs = []
            for owner in owners:
                key = (direction, _label(owner, "source bar reference"))
                if key not in source_refs or key in seen_sources:
                    raise ValueError("Unknown or duplicate source bar reference")
                seen_sources.add(key)
                source = source_refs[key]
                cert = source["certificate"]
                if (steel != cert["steel_class"] or diameter < cert["diameter_mm"]
                        or abs(coordinate - source["coordinate"]) > TOLERANCE_MM):
                    raise ValueError("Physical bar changes source axis, weakens diameter or changes steel")
                lo, hi = cert["required_interval_mm"]
                if start > lo - 40 * diameter + TOLERANCE_MM or end < hi + 40 * diameter - TOLERANCE_MM:
                    raise ValueError("Physical bar does not preserve source demand plus full NEW-diameter 40d")
                background = cert["background_diameter_mm"]
                origin = cert["background_origin_mm"]
                distance = abs(math.remainder(coordinate - origin, 300))
                gap = distance - (diameter + background) / 2
                prior_gap = abs(math.remainder(source["coordinate"] - origin, 300)) - (
                    cert["diameter_mm"] + background) / 2
                if gap < -TOLERANCE_MM or abs(prior_gap) <= TOLERANCE_MM and abs(gap) > TOLERANCE_MM:
                    raise ValueError("Physical substitution penetrates background or breaks prescribed contact")
                contact_count += abs(prior_gap) <= TOLERANCE_MM
                refs.append(deepcopy(source["ref"]))
            bars.append({**deepcopy(raw), "longitudinal_mm": [start, end],
                         "source_refs": sorted(refs, key=lambda r: (r["zone_id"], r["component_index"], r["bar_index"]))})
        result[direction] = bars
    if seen_sources != source_refs.keys():
        raise ValueError("Physical plan omitted original source bars")
    return result, contact_count


def _runs(bars_by_direction, report):
    directions, locators = [], {}
    group_count = 0
    for direction, source in zip(_DIRECTIONS, report["directions"]):
        grouped = defaultdict(list)
        for bar in bars_by_direction[direction]:
            grouped[(bar["steel_class"], bar["diameter_mm"], *bar["longitudinal_mm"])].append(bar)
        runs = []
        along = 0 if direction.endswith("X") else 1
        for group_index, (group_key, bars) in enumerate(sorted(grouped.items())):
            group_id = f"{direction}:execution-{group_index}"
            group_count += 1
            phases = defaultdict(list)
            for bar in bars:
                phases[bar["coordinate_mm"] % 300].append(bar)
            chunks = []
            for _phase, members in sorted(phases.items()):
                ordered = sorted(members, key=lambda b: (b["coordinate_mm"], b["id"]))
                for bar in ordered:
                    if (not chunks or len(chunks[-1]) >= 1000
                            or bar["coordinate_mm"] - chunks[-1][-1]["coordinate_mm"] != 300
                            or (bar["coordinate_mm"] % 300) != (chunks[-1][0]["coordinate_mm"] % 300)):
                        chunks.append([])
                    chunks[-1].append(bar)
            for index, members in enumerate(chunks):
                steel, diameter, lo, hi = group_key
                a, b = [0.0, 0.0], [0.0, 0.0]
                a[along], b[along] = lo, hi
                a[1 - along] = b[1 - along] = members[0]["coordinate_mm"]
                identifier = f"{group_id}:run-{index}"
                for bar_index, bar in enumerate(members):
                    if a[1 - along] + 300 * bar_index != bar["coordinate_mm"]:
                        raise ValueError("Regular run changes physical axes")
                    locators[(direction, bar["id"])] = {"run_id": identifier, "bar_index": bar_index}
                runs.append({"id": identifier, "execution_group_id": group_id, "steel_class": steel,
                    "diameter_mm": diameter, "start_xy_mm": a, "end_xy_mm": b,
                    "bar_count": len(members), "spacing_mm": 300.0,
                    "bar_sources": [{"bar_id": bar["id"], "source_refs": bar["source_refs"]} for bar in members]})
        directions.append({"direction": direction, "source": source["source"]["filenames"]["dxf"], "runs": runs})
    if group_count > 512 or sum(len(d["runs"]) for d in directions) > 2000:
        raise ValueError("Complete physical transport exceeds execution-group/run limits")
    return directions, locators, group_count


def _joint_tasks(bars_by_direction, locators):
    tasks, checked = [], 0
    counts = {d: 0 for d in _DIRECTIONS}
    affected = set()
    for direction, bars in bars_by_direction.items():
        ordered = sorted(bars, key=lambda b: (b["coordinate_mm"], b["id"]))
        for index, first in enumerate(ordered):
            for second in ordered[index + 1:]:
                distance = second["coordinate_mm"] - first["coordinate_mm"]
                if distance >= 40:
                    break
                checked += 1
                if checked > MAX_PAIRS:
                    raise ValueError("Same-plane pair budget exceeded; no partial conflict report")
                overlap = min(first["longitudinal_mm"][1], second["longitudinal_mm"][1]) - max(
                    first["longitudinal_mm"][0], second["longitudinal_mm"][0])
                if (overlap <= TOLERANCE_MM
                        or distance >= (first["diameter_mm"] + second["diameter_mm"]) / 2 - TOLERANCE_MM):
                    continue
                counts[direction] += 1
                affected.update(((direction, first["id"]), (direction, second["id"])))
                if len(tasks) >= 10000:
                    raise ValueError("Complete manual-task inventory exceeds diagnostic packet budget")
                tasks.append({"id": f"manual-joint-{len(tasks) + 1}", "direction": direction,
                    "first": deepcopy(locators[(direction, first["id"])]),
                    "second": deepcopy(locators[(direction, second["id"])]),
                    "status": "unresolved", "kind": "body_intersection"})
    return tasks, {"status": "fail" if tasks else "pass", "body_intersection_count": len(tasks),
        "affected_physical_bar_count": len(affected), "direction_pair_counts": counts,
        "pair_checks": checked, "geometry_tolerance_mm": TOLERANCE_MM,
        "assumption": "same-plane-within-each-layer-and-axis", "actual_3d_checked": False}


def build_physical_bar_trial(
    source_report: dict, raw_bars_by_direction: dict, *, original_problem: PlateProblem,
    source_report_sha256: str, raw_report_sha256: str, candidate_index: int = 0,
    stock_time_limit_s: float = 10,
) -> PhysicalBarTrialResult:
    """Validate a complete normalized physical plan and return rollback-only data.

    Input hashes bind exact bytes read by the caller, not reserialized dictionaries.
    ``original_problem`` must come from fresh source ingestion, not report metadata.
    Full source coverage is recomputed here before checking every original axis and
    demand interval against its equal-or-thicker physical owner, including NEW 40d.
    The checker does not certify host, actual Z, structural safety or manual joints.
    """
    _digest(source_report_sha256)
    _digest(raw_report_sha256)
    if not isinstance(original_problem, PlateProblem):
        raise ValueError("Fresh typed original PlateProblem required")
    _number(candidate_index, "candidate index", 0, 100000, integer=True)
    _number(stock_time_limit_s, "stock-check time limit", 0.001, 60)
    # Bound dictionaries before costly geometry work, with no lossy serialization.
    for value in (source_report, raw_bars_by_direction):
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 32 * 1024 * 1024:
            raise ValueError("Physical trial input exceeds bounded JSON size")
    try:
        certificates, source_refs, coverage, retained, prior = _revalidate_sources(
            source_report, original_problem, candidate_index, source_report_sha256)
        bars, contacts = _validate_bars(raw_bars_by_direction, source_refs)
        directions, locators, group_count = _runs(bars, source_report)
    except (KeyError, IndexError, StopIteration, TypeError) as error:
        raise ValueError("Malformed source or physical plan; nothing exported") from error
    tasks, conflicts = _joint_tasks(bars, locators)
    groups = tuple(BarScheduleGroup(f"{direction}:{bar['id']}", bar["diameter_mm"],
        bar["longitudinal_mm"][1] - bar["longitudinal_mm"][0], 1, bar["steel_class"])
        for direction, members in bars.items() for bar in members)
    schedule = build_bar_schedule(groups)
    stock = check_stock_cutting(schedule, time_limit_s=stock_time_limit_s)
    if stock["status"] != "pass":
        raise ValueError("Physical whole-batch stock cutting did not independently pass")
    blockers = set(source_report["blocking_check_ids"])
    blockers.update(("host-boundary-cover-openings", "actual-3d-placement-and-layer-order",
                     "source-demand-and-40d-engineering-approval", "revit-readback", "permanent-placement"))
    if tasks:
        blockers.add("manual-joints-unresolved")
    if not 1 <= len(blockers) <= 100:
        raise ValueError("Invalid complete blocker inventory")
    for blocker in blockers:
        _label(blocker, "source blocker")
    expected = {"source_zone_count": len(certificates), "execution_group_count": group_count,
        "run_count": sum(len(d["runs"]) for d in directions), "physical_bar_count": len(groups),
        "additional_mass_kg": math.fsum(rebar_mass_kg(g.diameter_mm, g.installed_length_mm, 1) for g in groups),
        "position_count": len(schedule)}
    packet = {"schema_version": SCHEMA, "mode": "commit-readback-rollback", "units": "mm",
        "placement_eligible": False, "case_id": _label(source_report["case_id"], "case ID"),
        "source_report_sha256": source_report_sha256, "raw_report_sha256": raw_report_sha256,
        "source_zones": certificates, "directions": directions, "expected": expected,
        "manual_joint_tasks": tasks, "source_blockers": sorted(blockers)}
    packet_bytes = json.dumps(packet, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    if len(packet_bytes) > 8 * 1024 * 1024:
        raise ValueError("Complete physical packet exceeds 8 MiB")
    review = {"schema_version": "physical-bar-plan-review/v1", "units": "mm", "placement_eligible": False,
        "engineering_approval": False, "source_report_sha256": source_report_sha256,
        "raw_report_sha256": raw_report_sha256, "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "expected": deepcopy(expected), "prior_metrics": deepcopy(prior), "source_layout_zones": retained,
        "source_demand_preserved": True, "source_original_coverage": coverage,
        "source_bar_reference_count": len(source_refs), "source_axis_and_new40d_status": "pass",
        "source_background_contact_count": contacts, "background_contact_and_penetration_status": "pass",
        "same_plane_conflicts": conflicts, "stock_cutting": stock, "bar_schedule": to_jsonable(schedule),
        "manual_joint_tasks": deepcopy(tasks), "permanent_blockers": sorted(blockers),
        "not_checked": ["actual-revit-host-and-openings", "actual-z-and-cross-direction-3d-collisions",
                        "structural-and-anchorage-engineering-approval", "manual-joint-design", "revit-readback"],
        "semantics": "Source parameterized zones are unchanged; execution groups are NOT LayoutZones. "
                     "Required source intervals exclude anchorage; physical owners retain full NEW diameter 40d. "
                     "Stock is checked under explicit 11700 mm, mixed-length, zero-kerf research assumptions."}
    return PhysicalBarTrialResult(packet, review)
