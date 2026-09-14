"""Research-only straight-bar fusion/side duplication with frozen owner FE proof.

The legacy once-only source/phase certificate is deliberately NOT reused. One
owner may have two descendants, but their geometric union must preserve EVERY
positive sufficient FE fragment served before replacement. Weak As never sums.
This does not certify rectangular zones, an approved lap detail, or placement.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import math

from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from rebar.models import Direction

from ..contracts.physical import PhysicalBar
from ..contracts.plate import PlateProblem
from .bar_schedule import BarScheduleGroup, build_bar_schedule
from .cutting import PLATE_11700_CUT_LENGTHS_MM
from .fe_host_repair import NOT_CHECKED, _installed_offers, _valid_bars, installed_fe_coverage
from .opening_relocation import collision_pairs, contained, lane_map, material_and_holes
from .physical_host_fit import _source_window
from .stock_cutting import check_stock_cutting

TOL = 1e-6
POLICY = "frozen-owner-FE-fuse-or-tangent-side-duplication-full40d/research-v1"


@dataclass(frozen=True)
class CollisionReplacementOperation:
    kind: str
    direction: Direction
    pair_ids: tuple[str, str]
    removed_ids: tuple[str, ...]
    added_bars: tuple[PhysicalBar, ...]


@dataclass(frozen=True)
class CollisionReplacementLimits:
    maximum_mass_kg: float
    maximum_bar_count: int
    maximum_position_count: int
    maximum_operations: int = 64
    maximum_frozen_fragments: int = 100000


@dataclass(frozen=True)
class OwnerFeObligation:
    direction: Direction
    source_id: str
    cell_ids: tuple[int, ...]
    # Shapes are the actual before intersections, not their envelopes.
    fragments: tuple
    required_interval_mm: tuple[float, float] | None


def _key(bar):
    return bar.direction, bar.id


def transverse_source_geometry_ok(bar, sources):
    """Original owner/material/windows/contact, without obsolete along bbox40d."""
    for owner in bar.source_bar_ids:
        lane = sources.get((bar.direction, owner))
        if lane is None:
            return False
        source = lane.source
        if (bar.steel_class != source.steel_class or bar.diameter_mm < source.diameter_mm
                or not lane.axis_window_mm[0]-TOL <= bar.transverse_axis_mm <= lane.axis_window_mm[1]+TOL):
            return False
        old_gap = abs(math.remainder(source.transverse_axis_mm-source.background_origin_mm, 300)) - (
            source.diameter_mm+source.background_diameter_mm)/2
        new_gap = abs(math.remainder(bar.transverse_axis_mm-source.background_origin_mm, 300)) - (
            bar.diameter_mm+source.background_diameter_mm)/2
        if new_gap < -TOL or abs(old_gap) <= TOL < abs(new_gap):
            return False
    return True


def freeze_owner_fe_service(before, lanes, problem, *, maximum_fragments=100000):
    """Freeze all original owner FE pieces from independently valid actual cores."""
    if not isinstance(problem, PlateProblem):
        raise ValueError("Typed complete original PlateProblem required")
    if (not isinstance(lanes, tuple) or not 1 <= len(lanes) <= 5000
            or sum(len(p.demand.cells) for p in problem.direction_problems) > 10000):
        raise ValueError("Bounded immutable lanes and original FE problem required")
    if type(maximum_fragments) is not int or not 1 <= maximum_fragments <= 1000000:
        raise ValueError("Bounded positive frozen fragment limit required")
    _valid_bars(before)
    sources = lane_map(lanes)
    owners = Counter((bar.direction, owner) for bar in before for owner in bar.source_bar_ids)
    if set(owners) != set(sources) or any(count != 1 for count in owners.values()):
        raise ValueError("Before must contain every source owner exactly once")
    for lane in lanes:
        source = lane.source
        original = PhysicalBar(source.id, source.direction, source.steel_class, source.diameter_mm,
            source.transverse_axis_mm, source.installed_interval_mm, (source.id,))
        _source_window(original, (source,))
    if any(not transverse_source_geometry_ok(bar, sources) for bar in before):
        raise ValueError("Before source windows/material/background invalid")
    if installed_fe_coverage(problem, before, sources)["status"] != "pass":
        raise ValueError("Complete original FE coverage required before freezing")
    trees = {}
    for original in problem.direction_problems:
        demand = original.demand
        cells = [c for c in demand.cells if demand.level(c.level_index).recipe.additions]
        shapes = [Polygon(c.poly) for c in cells]
        specs = [demand.level(c.level_index).recipe.additions[0] for c in cells]
        trees[demand.direction] = STRtree(shapes), cells, shapes, specs
    result, total = {}, 0
    for bar in before:
        tree, cells, shapes, specs = trees[bar.direction]
        axis = 0 if str(bar.direction).endswith("X") else 1
        for owner in bar.source_bar_ids:
            fragments, ids = [], []
            for diameter, step, offer in _installed_offers(replace(bar, source_bar_ids=(owner,)), sources):
                for index in sorted(tree.query(offer)):
                    if diameter < specs[index].diameter or step > specs[index].step:
                        continue
                    hit = offer.intersection(shapes[index])
                    if hit.area > 0:  # NO positive-fragment tolerance/discard.
                        fragments.append(hit)
                        ids.append(cells[index].id)
                        total += 1
                        if total > maximum_fragments:
                            raise ValueError("Frozen FE fragment budget exceeded; no truncation")
            interval = (min(p.bounds[axis] for p in fragments), max(p.bounds[axis+2] for p in fragments)) if fragments else None
            result[(bar.direction, owner)] = OwnerFeObligation(bar.direction, owner,
                tuple(ids), tuple(fragments), interval)
    return result


def owner_service_preserved(obligation, descendants, sources):
    """Union of sufficient same-owner cores; no self-certification or As summing."""
    owner = obligation.source_id
    offers = [shape for bar in descendants if bar.direction == obligation.direction and owner in bar.source_bar_ids
              for _, _, shape in _installed_offers(replace(bar, source_bar_ids=(owner,)), sources)]
    if not offers:
        return not obligation.fragments and any(owner in b.source_bar_ids and b.direction == obligation.direction for b in descendants)
    union = unary_union(offers)
    return all(fragment.difference(union).area == 0 for fragment in obligation.fragments)


def operation_bars(before, operations):
    """Apply only explicit disjoint original collision replacements, never mutations."""
    _valid_bars(before)
    if not isinstance(operations, tuple) or len(operations) > 64:
        raise ValueError("Bounded typed replacement operations required")
    old = {_key(b): b for b in before}
    pairs = collision_pairs(before)
    used, removed, additions = set(), set(), []
    for op in operations:
        if (not isinstance(op, CollisionReplacementOperation) or not isinstance(op.direction, Direction)
                or not isinstance(op.pair_ids, tuple) or len(op.pair_ids) != 2
                or any(not isinstance(v, str) or not v for v in op.pair_ids)
                or tuple(sorted(set(op.pair_ids))) != op.pair_ids
                or (str(op.direction), *op.pair_ids) not in pairs
                or not isinstance(op.removed_ids, tuple) or len(set(op.removed_ids)) != len(op.removed_ids)
                or any(i not in op.pair_ids for i in op.removed_ids)):
            raise ValueError("Explicit original colliding pair and removed identities required")
        pair_keys = {(op.direction, i) for i in op.pair_ids}
        if used & pair_keys:
            raise ValueError("Collision operations must have disjoint original pairs")
        used.update(pair_keys)
        _valid_bars(op.added_bars)
        if any(b.direction != op.direction or _key(b) in old for b in op.added_bars):
            raise ValueError("Added bars need new identities in the original direction")
        pair = tuple(old[(op.direction, i)] for i in op.pair_ids)
        if pair[0].transverse_axis_mm != pair[1].transverse_axis_mm or pair[0].steel_class != pair[1].steel_class:
            raise ValueError("Only exactly coaxial same-material pair replacement supported")
        if op.kind == "fuse":
            if set(op.removed_ids) != set(op.pair_ids) or len(op.added_bars) != 1:
                raise ValueError("Fusion must replace exactly two bars by one")
            new = op.added_bars[0]
            owners = set(pair[0].source_bar_ids) | set(pair[1].source_bar_ids)
            if (new.diameter_mm != max(b.diameter_mm for b in pair) or new.steel_class != pair[0].steel_class
                    or new.transverse_axis_mm != pair[0].transverse_axis_mm or set(new.source_bar_ids) != owners):
                raise ValueError("Fusion must preserve axis/material and union owners at maximum diameter")
        elif op.kind == "split_sides":
            if len(op.removed_ids) != 1 or len(op.added_bars) != 2:
                raise ValueError("Side split must replace exactly one bar by two")
            target = old[(op.direction, op.removed_ids[0])]
            stationary = next(b for b in pair if b.id != target.id)
            distance = (target.diameter_mm+stationary.diameter_mm)/2
            axes = sorted(b.transverse_axis_mm for b in op.added_bars)
            if (any(abs(a-b) > TOL for a, b in zip(axes,
                    (target.transverse_axis_mm-distance, target.transverse_axis_mm+distance)))
                    or any(b.diameter_mm != target.diameter_mm or b.steel_class != target.steel_class
                        or b.source_bar_ids != target.source_bar_ids for b in op.added_bars)):
                raise ValueError("Both tangent side descendants must retain target diameter/material/owners")
        else:
            raise ValueError("Unsupported collision replacement operation")
        removed.update((op.direction, i) for i in op.removed_ids)
        additions.extend(op.added_bars)
    after = tuple(b for b in before if _key(b) not in removed) + tuple(additions)
    _valid_bars(after)
    return after


def check_collision_replacement(before, operations, lanes, problem, host, *, limits, stock_time_limit_s=30):
    """Independent complete proof; a host regression is explicit, NEVER accepted."""
    if (not isinstance(limits, CollisionReplacementLimits)
            or isinstance(limits.maximum_mass_kg, bool) or not isinstance(limits.maximum_mass_kg, (int, float))
            or not math.isfinite(limits.maximum_mass_kg) or not 0 < limits.maximum_mass_kg <= 1e9
            or any(type(v) is not int or not 1 <= v <= ceiling for v, ceiling in (
                (limits.maximum_bar_count, 5000), (limits.maximum_position_count, 512),
                (limits.maximum_operations, 64), (limits.maximum_frozen_fragments, 1000000)))
            or isinstance(stock_time_limit_s, bool) or not isinstance(stock_time_limit_s, (int, float))
            or not math.isfinite(stock_time_limit_s) or not 0.001 <= stock_time_limit_s <= 60):
        raise ValueError("Finite explicit mass/count/positions/verification limits required")
    frozen = freeze_owner_fe_service(before, lanes, problem, maximum_fragments=limits.maximum_frozen_fragments)
    after = operation_bars(before, operations)
    if len(operations) > limits.maximum_operations or len(after) > limits.maximum_bar_count:
        raise ValueError("Replacement operation or physical count limit exceeded")
    sources = lane_map(lanes)
    for bar in after:
        if (not transverse_source_geometry_ok(bar, sources)
                or min(abs(bar.installed_length_mm-length) for length in PLATE_11700_CUT_LENGTHS_MM) > TOL):
            raise ValueError("Actual source windows/background/material or catalogue length invalid")
    descendants = {}
    for bar in after:
        for owner in bar.source_bar_ids:
            descendants.setdefault((bar.direction, owner), []).append(bar)
    if set(descendants) != set(frozen):
        raise ValueError("Original source ownership lost or invented")
    for key, obligation in frozen.items():
        if not owner_service_preserved(obligation, descendants[key], sources):
            raise ValueError("Frozen original owner FE fragment/full40d lost")
    coverage = installed_fe_coverage(problem, after, sources)
    if coverage["status"] != "pass":
        raise ValueError("Complete original installed-core FE coverage failed")
    old_pairs, new_pairs = collision_pairs(before), collision_pairs(after)
    if new_pairs-old_pairs:
        raise ValueError("New same-direction body collision after replacement")
    schedule = build_bar_schedule(tuple(BarScheduleGroup(f"{b.direction}/{b.id}", b.diameter_mm,
        b.installed_length_mm, 1, b.steel_class) for b in after))
    mass = math.fsum(item.total_mass_kg for item in schedule)
    if mass > limits.maximum_mass_kg+TOL or len(schedule) > limits.maximum_position_count:
        raise ValueError("Explicit mass or specification position limit exceeded")
    stock = check_stock_cutting(schedule, time_limit_s=stock_time_limit_s)
    material, _, _ = material_and_holes(host)
    old_host = sum(not contained(b, material, host.side_cover_mm) for b in before)
    new_host = sum(not contained(b, material, host.side_cover_mm) for b in after)
    return after, {"schema_version": "collision-replacement-check/v1", "policy": POLICY,
        "placement_eligible": False, "structural_placement_supported": False, "engineering_approval": False,
        "source_demand_removed": False, "source_demand_values_changed": False,
        "legacy_source_certificate_reused": False, "rectangular_zone_certificate_reused": False,
        "input_transverse_axes_preserved": all(op.kind == "fuse" for op in operations),
        "research_service_width_assumption": "original per-owner finite widths translated to each descendant; union, never added As",
        "tangent_parallel_side_duplication_is_engineering_approved": False,
        "physical_bar_count": len(after), "additional_mass_kg": mass, "position_count": len(schedule),
        "source_coverage": coverage, "stock_cutting": stock,
        "host_blocked_before": old_host, "host_blocked_after": new_host, "host_regression": new_host > old_host,
        "same_direction_body_pairs_before": len(old_pairs), "same_direction_body_pairs_after": len(new_pairs),
        "new_body_pairs": len(new_pairs-old_pairs), "remaining_body_pairs": sorted(new_pairs),
        "owner_proof": {"source_owner_count": len(frozen), "positive_fragment_count": sum(len(v.fragments) for v in frozen.values()),
            "every_positive_fragment_preserved": True, "weak_As_summed": False,
            "duplicated_owner_count": sum(len(v) > 1 for v in descendants.values()),
            "owners": [{"direction": str(v.direction), "source_id": v.source_id, "cell_ids": v.cell_ids,
                "frozen_required_interval_mm": v.required_interval_mm, "fragment_count": len(v.fragments),
                "descendant_ids": [b.id for b in descendants[key]]} for key, v in frozen.items()]},
        "not_checked": [*NOT_CHECKED, "approved_parallel_contact_lap_detail", "original_STO_phase_certificate",
            "replacement_STO_phase_profile"],
        "status": "blocked_host_regression" if new_host > old_host else "blocked_host" if new_host
            else "blocked_body_collisions" if new_pairs else "blocked_stock" if stock["status"] != "pass"
            else "research_checks_passed_not_placement_approved"}
