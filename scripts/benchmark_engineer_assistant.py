"""Reproduce a complete preserved-demand assistant/engineer comparison.

Reads a known four-DXF case and independently extracts its PDF quantities. Writes
a new local snapshot accepted by export_layout_plate_trial.py. No source or RVT
is modified. Coverage feasibility is not permission to issue reinforcement.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rebar.application.analyze_plate import PlateDirectionSource, analyze_plate
from rebar.application.gate_assessment import assess_plate_gates
from rebar.application.genetic_benchmark import GeneticRunConfig
from rebar.application.layout_snapshot import CASE_MAPPINGS
from rebar.golden import get_engineer_reference_case, plate_metric_reference_from_engineer
from rebar.golden.dataset_inventory import extract_engineer_reference_metrics
from rebar.golden.sources import resolve_engineer_reference_files
from rebar.optimization import ComplexityAxis
from rebar.reporting.serialization import to_jsonable


def code_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src" / "rebar").rglob("*.py")):
        digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def source_record(path: Path) -> dict:
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def select_candidates(candidates: list[dict], engineer_mass: float, engineer_bars: int) -> dict:
    """Explicit economic preferences; never erase the other engineering checks."""
    valid = [c for c in candidates if c["valid"] and c["metrics"]["under_reinforced_cell_count"] == 0]
    mass_pass = [c for c in valid if c["metrics"]["total_mass_kg"] <= 1.15 * engineer_mass]
    bar_pass = [c for c in valid if c["metrics"]["physical_bar_count"] <= engineer_bars]

    def choose(items, *fields):
        if not items:
            return None
        return min(items, key=lambda c: (*[c["metrics"][field] for field in fields], c["id"]))["id"]

    return {
        "minimum_mass": choose(valid, "total_mass_kg", "physical_bar_count"),
        "minimum_bars_under_15pct_mass": choose(mass_pass, "physical_bar_count", "total_mass_kg"),
        "minimum_mass_without_extra_bars": choose(bar_pass, "total_mass_kg", "physical_bar_count"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=tuple(CASE_MAPPINGS))
    parser.add_argument("--materials-root", type=Path, default=ROOT / "Дополнительные материалы")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--algorithm", choices=("genetic-pareto", "genetic-source-recovery"),
                        default="genetic-source-recovery")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--population", type=int, default=8)
    parser.add_argument("--generations", type=int, default=3)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Output already exists; choose a new JSON path")
    if args.output.suffix.lower() != ".json":
        parser.error("Output must be a JSON snapshot")
    if not 4 <= args.population <= 128 or not 1 <= args.generations <= 100:
        parser.error("population must be 4..128, generations 1..100")

    case = get_engineer_reference_case(args.case)
    files = resolve_engineer_reference_files(case, args.materials_root)
    input_id = case.input_sets[0].id
    paths = tuple(files.dxf_by_input_set[input_id].values())
    source_pdf = source_record(files.engineer_pdf)
    source_dxf = [source_record(path) for path in paths]
    reference = extract_engineer_reference_metrics(case, files.engineer_pdf)
    if (abs(reference.total_mass_kg - case.expected_mass_kg) > 0.01
            or reference.bar_count != case.expected_bar_count
            or reference.position_count != case.expected_position_count):
        raise ValueError("Fresh PDF quantities differ from the verified engineer reference")
    print(f"PDF verified: {case.id}; {reference.total_mass_kg:.2f} kg; {reference.bar_count} bars", flush=True)
    config = GeneticRunConfig(
        population_size=args.population, generations=args.generations, random_seed=args.seed,
        complexity_axis=ComplexityAxis.PHYSICAL_BAR_COUNT, operator_policy="uniform",
        pool_polish="milp", pool_polish_solves=6, pool_polish_time_s=10,
        recombination_variants=1500,
        baseline_seed_algorithms=("agglomerative", "bsp", "greedy-priority"),
    )
    before = code_digest()
    started = perf_counter()
    analysis = analyze_plate(
        tuple(PlateDirectionSource(path, mapping_id=CASE_MAPPINGS[case.id]) for path in paths),
        algorithm_names=(args.algorithm,), complexity_axis=config.complexity_axis,
        algorithm_params={args.algorithm: config.algorithm_params()}, case_id=case.id,
        cutting_profile="plate-11700", min_width_cells=2, single_cell_policy="preserve",
    )
    elapsed = perf_counter() - started
    after = code_digest()
    if before != after:
        raise ValueError("Core source changed during calculation; snapshot not published, rerun on stable code")
    if source_record(files.engineer_pdf) != source_pdf or [source_record(p) for p in paths] != source_dxf:
        raise ValueError("Input changed during calculation; snapshot not published")
    metric_reference = plate_metric_reference_from_engineer(case)
    candidates = []
    for candidate in analysis.front.candidates:
        metrics = candidate.solution.metrics
        candidates.append({
            "id": candidate.id, "valid": candidate.solution.valid,
            "metrics": to_jsonable(metrics), "constructability": to_jsonable(candidate.constructability),
            "mass_delta_pct": (metrics.total_mass_kg / reference.total_mass_kg - 1) * 100,
            "bar_delta_pct": (metrics.physical_bar_count / reference.bar_count - 1) * 100,
            "mass_threshold_15pct_met": metrics.total_mass_kg <= 1.15 * reference.total_mass_kg,
            "gates": to_jsonable(assess_plate_gates(analysis.problem, candidate.solution, reference=metric_reference)),
            "solution": to_jsonable(candidate.solution),
        })
    selection = select_candidates(candidates, reference.total_mass_kg, reference.bar_count)
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        head = None
    types = {(p.diameter_mm, p.length_mm) for _, sheet in reference.sheets for p in sheet.positions}
    payload = {
        "case_id": case.id, "input_set_id": input_id, "algorithm": args.algorithm,
        "config": to_jsonable(config), "engineering_profile": {
            "single_cell_policy": "preserve", "cutting_profile": "plate-11700",
            "min_width_cells": 2, "host_supplied": False,
        }, "elapsed_s": elapsed, "source_pdf": source_pdf, "source_dxf": source_dxf,
        "working_tree_head": head, "code_sha256": before, "code_sha256_after": after,
        "engineer": {"mass_kg": reference.total_mass_kg, "physical_bar_count": reference.bar_count,
                     "specification_rows": reference.position_count,
                     "unique_diameter_length_pairs": len(types), "sheets": to_jsonable(reference.sheets)},
        "problems": to_jsonable(analysis.problem), "candidates": candidates, "selection": selection,
        "representative_solutions": to_jsonable(analysis.solutions), "placement_eligible": False,
        "scope": "Full original-demand uniform-layout comparison; actual patterns, batch cutting and host are separate checks",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, allow_nan=False, indent=2)
    for preference, candidate_id in selection.items():
        candidate = next((c for c in candidates if c["id"] == candidate_id), None)
        print(preference, None if candidate is None else {
            "id": candidate_id, "mass_kg": candidate["metrics"]["total_mass_kg"],
            "physical_bars": candidate["metrics"]["physical_bar_count"],
            "mass_delta_pct": candidate["mass_delta_pct"], "bar_delta_pct": candidate["bar_delta_pct"],
        }, flush=True)
    print(f"Complete snapshot: {args.output}; {elapsed:.1f} s. Not a placement authorization.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
