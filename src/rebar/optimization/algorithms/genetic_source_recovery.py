"""Opt-in GA proposals followed by hard recovery of the unchanged source demand.

The legacy single-cell rule is only a proposal heuristic. It is not accepted as
averaging, and no proposal earns a feasible result against its reduced input.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import replace
from time import perf_counter

from ..contracts import (
    AlgorithmRequest,
    LayoutMetrics,
    LayoutProblem,
    LayoutSolution,
    SolutionStatus,
)
from ..services.cutting import CutLengthInfeasibleError
from ..services.preprocessing import apply_single_cell_rule
from ..services.source_revalidation import revalidate_source_demand
from .genetic_pareto import GeneticParetoOptimizer
from .source_recovery import recover_source_demand


def _repair_parameters(request: AlgorithmRequest) -> tuple[tuple[float, ...], float, int]:
    raw_penalties = request.params.get("source_recovery_bar_penalties", (0.0, 2.0))
    if not isinstance(raw_penalties, (tuple, list)) or not 1 <= len(raw_penalties) <= 8:
        raise ValueError("source_recovery_bar_penalties requires 1..8 numeric penalties")
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value < 0 for value in raw_penalties):
        raise ValueError("source recovery bar penalties must be finite and nonnegative")
    penalties = tuple(dict.fromkeys(float(value) for value in raw_penalties))
    position_penalty = request.params.get("source_recovery_position_penalty_kg", 0.0)
    if (isinstance(position_penalty, bool) or not isinstance(position_penalty, (int, float))
            or not math.isfinite(position_penalty) or position_penalty < 0):
        raise ValueError("source recovery position penalty must be finite and nonnegative")
    neighbors = request.params.get("source_recovery_neighbor_count", 6)
    if type(neighbors) is not int or not 1 <= neighbors <= 64:
        raise ValueError("source_recovery_neighbor_count must be an integer in 1..64")
    return penalties, float(position_penalty), neighbors


def _geometry_key(solution: LayoutSolution) -> tuple:
    # IDs/coverage caches/proposal ancestry are not separate physical candidates.
    return tuple(sorted((zone.demand_bbox, zone.bbox, zone.level_index,
                         zone.rebar.diameter, zone.rebar.step, zone.bar_count)
                        for zone in solution.zones))


class GeneticSourceRecoveryOptimizer:
    """Generate old-style shapes, then recover/validate on the original source.

    ``solve_many`` returns only source-hard-valid unique candidates if any exist;
    it does not prune alternative position sets before common plate combination.
    Otherwise it returns one explicit ERROR with original-demand metrics and the
    retained least-deficient geometry. No demand is removed, including the edge.

    Extra request params: ``source_recovery_bar_penalties`` (default ``(0, 2)``),
    ``source_recovery_position_penalty_kg`` (default 0), and
    ``source_recovery_neighbor_count`` (default 6). Bounds limit repair branching;
    each repair has at most one iteration per initially deficient source cell.
    ``time_limit_s`` is forwarded unchanged to GA. Bounded recovery and independent
    validation run afterwards and are included in reported total runtime, not in
    that stochastic search budget. Placement remains a separate engineering gate.
    """

    name = "genetic-source-recovery"

    def solve_many(
        self, problem: LayoutProblem, request: AlgorithmRequest | None = None,
    ) -> tuple[LayoutSolution, ...]:
        started = perf_counter()
        request = request or AlgorithmRequest()
        penalties, position_penalty, neighbors = _repair_parameters(request)
        maximum_zones = request.max_details or request.params.get("maximum_zones", 32)
        if type(maximum_zones) is not int or maximum_zones < 1:
            raise ValueError("maximum_zones must be a positive integer")
        # The GA's implicit cap must also constrain recovery, not just generation.
        effective_request = replace(request, max_details=maximum_zones)
        empty = LayoutSolution(
            algorithm=self.name, status=SolutionStatus.ERROR, zones=(),
            metrics=LayoutMetrics(0, 0.0, 0, 0, 0, 0, 0.0, 0.0),
            request=effective_request,
        )
        # Validates original IDs/mapping and rejects already-reduced input BEFORE
        # running the GA. This also supplies honest original metrics on total failure.
        empty_audit = revalidate_source_demand(problem, empty)
        fallback = empty_audit.solution
        reduced = apply_single_cell_rule(problem, policy="legacy-research")
        preprocessing = deepcopy(reduced.meta["single_cell_preprocessing"])
        rejections: list[dict] = []
        accepted: dict[tuple, LayoutSolution] = {}
        statistics = {
            "proposal_count": 0,
            "unchanged_source_valid_count": 0,
            "repair_attempt_count": 0,
            "source_valid_attempt_count": 0,
            "duplicate_geometry_count": 0,
        }

        def retain_fallback(candidate: LayoutSolution) -> None:
            nonlocal fallback
            def key(item: LayoutSolution):
                return (item.metrics.under_reinforced_cell_count,
                        sum(message.startswith("ERROR:") for message in item.diagnostics),
                        item.metrics.total_mass_kg, item.metrics.detail_count)
            if key(candidate) < key(fallback):
                fallback = candidate

        def accept(candidate: LayoutSolution, proposal: LayoutSolution,
                   proposal_index: int, penalty: float | None, original_missing: int) -> None:
            # Do not trust a repair's status/metrics or patched coverage caches.
            checked = revalidate_source_demand(problem, candidate).solution
            retain_fallback(checked)
            if checked.status is not SolutionStatus.FEASIBLE:
                rejections.append({
                    "proposal_index": proposal_index, "bar_penalty_kg": penalty,
                    "reason": "original hard validation failed",
                    "under_reinforced_cell_count": checked.metrics.under_reinforced_cell_count,
                    "diagnostics": checked.diagnostics,
                })
                return
            statistics["source_valid_attempt_count"] += 1
            geometry_key = _geometry_key(checked)
            if geometry_key in accepted:
                statistics["duplicate_geometry_count"] += 1
                return
            proposal_warnings = tuple(
                "WARNING: исходное GA-предложение: " + message.removeprefix("WARNING:").strip()
                for message in proposal.diagnostics if message.startswith("WARNING:")
            )
            checked = replace(
                checked, algorithm=self.name,
                diagnostics=tuple(dict.fromkeys((*checked.diagnostics, *proposal_warnings))),
                meta={**checked.meta, "source_recovery_proposal": {
                    "index": proposal_index, "algorithm": proposal.algorithm,
                    "status": proposal.status.value, "bar_penalty_kg": penalty,
                    "original_missing_before_recovery": original_missing,
                }},
            )
            accepted[geometry_key] = checked

        if not empty_audit.uncovered_cells:
            accept(fallback, fallback, 0, None, 0)
        else:
            try:
                proposals = GeneticParetoOptimizer().solve_many(reduced, effective_request)
            except CutLengthInfeasibleError as error:
                proposals = ()
                rejections.append({"reason": "GA could not construct stock-compatible proposals",
                                   "diagnostics": (str(error),)})
            statistics["proposal_count"] = len(proposals)
            for proposal_index, proposal in enumerate(proposals, 1):
                # A generator cannot weaken the cap/objective through its output request.
                proposal = replace(proposal, request=effective_request)
                try:
                    audit = revalidate_source_demand(problem, proposal)
                except ValueError as error:
                    rejections.append({"proposal_index": proposal_index,
                                       "reason": "invalid unchanged proposal", "diagnostics": (str(error),)})
                    continue
                retain_fallback(audit.solution)
                if audit.evaluation.valid:
                    statistics["unchanged_source_valid_count"] += 1
                    accept(audit.solution, proposal, proposal_index, None, 0)
                    continue
                for penalty in penalties:
                    statistics["repair_attempt_count"] += 1
                    try:
                        repaired = recover_source_demand(
                            problem, audit.solution, bar_penalty_kg=penalty,
                            position_penalty_kg=position_penalty, neighbor_count=neighbors,
                        )
                        accept(replace(repaired, request=effective_request), proposal,
                               proposal_index, penalty, len(audit.uncovered_cells))
                    except ValueError as error:
                        rejections.append({"proposal_index": proposal_index, "bar_penalty_kg": penalty,
                                           "reason": "repair rejected", "diagnostics": (str(error),)})

        metadata = {
            "policy": "legacy-geometry-proposals-original-demand-recovery/v1",
            "proposal_preprocessing": preprocessing,
            "preprocessing_used_for": "geometry proposals only; not accepted demand averaging",
            "final_demand_policy": "preserve-original-demand",
            "original_cell_count": len(problem.demand.cells),
            "original_demanded_cell_count": empty_audit.evaluation.metrics.demanded_cell_count,
            "statistics": {**statistics, "accepted_unique_count": len(accepted),
                           "rejected_attempt_count": len(rejections)},
            "rejections": tuple(rejections),
            "source_recovery_bar_penalties": penalties,
            "source_recovery_position_penalty_kg": position_penalty,
            "source_recovery_neighbor_count": neighbors,
            "maximum_zones": maximum_zones,
            "time_limit_scope": "GA search only; bounded recovery and validation follow",
            "placement_eligible": False,
        }
        results = tuple(accepted.values())
        if not results:
            results = (replace(
                fallback, algorithm=self.name, status=SolutionStatus.ERROR,
                diagnostics=(*fallback.diagnostics,
                             "ERROR: bounded source recovery found no original-hard-valid candidate; "
                             "retained geometry is not an accepted solution"),
            ),)
        elapsed_ms = (perf_counter() - started) * 1000.0
        return tuple(replace(
            candidate, runtime_ms=elapsed_ms,
            meta={**candidate.meta, "genetic_source_recovery": deepcopy(metadata)},
        ) for candidate in results)

    def solve(
        self, problem: LayoutProblem, request: AlgorithmRequest | None = None,
    ) -> LayoutSolution:
        return min(self.solve_many(problem, request), key=lambda solution: (
            solution.metrics.objective_value, solution.metrics.detail_count,
            solution.metrics.total_mass_kg,
        ))
