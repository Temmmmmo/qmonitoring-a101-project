"""Finite simultaneous shaped selection with exact coverage/collision separation.

MILP points are search constraints, NEVER a substitute for polygon coverage.
Only a complete incumbent passing fresh whole-polygon and 3D checks is returned.
No source field, blank length, diameter, owner inventory or approval is changed.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from time import perf_counter

from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .shaped_global_repair import _axis_choices
from ..contracts.shaped_physical import K09_U_RETURN_50D_PROFILE
from ..services.collision_replacement import freeze_owner_fe_service
from ..services.opening_relocation import lane_map
from ..services.shaped_collisions import check_shaped_collisions
from ..services.shaped_fe_repair import ResearchLayerProfile, exterior_edge_choices, layer_elevations, shaped_service_offers
from ..services.shaped_geometry import build_u_edge_bar, check_shaped_host, straight_bar_from_physical


@dataclass(frozen=True)
class _Candidate:
    owner: int
    bar: object
    original: bool
    host_ok: bool
    offers: tuple


def _missing(problem, selected):
    """Whole positive polygons, independent of any finite witness constraints."""
    missing = []
    for p in problem.direction_problems:
        offers = [offer for item in selected if item.bar.direction == p.demand.direction for offer in item.offers]
        for level in p.demand.levels:
            if not level.recipe.additions:
                continue
            if len(level.recipe.additions) != 1:
                raise ValueError("Explicit single-addition source levels required")
            spec = level.recipe.additions[0]
            supply = unary_union([poly for d, s, poly in offers if d >= spec.diameter and s <= spec.step])
            for cell in p.demand.cells:
                if cell.level_index != level.index:
                    continue
                residue = Polygon(cell.poly).difference(supply)
                if residue.area > 0:
                    parts = (residue,) if isinstance(residue, Polygon) else residue.geoms
                    for part in parts:
                        if part.area > 0:
                            missing.append((p.demand.direction, spec.diameter, spec.step, part))
    return missing


def _witnesses(regions):
    return [(d, diameter, step, polygon.representative_point()) for d, diameter, step, polygon in regions]


def _initial_regions(problem):
    result = []
    for p in problem.direction_problems:
        for cell in p.demand.cells:
            recipe = p.demand.level(cell.level_index).recipe
            if recipe.additions:
                spec = recipe.additions[0]
                result.append((p.demand.direction, spec.diameter, spec.step, Polygon(cell.poly)))
    return result


def _pair_ids(pair):
    return tuple((row["direction"], row["bar_id"]) for row in (pair["first"], pair["second"]))


def _score(selected, conflict_ids):
    return (sum(not c.host_ok for c in selected),
            sum(c.original and (str(c.bar.direction), c.bar.id) in conflict_ids for c in selected),
            sum(not c.original for c in selected))


def propose_joint_shaped_repair(before, lanes, problem, host, *,
        layer_profile=ResearchLayerProfile(), maximum_axes_per_bar=8,
        maximum_candidates_per_bar=16, maximum_rounds=20,
        maximum_witnesses=20000, time_limit_s=600):
    """Pure q/U joint experiment; a final independent checker is still required.

    Lexicographic integer objective: remaining host failures, original conflict
    participants retained, then changed bars. This is not an engineering score.
    Original-original conflict pairs may remain, explicitly; modified pairs may
    not. Point cuts converge only within the finite, explicitly truncated pool.
    """
    for value, high in ((maximum_axes_per_bar, 32), (maximum_candidates_per_bar, 64),
                        (maximum_rounds, 100), (maximum_witnesses, 100000)):
        if type(value) is not int or not 1 <= value <= high:
            raise ValueError("Positive bounded joint-search resources required")
    if (isinstance(time_limit_s, bool) or not isinstance(time_limit_s, (int, float))
            or not math.isfinite(time_limit_s) or not .001 <= time_limit_s <= 1800):
        raise ValueError("Finite bounded joint-search time required")
    freeze_owner_fe_service(before, lanes, problem)
    sources = lane_map(lanes)
    started = perf_counter()
    candidates, by_owner = [], [[] for _ in before]
    baseline = []
    for owner, previous in enumerate(before):
        zm, _ = layer_elevations(host, previous.direction, previous.diameter_mm, layer_profile)
        bar = straight_bar_from_physical(previous, axis_z_mm=zm, placement_profile_id=layer_profile.id)
        item = _Candidate(owner, bar, True, check_shaped_host(bar, host)["whole_body_with_cover_contained"],
                          shaped_service_offers(bar, sources))
        by_owner[owner].append(len(candidates))
        candidates.append(item)
        baseline.append(item)
    if _missing(problem, baseline):
        raise ValueError("The entire original FE field must pass before a joint experiment")
    initial_collisions = check_shaped_collisions(tuple(c.bar for c in baseline))
    conflict_ids = {key for pair in (*initial_collisions["proven_collision_pairs"],
                                    *initial_collisions["uncertain_pairs"]) for key in _pair_ids(pair)}
    best, cuts, points, seen_points, rounds = tuple(baseline), set(), [], set(), []
    telemetry = {"policy": "joint-original-FE-finite-q-U-pool/research-v1",
        "placement_eligible": False, "source_field_transferred": False,
        "global_optimality_proven": False, "finite_point_coverage_is_final_proof": False,
        "candidate_pool_truncated": False, "original_3D_collisions": initial_collisions}
    telemetry["U_shape_length_limit_bar_ids"] = []

    def finish(reason):
        telemetry.update(reason=reason, candidate_count=len(candidates), rounds=rounds,
            witness_count=len(points), collision_cut_count=len(cuts), runtime_s=perf_counter()-started,
            before_score=_score(baseline, conflict_ids), after_score=_score(best, conflict_ids),
            changed_bar_count=_score(best, conflict_ids)[2], complete_incumbent_retained=True)
        return tuple(c.bar for c in best), telemetry

    # Build all original fallbacks before any bounded candidate generation.
    for owner, previous in enumerate(before):
        if perf_counter()-started >= time_limit_s:
            return finish("candidate_time_budget")
        axes, cut = _axis_choices(previous, sources, host, 300, 256)
        telemetry["candidate_pool_truncated"] |= cut or len(axes) > maximum_axes_per_bar
        if len(axes) > maximum_axes_per_bar:
            # Distribute choices over the entire allowed list, rather than spend
            # the whole pool on submillimetre variations near the old position.
            indexes = {0} if maximum_axes_per_bar == 1 else {
                round(i*(len(axes)-1)/(maximum_axes_per_bar-1)) for i in range(maximum_axes_per_bar)}
            axes = tuple(axes[i] for i in sorted(indexes))
        zm, zr = layer_elevations(host, previous.direction, previous.diameter_mm, layer_profile)
        allow_u = previous.installed_length_mm <= K09_U_RETURN_50D_PROFILE.maximum_cut_length_mm
        if not allow_u:
            telemetry["U_shape_length_limit_bar_ids"].append((str(previous.direction), previous.id))
        choices = []
        for q in axes:
            choices.append(straight_bar_from_physical(replace(previous, transverse_axis_mm=q),
                axis_z_mm=zm, placement_profile_id=layer_profile.id))
            if not allow_u:
                # Keep the exact source length and all straight candidates.
                # Do not silently round a slightly over-limit binary float to
                # 11700 just to make the stricter U constructor accept it.
                continue
            for edge, inward in exterior_edge_choices(host, previous.direction, q, previous.diameter_mm):
                built = build_u_edge_bar(bar_id=previous.id, direction=previous.direction,
                    steel_class=previous.steel_class, diameter_mm=previous.diameter_mm,
                    transverse_axis_mm=q, edge_coordinate_mm=edge, inward_sign=inward,
                    main_axis_z_mm=zm, return_axis_z_mm=zr,
                    slab_thickness_mm=host.sections[-1].top_z_mm-host.sections[0].bottom_z_mm,
                    side_cover_mm=host.side_cover_mm, cut_length_mm=previous.installed_length_mm,
                    source_bar_ids=previous.source_bar_ids, placement_profile_id=layer_profile.id)
                if built.status == "geometry_conditions_met":
                    choices.append(built.bar)
        known = {baseline[owner].bar}
        for bar in choices:
            if perf_counter()-started >= time_limit_s:
                return finish("candidate_time_budget")
            if bar in known:
                continue
            known.add(bar)
            if not check_shaped_host(bar, host)["whole_body_with_cover_contained"]:
                continue
            if len(by_owner[owner]) >= maximum_candidates_per_bar:
                telemetry["candidate_pool_truncated"] = True
                break
            if len(candidates) >= 50000:
                return finish("complete_candidate_pool_budget")
            item = _Candidate(owner, bar, False, True, shaped_service_offers(bar, sources))
            by_owner[owner].append(len(candidates))
            candidates.append(item)
    try:
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import coo_matrix
    except ImportError:
        return finish("optional_solver_unavailable")

    indexed = {}
    for p in problem.direction_problems:
        rows = [(i, d, s, polygon) for i, c in enumerate(candidates) if c.bar.direction == p.demand.direction
                for d, s, polygon in c.offers]
        if len(rows) > 100000:
            return finish("complete_offer_index_budget")
        indexed[p.demand.direction] = STRtree([item[3] for item in rows]), rows

    def add_points(witnesses):
        added = 0
        for direction, diameter, step, point in witnesses:
            if perf_counter()-started >= time_limit_s:
                return None
            key = direction, diameter, step, point.x, point.y
            if key in seen_points:
                continue
            if len(points) >= maximum_witnesses:
                return None
            tree, items = indexed[direction]
            eligible = tuple(sorted({items[j][0] for j in tree.query(point, predicate="intersects")
                                     if items[j][1] >= diameter and items[j][2] <= step}))
            if not eligible:
                return None
            seen_points.add(key)
            points.append(eligible)
            added += 1
        return added

    if add_points(_witnesses(_initial_regions(problem))) is None:
        return finish("initial_witness_unavailable_or_budget")
    n = len(before)
    # One whole host failure outweighs every lower-priority term in the party.
    cost = np.array([(not c.host_ok)*(n+1)**2
        + (c.original and (str(c.bar.direction), c.bar.id) in conflict_ids)*(n+1)
        + (not c.original) for c in candidates], dtype=float)
    for iteration in range(maximum_rounds):
        remaining = time_limit_s-(perf_counter()-started)
        if remaining <= 0:
            return finish("solver_time_budget")
        rows, cols, lower, upper = [], [], [], []

        def row(indices, lo, hi):
            rows.extend([len(lower)]*len(indices))
            cols.extend(indices)
            lower.append(lo)
            upper.append(hi)

        for group in by_owner:
            row(group, 1, 1)
        for group in points:
            row(group, 1, math.inf)
        for pair in sorted(cuts):
            row(pair, -math.inf, 1)
        matrix = coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(len(lower), len(candidates))).tocsr()
        result = milp(cost, integrality=np.ones(len(candidates)), bounds=Bounds(0, 1),
            constraints=LinearConstraint(matrix, lower, upper),
            options={"time_limit": remaining, "mip_rel_gap": 0.0})
        if result.x is None or not np.isfinite(result.x).all():
            return finish("no_integer_incumbent")
        selected_indexes = tuple(i for i, value in enumerate(result.x) if value > .5)
        if (len(selected_indexes) != n or {candidates[i].owner for i in selected_indexes} != set(range(n))
                or any(abs(value-round(value)) > 1e-6 for value in result.x)):
            return finish("nonintegral_or_incomplete_solver_result")
        selected = tuple(sorted((candidates[i] for i in selected_indexes), key=lambda c: c.owner))
        missing = _missing(problem, selected)
        point_count = add_points(_witnesses(missing))
        if point_count is None:
            return finish("coverage_witness_unavailable_or_budget")
        collisions = check_shaped_collisions(tuple(c.bar for c in selected))
        selected_map = {(str(candidates[i].bar.direction), candidates[i].bar.id): i for i in selected_indexes}
        new_cuts = 0
        for pair in (*collisions["proven_collision_pairs"], *collisions["uncertain_pairs"]):
            ids = tuple(sorted(selected_map[key] for key in _pair_ids(pair)))
            if all(candidates[i].original for i in ids):
                continue
            if ids not in cuts:
                cuts.add(ids)
                new_cuts += 1
        rounds.append({"round": iteration+1, "solver_status": int(result.status),
            "missing_polygon_count": len(missing), "new_witnesses": point_count,
            "new_collision_cuts": new_cuts, "selected_score": _score(selected, conflict_ids)})
        if not missing and not new_cuts:
            # Recheck all selected collision pairs, not just the newly inserted
            # cuts, before accepting a possibly time-limited MIP incumbent.
            if any(not all(candidates[selected_map[key]].original for key in _pair_ids(pair))
                   for pair in (*collisions["proven_collision_pairs"], *collisions["uncertain_pairs"])):
                return finish("selected_conflict_survived_existing_cut")
            if _score(selected, conflict_ids) < _score(best, conflict_ids):
                best = selected
            return finish("whole_polygon_and_joint_3D_incumbent_checked")
        if not point_count and not new_cuts:
            return finish("positive_polygon_residual_without_new_cut")
    return finish("separation_round_budget")
