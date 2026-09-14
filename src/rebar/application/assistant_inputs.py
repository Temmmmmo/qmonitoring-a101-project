"""Fresh four-DXF inputs and explicit source-candidate selection for the assistant.

This layer chooses a complete uniform starting point, not a final fabrication plan.
Physical normalization and all subsequent independent checks are a separate service.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path

from rebar.golden import get_engineer_reference_case
from rebar.golden.dataset_inventory import extract_engineer_reference_metrics
from rebar.golden.sources import resolve_engineer_reference_files
from rebar.optimization import ComplexityAxis, PlateProblem, PlateSolution
from rebar.reporting.serialization import to_jsonable

from .analyze_plate import PlateAnalysis, PlateDirectionSource, analyze_plate
from .genetic_benchmark import GeneticRunConfig
from .layout_snapshot import CASE_MAPPINGS


@dataclass(frozen=True)
class AssistantSourceSelection:
    """Typed complete demand and selected source geometry, with input hashes."""

    problem: PlateProblem
    solution: PlateSolution
    provenance: dict
    engineer_reference: dict | None = None


def source_record(path: str | Path, *, role: str) -> dict:
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("Source must be a regular local file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path), "sha256": digest.hexdigest(), "role": role}


def verify_source_records(records: list[dict]) -> None:
    for record in records:
        if source_record(record["path"], role=record["role"]) != record:
            raise ValueError(f"Source changed during calculation: {record['path']}")


def assistant_genetic_config(*, population: int = 8, generations: int = 3,
                             seed: int = 7) -> GeneticRunConfig:
    for value, lower, upper, name in ((population, 4, 128, "population"),
        (generations, 1, 100, "generations"), (seed, 0, 2147483647, "seed")):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{name} must be an integer in {lower}..{upper}")
    return GeneticRunConfig(population_size=population, generations=generations,
        random_seed=seed, complexity_axis=ComplexityAxis.PHYSICAL_BAR_COUNT,
        operator_policy="uniform", pool_polish="milp", pool_polish_solves=6,
        pool_polish_time_s=10, recombination_variants=1500)


def select_source_candidate(analysis: PlateAnalysis, *, maximum_source_bars: int | None = None,
                            maximum_source_mass_kg: float | None = None):
    """Minimum source mass under explicit optional source limits; no hidden fallback.

    Limits apply to this pre-pattern starting point only. Final metrics must be
    recalculated after physical patterns, normalization and stock balancing.
    """
    if maximum_source_bars is not None and (type(maximum_source_bars) is not int or maximum_source_bars < 1):
        raise ValueError("maximum_source_bars must be a positive integer")
    if maximum_source_mass_kg is not None and (
            isinstance(maximum_source_mass_kg, bool) or not isinstance(maximum_source_mass_kg, (int, float))
            or not math.isfinite(maximum_source_mass_kg) or maximum_source_mass_kg <= 0):
        raise ValueError("maximum_source_mass_kg must be finite and positive")
    if analysis.front is None:
        raise ValueError("No complete source Pareto front was returned")
    candidates = []
    for candidate in analysis.front.candidates:
        metrics = candidate.solution.metrics
        if (not candidate.solution.valid or metrics.under_reinforced_cell_count
                or not math.isfinite(metrics.total_mass_kg)):
            continue
        if maximum_source_bars is not None and metrics.physical_bar_count > maximum_source_bars:
            continue
        if maximum_source_mass_kg is not None and metrics.total_mass_kg > maximum_source_mass_kg:
            continue
        candidates.append(candidate)
    if not candidates:
        raise ValueError("No complete source candidate satisfies the explicit source limits; limits were not relaxed")
    return min(candidates, key=lambda c: (c.solution.metrics.total_mass_kg,
                                          c.solution.metrics.physical_bar_count, c.id))


def analyze_assistant_sources(sources: tuple[PlateDirectionSource, ...], *, case_id: str,
                              config: GeneticRunConfig | None = None,
                              maximum_source_bars: int | None = None,
                              maximum_source_mass_kg: float | None = None) -> AssistantSourceSelection:
    """Read all original DXF, run recovery GA and choose a complete starting point."""
    if len(sources) != 4:
        raise ValueError("Exactly four DXF sources are required")
    if not isinstance(case_id, str) or not case_id.strip() or len(case_id) > 120:
        raise ValueError("Explicit nonempty case_id up to 120 characters required")
    config = config or assistant_genetic_config()
    # Validate the bounded public controls even when the typed config is supplied.
    assistant_genetic_config(population=config.population_size, generations=config.generations,
                             seed=config.random_seed)
    records, used_paths = [], set()
    for source in sources:
        path = Path(source.dxf_path).resolve(strict=True)
        if path.suffix.lower() != ".dxf" or path in used_paths:
            raise ValueError("Each direction requires a distinct DXF file")
        used_paths.add(path)
        records.append(source_record(path, role="dxf"))
        if source.shk_path is not None:
            shk = Path(source.shk_path).resolve(strict=True)
            if shk.suffix.lower() != ".shk":
                raise ValueError("Explicit legend must be a SHK file")
            record = source_record(shk, role="shk")
            if record not in records:
                records.append(record)
    analysis = analyze_plate(sources, algorithm_names=("genetic-source-recovery",),
        complexity_axis=ComplexityAxis.PHYSICAL_BAR_COUNT,
        algorithm_params={"genetic-source-recovery": config.algorithm_params()},
        case_id=case_id, cutting_profile="plate-11700", min_width_cells=2, single_cell_policy="preserve")
    selected = select_source_candidate(analysis, maximum_source_bars=maximum_source_bars,
                                        maximum_source_mass_kg=maximum_source_mass_kg)
    verify_source_records(records)
    provenance = {"schema_version": "assistant-source-selection/v1", "mode": "fresh-four-dxf",
        "case_id": case_id, "source_files": records,
        "source_mappings": [{"dxf": str(Path(s.dxf_path).resolve()), "mapping_id": s.mapping_id,
            "shk": str(Path(s.shk_path).resolve()) if s.shk_path is not None else None} for s in sources],
        "algorithm": "genetic-source-recovery", "config": to_jsonable(config),
        "candidate_id": selected.id, "source_candidate_count": len(analysis.front.candidates),
        "selection": {"policy": "minimum_source_mass_with_explicit_limits",
            "maximum_source_bars": maximum_source_bars, "maximum_source_mass_kg": maximum_source_mass_kg,
            "scope": "uniform source candidate; not a final physical-plan gate"},
        "source_metrics": to_jsonable(selected.solution.metrics), "placement_eligible": False}
    return AssistantSourceSelection(analysis.problem, selected.solution, provenance)


def analyze_assistant_case(case_id: str, materials_root: str | Path, *,
                           config: GeneticRunConfig | None = None,
                           maximum_source_bars: int | None = None,
                           maximum_source_mass_kg: float | None = None,
                           use_engineer_bar_limit: bool = True) -> AssistantSourceSelection:
    """Convenience for three explicitly mapped cases, with freshly checked PDF totals."""
    if case_id not in CASE_MAPPINGS:
        raise ValueError("No verified mapping/reference for this case")
    if type(use_engineer_bar_limit) is not bool:
        raise ValueError("use_engineer_bar_limit must be explicit boolean")
    case = get_engineer_reference_case(case_id)
    files = resolve_engineer_reference_files(case, Path(materials_root))
    input_id = case.input_sets[0].id
    pdf_record = source_record(files.engineer_pdf, role="engineer-pdf")
    reference = extract_engineer_reference_metrics(case, files.engineer_pdf)
    if (abs(reference.total_mass_kg - case.expected_mass_kg) > 0.01
            or reference.bar_count != case.expected_bar_count
            or reference.position_count != case.expected_position_count):
        raise ValueError("Fresh PDF totals differ from the verified matched engineer reference")
    if maximum_source_bars is None and use_engineer_bar_limit:
        maximum_source_bars = reference.bar_count
    selected = analyze_assistant_sources(tuple(PlateDirectionSource(path, mapping_id=CASE_MAPPINGS[case_id])
        for path in files.dxf_by_input_set[input_id].values()), case_id=case_id, config=config,
        maximum_source_bars=maximum_source_bars, maximum_source_mass_kg=maximum_source_mass_kg)
    verify_source_records([pdf_record])
    engineer = {"mass_kg": reference.total_mass_kg, "physical_bar_count": reference.bar_count,
        "specification_rows": reference.position_count, "source_pdf": pdf_record,
        "scope": "matched additional reinforcement; source bar limit is not final gate approval"}
    provenance = {**selected.provenance, "mode": "fresh-matched-case", "input_set_id": input_id,
        "source_files": [*selected.provenance["source_files"], pdf_record], "engineer_reference": engineer}
    return AssistantSourceSelection(selected.problem, selected.solution, provenance, engineer)
