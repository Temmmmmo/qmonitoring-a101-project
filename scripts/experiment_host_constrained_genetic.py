"""Filter full GA candidate geometry against the actual host BEFORE evolution.

The original FE demand is never clipped. Full40d is an explicit control model,
not a normative universal free-edge rule. Output is research, not a Revit packet.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from correct_small_openings import _code_digest, _load_recovery
from rebar.application.analyze_composite_plate import CompositeDirectionSettings, _placements
from rebar.application.assistant_inputs import assistant_genetic_config, source_record, verify_source_records
from rebar.application.layout_snapshot import load_layout_snapshot
from rebar.application.patterned_layout_recovery import recover_patterned_layout
from rebar.application.physical_layout_recovery import _bytes
from rebar.application.working_host import load_working_host_json
from rebar.application.working_solid_host import MAX_WORKING_REPORT_BYTES, inspect_working_solid
from rebar.optimization.algorithms.genetic_pareto import GeneticParetoOptimizer
from rebar.optimization.contracts import AlgorithmRequest, PlateDirectionSolution, SolutionStatus
from rebar.optimization.contracts.physical import PhysicalBar
from rebar.optimization.services.axis_patterns import pattern_coordinates
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.composite_windows import covering_composite_window
from rebar.optimization.services.host_search_domain import (
    optimistic_host_reachability, prepare_uniform_zone_host_guard,
)
from rebar.optimization.services.opening_relocation import collision_pairs, contained, material_and_holes
from rebar.optimization.services.plate import build_plate_solution
from rebar.reporting.serialization import to_jsonable


def patterned_bars(zones, steel):
    return tuple(PhysicalBar(f"{z.id}/{c.component_index}/{i}", z.direction, steel,
        c.rebar.diameter, coordinate, c.longitudinal_interval_mm, (f"{z.id}/{c.component_index}/{i}",))
        for z in zones for c in z.components
        for i, coordinate in enumerate(pattern_coordinates(c.placement, c.axis_window_mm)))


def make_guard(problem, host, settings):
    """Require BOTH source uniform geometry and its declared STO conversion to fit.

    No claim that independently fitting zones are mutually collision-free or that
    the later stock balancing preserves host containment; the full result is checked.
    """
    uniform = prepare_uniform_zone_host_guard(problem, host)
    profile = dict(_placements(problem.demand, settings))
    material, _, _ = material_and_holes(host)
    cache, stats = {}, Counter()

    def guard(actual_problem, zone):
        if actual_problem is not problem:
            raise ValueError("Guard is bound to the exact original problem")
        stats["calls"] += 1
        if not uniform(zone):
            stats["uniform_host_rejected"] += 1
            return False
        key = zone.demand_bbox, zone.level_index
        if key not in cache:
            try:
                bounds = covering_composite_window(problem.demand, zone.demand_bbox, zone.level_index,
                    profile[zone.level_index], constraints=problem.constraints)
                patterned = build_composite_zone(problem.demand, bounds, zone.level_index,
                    "candidate", profile[zone.level_index], constraints=problem.constraints)
            except ValueError:
                # Unsupported/too-long finite proposals are discarded, never repaired by clipping.
                cache[key] = False
                stats["pattern_geometry_rejected"] += 1
            else:
                cache[key] = all(contained(b, material, host.side_cover_mm)
                    for b in patterned_bars((patterned,), settings.steel_class))
        if not cache[key]:
            stats["pattern_host_or_geometry_rejected"] += 1
        return cache[key]
    return guard, stats


def run(args):
    if args.output.exists():
        raise ValueError("Output exists; choose a NEW file")
    if args.confirm_identity_xy is not True:
        raise ValueError("Explicit --confirm-identity-xy required for this experiment")
    if not math.isfinite(args.time_limit_s) or not 0 < args.time_limit_s <= 600:
        raise ValueError("Direction time limit must be finite in (0,600]")
    if not 1 <= args.maximum_pool_merges <= 5000 or not 0 <= args.recombination_variants <= 5000:
        raise ValueError("Candidate expansion resource bounds are invalid")
    config = replace(assistant_genetic_config(population=args.population, generations=args.generations,
        seed=args.seed), maximum_pool_merges=args.maximum_pool_merges,
        recombination_variants=args.recombination_variants)
    started, code_before = perf_counter(), _code_digest()
    script_record = source_record(Path(__file__), role="experiment-code")
    loaded = load_layout_snapshot(args.snapshot, candidate_id=args.candidate_id)
    recovery, summary, records = _load_recovery(args, loaded)
    snapshot_record = source_record(args.snapshot, role="source-snapshot")
    if snapshot_record["sha256"] != loaded.source_sha256:
        raise ValueError("Snapshot changed after restoration")
    host_record = source_record(args.working_host_report, role="working-host-snapshot")
    if summary["working_host_source"]["sha256"] != host_record["sha256"]:
        raise ValueError("Host differs from the recorded working pipeline")
    records.extend((script_record, snapshot_record, host_record))
    with args.working_host_report.open("rb") as stream:
        content = stream.read(MAX_WORKING_REPORT_BYTES+1)
    if len(content) > MAX_WORKING_REPORT_BYTES or hashlib.sha256(content).hexdigest() != host_record["sha256"]:
        raise ValueError("Host exceeds limit or changed while reading")
    host, _ = inspect_working_solid(load_working_host_json(content, maximum_bytes=MAX_WORKING_REPORT_BYTES))
    raw_settings = {str(p.demand.direction): row["settings"] for p, row in
        zip(loaded.problem.direction_problems, recovery.patterned_report["directions"], strict=True)}
    rows, selected, settings = [], [], []
    for problem in loaded.problem.direction_problems:
        direction = problem.demand.direction
        raw = raw_settings[str(direction)]
        if raw["direction"] != to_jsonable(direction):
            raise ValueError("Patterned settings order differs from original directions")
        setting = CompositeDirectionSettings(**{**raw, "direction": direction})
        settings.append(setting)
        print("Original full demand / host search:", direction, flush=True)
        bound = optimistic_host_reachability(problem, host)
        guard, stats = make_guard(problem, host, setting)
        request = AlgorithmRequest(params=config.algorithm_params(), time_limit_s=args.time_limit_s)
        solutions = GeneticParetoOptimizer(candidate_guard=None if args.control_unbounded else guard).solve_many(problem, request)
        valid = [s for s in solutions if s.status in (SolutionStatus.FEASIBLE, SolutionStatus.OPTIMAL)
                 and s.metrics.under_reinforced_cell_count == 0]
        chosen = min(valid, key=lambda s: (s.metrics.total_mass_kg, s.metrics.physical_bar_count)) if valid else None
        if chosen is not None:
            selected.append(PlateDirectionSolution(direction, chosen))
        rows.append({"direction": str(direction), "optimistic_reachability": bound,
            "guard_calls": dict(stats), "full_source_solution_found": chosen is not None,
            "solutions": [to_jsonable(s) for s in solutions]})
        print(json.dumps({"direction": str(direction), "full_solutions": len(valid),
            "guard_calls": dict(stats), "statuses": [str(s.status.value) for s in solutions]}, ensure_ascii=False), flush=True)
    physical = None
    if len(selected) == 4:
        plate = build_plate_solution(selected)
        rebuilt = recover_patterned_layout(loaded.problem, plate, tuple(settings))
        bars = tuple(b for zones, setting in zip(rebuilt.direction_zones, settings, strict=True)
                     for b in patterned_bars(zones, setting.steel_class))
        material, _, _ = material_and_holes(host)
        outside = sum(not contained(b, material, host.side_cover_mm) for b in bars)
        pairs = collision_pairs(bars)
        point = (rebuilt.report["front"] or rebuilt.report["diagnostic_front_before_cutting"])[0]
        physical = {"patterned_report": rebuilt.report, "host_blocked_bar_count": outside,
            "same_direction_body_pairs": len(pairs), "full_physical_checks_passed":
                rebuilt.stock_balanced and outside == 0 and not pairs,
            "comparison_to_matched_engineer": {
                "additional_mass_kg": point["additional_mass_kg"], "physical_bar_count": point["physical_bar_count"],
                "mass_delta_pct": (point["additional_mass_kg"]/loaded.engineer_comparison["mass_kg"]-1)*100,
                "bar_delta_pct": (point["physical_bar_count"]/loaded.engineer_comparison["physical_bar_count"]-1)*100,
                "scope": "complete patterned batch only, NOT all engineering gates"}}
    report = {"schema_version": "host-constrained-genetic-experiment/v1", "placement_eligible": False,
        "engineering_approval": False, "structural_placement_supported": False,
        "status": "no_complete_candidate" if len(selected) != 4 else "complete_source_candidate_physically_rechecked",
        "control_unbounded": args.control_unbounded, "source_demand_removed": False, "source_demand_values_changed": False,
        "source_to_revit_xy_mm": [0, 0], "case_id": loaded.problem.case_id, "config": to_jsonable(config),
        "source_files": records, "code_sha256": code_before, "directions": rows, "physical_result": physical,
        "runtime_s": perf_counter()-started,
        "warning": "Full40d is a CONTROL APPROXIMATION, not a universal normative rule from every edge FE. No partial mass comparison or Revit placement.",
        "not_checked": ["normative_edge_anchorage", "actual_Z_3D_existing_rebar", "engineering_acceptance", "Revit_readback"]}
    verify_source_records(records)
    if _code_digest() != code_before:
        raise ValueError("Code changed during experiment")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(_bytes(report))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("pipeline-dir", "snapshot", "working-host-report", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--confirm-identity-xy", action="store_true")
    parser.add_argument("--control-unbounded", action="store_true")
    parser.add_argument("--population", type=int, default=8)
    parser.add_argument("--generations", type=int, default=3)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--maximum-pool-merges", type=int, default=128)
    parser.add_argument("--recombination-variants", type=int, default=0)
    parser.add_argument("--time-limit-s", type=float, default=60)
    try:
        report = run(parser.parse_args(argv))
    except (ValueError, TypeError, KeyError, OSError) as error:
        parser.error(str(error))
    return 0 if report["physical_result"] and report["physical_result"]["full_physical_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
