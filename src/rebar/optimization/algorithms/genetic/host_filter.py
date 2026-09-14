"""Opt-in candidate screening; never clip the original demand to a host.

The guard is a caller-owned, deterministic, read-only predicate on a fully
detailed LayoutZone. This module knows nothing about Revit, 40d or a particular
physical placement policy. It screens EVERY candidate origin and rechecks final
materialized zones because phase repair can change their actual geometry.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING, Callable

from ...contracts import LayoutProblem, LayoutZone
from .coverage import demand_fragments

if TYPE_CHECKING:
    from ..genetic_pareto import _SearchSpace

CandidateGuard = Callable[[LayoutProblem, LayoutZone], bool]


def _check(problem: LayoutProblem, zone: LayoutZone, guard: CandidateGuard) -> tuple[bool, str, str | None]:
    try:
        value = guard(problem, zone)
    except Exception as error:
        # No candidate with an unknown geometric check is admitted. Keyboard
        # interrupts / process termination are not swallowed.
        return False, "guard_exception", f"{type(error).__name__}: {error}"[:1000]
    if type(value) is not bool:
        return False, "guard_returned_non_boolean", type(value).__name__
    return value, "accepted" if value else "guard_rejected", None


def missing_atoms(space: _SearchSpace) -> dict[str, object]:
    """Finite-pool necessary coverage condition, NOT global infeasibility proof."""
    if len(space.leaves) != space.leaf_count:
        raise ValueError("Guarded search requires every original coverage atom")
    covered = set()
    for candidate in space.candidates:
        if any(type(index) is not int or not 0 <= index < space.leaf_count for index in candidate.leaf_ids):
            raise ValueError("Candidate references an unknown original coverage atom")
        covered.update(candidate.leaf_ids)
    missing = sorted(set(range(space.leaf_count))-covered)
    return {
        "coverage_atom_count": space.leaf_count,
        "missing_atom_count": len(missing),
        "missing_atom_ids": missing,
        "missing_source_cell_ids": sorted({cell for index in missing for cell in space.leaves[index].source_cell_ids}),
        "missing_atoms": [{"atom_id": index, "bbox_mm": space.leaves[index].bbox,
            "required_level_index": space.leaves[index].level_index,
            "source_cell_ids": space.leaves[index].source_cell_ids} for index in missing],
        "all_original_atoms_have_a_candidate": not missing,
        "scope": "necessary_coverage_condition_for_this_finite_candidate_pool_only",
        "source_demand_removed": False,
    }


def filter_candidate_space(problem: LayoutProblem, space: _SearchSpace, guard: CandidateGuard,
                           *, stage: str) -> tuple[_SearchSpace, dict[str, object]]:
    """Filter all origins, remap chromosomes, preserve EVERY atom and source FE.

    Partially filtered seeds may still seed ordinary repair. A baseline loses its
    protected/exact status if any of its rectangles was rejected: otherwise its
    shortened chromosome would bypass both coverage repair and phase materialization.
    """
    if not callable(guard):
        raise TypeError("candidate_guard must be callable")
    remap, kept, rejected = {}, [], []
    origin_before, origin_after, reasons = Counter(), Counter(), Counter()
    for old_index, candidate in enumerate(space.candidates):
        origin_before.update(candidate.origins)
        accepted, reason, detail = _check(problem, candidate.rectangle.zone, guard)
        reasons[reason] += 1
        if accepted:
            remap[old_index] = len(kept)
            kept.append(candidate)
            origin_after.update(candidate.origins)
        else:
            rejected.append({"candidate_index_before_filter": old_index,
                "zone_id": candidate.rectangle.zone.id, "origins": sorted(candidate.origins),
                "source_cell_ids": candidate.source_cell_ids, "atom_ids": sorted(candidate.leaf_ids),
                "reason": reason, "detail": detail})
    def remap_genome(genome):
        if any(type(i) is not int or not 0 <= i < len(space.candidates) for i in genome):
            raise ValueError("Seed chromosome references an unknown original candidate")
        return frozenset(remap[i] for i in genome if i in remap)
    seeds = tuple(dict.fromkeys(remap_genome(genome) for genome in space.seed_genomes))
    baselines, algorithms, metrics, dropped_baselines = [], [], [], []
    for genome, algorithm, metric in zip(space.baseline_seed_genomes, space.baseline_seed_algorithms,
                                       space.baseline_seed_metrics, strict=True):
        mapped = remap_genome(genome)
        if len(mapped) != len(genome):
            dropped_baselines.append(algorithm)
        else:
            baselines.append(mapped)
            algorithms.append(algorithm)
            metrics.append(metric)
    result = replace(space, candidates=tuple(kept), seed_genomes=seeds,
        baseline_seed_genomes=tuple(baselines), baseline_seed_algorithms=tuple(algorithms),
        baseline_seed_metrics=tuple(metrics))
    return result, {"stage": stage, "candidate_count_before": len(space.candidates),
        "candidate_count_after": len(kept), "rejected_candidate_count": len(rejected),
        "reason_counts": dict(sorted(reasons.items())),
        "origin_counts_before": dict(sorted(origin_before.items())),
        "origin_counts_after": dict(sorted(origin_after.items())),
        "rejected_candidates": rejected, "candidate_index_remap": sorted(remap.items()),
        "seed_count_before": len(space.seed_genomes), "seed_count_after": len(seeds),
        "exact_baselines_dropped": dropped_baselines, "atoms_preserved_unchanged": result.leaves is space.leaves,
        "coverage": missing_atoms(result)}


def preserve_atom_boundaries(problem: LayoutProblem, before: _SearchSpace, expanded: _SearchSpace) -> _SearchSpace:
    """Recombination may refine atoms, never discard an old fragment boundary.

    The ordinary recombination builder uses its current candidate bboxes. Once
    some candidates have been filtered, that alone could coarsen earlier atoms.
    Include their original boundaries explicitly; source demand is still read
    from the exact same problem, never from the retained candidate geometry.
    """
    from ..genetic_pareto import _leaf_ids_for_rectangle
    boundaries = tuple(atom.bbox for atom in before.leaves) + tuple(
        candidate.rectangle.zone.demand_bbox for candidate in expanded.candidates)
    leaves = demand_fragments(problem, expanded.grid, boundaries)
    before_sources = {identifier for atom in before.leaves for identifier in atom.source_cell_ids}
    after_sources = {identifier for atom in leaves for identifier in atom.source_cell_ids}
    if before_sources != after_sources or len(leaves) < len(before.leaves):
        raise ValueError("Guarded recombination lost original FE atoms or their source references")
    return replace(expanded, leaves=leaves, leaf_count=len(leaves), candidates=tuple(
        replace(candidate, leaf_ids=_leaf_ids_for_rectangle(candidate.rectangle, leaves))
        for candidate in expanded.candidates))


def check_materialized_zones(problem: LayoutProblem, zones: tuple[LayoutZone, ...],
                             guard: CandidateGuard) -> dict[str, object]:
    rejected, reasons = [], Counter()
    for index, zone in enumerate(zones):
        accepted, reason, detail = _check(problem, zone, guard)
        reasons[reason] += 1
        if not accepted:
            rejected.append({"zone_index": index, "zone_id": zone.id,
                "source_cell_ids": tuple(zone.meta.get("seed_cell_ids", zone.covered_cell_ids)),
                "reason": reason, "detail": detail})
    return {"stage": "after_materialization_and_phase_repair", "checked_zone_count": len(zones),
        "passed": not rejected, "rejected_zone_count": len(rejected), "rejected_zones": rejected,
        "reason_counts": dict(sorted(reasons.items()))}
