"""Bounded physical additional-bar normalization under an explicit research profile.

This opt-in service preserves every source axis and its complete required interval
plus 40d of the NEW diameter. It does not consume saved accepted layouts, assign Z,
approve a host, or authorize placement. Unknown engineering checks remain unknown.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from bisect import insort
import math
from time import perf_counter

from rebar.models import Axis, Direction, Layer

from ..contracts.physical import (
    PhysicalBar, PhysicalConflictTask, PhysicalNormalizationConfig,
    PhysicalNormalizationLimitError, PhysicalNormalizationMetrics,
    PhysicalNormalizationResult, PhysicalSourceBar,
)
from ..services.bar_schedule import BarScheduleGroup, build_bar_schedule
from ..services.cutting import PLATE_11700_CUT_LENGTHS_MM
from ..services.detailing import rebar_mass_kg
from ..services.geometry import GEOMETRY_TOLERANCE_MM as TOL
from ..services.stock_cutting import check_stock_cutting
from .stock_length_balance import balance_stock_lengths


class _SearchLimit(Exception):
    pass


class _Budget:
    def __init__(self, config):
        self.config = config
        self.deadline = perf_counter() + config.time_limit_s
        self.pair_checks = 0
        self.merge_operations = 0

    def check(self, pairs=0):
        self.pair_checks += pairs
        if self.pair_checks > self.config.maximum_pair_checks or perf_counter() >= self.deadline:
            raise _SearchLimit

    def seconds(self):
        self.check()
        return min(self.config.stock_balance_time_limit_s, self.deadline - perf_counter())


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _interval(value):
    return (isinstance(value, tuple) and len(value) == 2 and all(_finite(v) for v in value)
            and value[0] < value[1])


def _validate_config(config):
    if not isinstance(config, PhysicalNormalizationConfig) or type(config.allow_diameter_increase) is not bool:
        raise ValueError("typed PhysicalNormalizationConfig and explicit diameter policy are required")
    for name, low, high in (("maximum_bars", 1, 5000), ("maximum_merge_operations", 0, 10000),
                            ("maximum_pair_checks", 1, 100_000_000), ("maximum_exchange_attempts", 0, 300)):
        value = getattr(config, name)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be an integer in {low}..{high}")
    for name, low, high in (("time_limit_s", 0.001, 600), ("stock_balance_time_limit_s", 0.001, 60),
                            ("maximum_batch_mass_increase_pct", 0, 5)):
        value = getattr(config, name)
        if not _finite(value) or not low <= value <= high:
            raise ValueError(f"{name} must be finite in {low}..{high}")
    if config.maximum_mass_kg is not None and (not _finite(config.maximum_mass_kg) or config.maximum_mass_kg <= 0):
        raise ValueError("maximum_mass_kg must be finite and positive")


def _source_input(source_bars, config):
    if not isinstance(source_bars, tuple) or len(source_bars) > config.maximum_bars:
        raise ValueError("source_bars must be a bounded tuple; no records are truncated")
    source = {}
    for bar in source_bars:
        if (not isinstance(bar, PhysicalSourceBar) or not isinstance(bar.direction, Direction)
                or not isinstance(bar.direction.layer, Layer) or not isinstance(bar.direction.axis, Axis)
                or not isinstance(bar.id, str) or not bar.id.strip()
                or not isinstance(bar.steel_class, str) or not bar.steel_class.strip()
                or bar.steel_class != bar.steel_class.strip()
                or type(bar.diameter_mm) is not int or bar.diameter_mm <= 0
                or type(bar.background_diameter_mm) is not int or bar.background_diameter_mm <= 0
                or not _finite(bar.transverse_axis_mm) or not _finite(bar.background_origin_mm)
                or not _finite(bar.background_step_mm) or abs(bar.background_step_mm - 300) > TOL
                or not _interval(bar.required_interval_mm) or not _interval(bar.installed_interval_mm)
                or bar.installed_interval_mm[1] - bar.installed_interval_mm[0] > 11700 + TOL):
            raise ValueError("invalid source bar, direction, material, intervals or explicit @300 background")
        key = (bar.direction, bar.id)
        if key in source:
            raise ValueError("duplicate source bar ID within a direction")
        source[key] = bar
    bars = tuple(PhysicalBar(b.id, b.direction, b.steel_class, b.diameter_mm, b.transverse_axis_mm,
                            b.installed_interval_mm, (b.id,)) for b in source_bars)
    _certificate(bars, source)
    return source, bars


def _mass(bars):
    return math.fsum(rebar_mass_kg(b.diameter_mm, b.installed_length_mm, 1) for b in bars)


def _source_bounds(bars, source):
    direction = bars[0].direction
    ids = tuple(sorted({identifier for b in bars for identifier in b.source_bar_ids}))
    parents = tuple(source[(direction, identifier)] for identifier in ids)
    diameter = max(p.diameter_mm for p in parents)
    lower = min(p.required_interval_mm[0] for p in parents) - 40 * diameter
    upper = max(p.required_interval_mm[1] for p in parents) + 40 * diameter
    return ids, diameter, lower, upper


def _window(bar, source):
    parents = [source[(bar.direction, i)] for i in bar.source_bar_ids]
    lower = max(p.required_interval_mm[1] + 40 * bar.diameter_mm for p in parents) - bar.installed_length_mm
    upper = min(p.required_interval_mm[0] - 40 * bar.diameter_mm for p in parents)
    if lower > upper + TOL:
        raise ValueError("bar cannot retain all source demand and full NEW diameter 40d")
    return (lower, upper) if lower <= upper else ((lower + upper) / 2,) * 2


def _contact_valid(bar, source):
    for identifier in bar.source_bar_ids:
        p = source[(bar.direction, identifier)]
        axis = p.background_origin_mm + round((bar.transverse_axis_mm - p.background_origin_mm) / p.background_step_mm) * p.background_step_mm
        old = abs(p.transverse_axis_mm - axis) - (p.background_diameter_mm + p.diameter_mm) / 2
        new = abs(bar.transverse_axis_mm - axis) - (p.background_diameter_mm + bar.diameter_mm) / 2
        if new < -TOL or abs(old) <= TOL < abs(new):
            return False
    return True


def _certificate(bars, source):
    represented, output_ids = set(), set()
    contacts = 0
    for bar in bars:
        key = (bar.direction, bar.id)
        if key in output_ids or not bar.source_bar_ids or len(set(bar.source_bar_ids)) != len(bar.source_bar_ids):
            raise ValueError("invalid output identity/source-reference partition")
        output_ids.add(key)
        if not _interval(bar.installed_interval_mm) or not _contact_valid(bar, source):
            raise ValueError("invalid interval or source-background penetration/contact change")
        for identifier in bar.source_bar_ids:
            source_key = (bar.direction, identifier)
            if source_key not in source or source_key in represented:
                raise ValueError("unknown or multiply represented source bar")
            represented.add(source_key)
            parent = source[source_key]
            if (bar.steel_class != parent.steel_class or bar.diameter_mm < parent.diameter_mm
                    or bar.transverse_axis_mm != parent.transverse_axis_mm
                    or bar.installed_interval_mm[0] > parent.required_interval_mm[0] - 40 * bar.diameter_mm + TOL
                    or bar.installed_interval_mm[1] < parent.required_interval_mm[1] + 40 * bar.diameter_mm - TOL):
                raise ValueError("source axis/material/required interval/full NEW40d was not preserved")
            nearest = parent.background_origin_mm + round((parent.transverse_axis_mm - parent.background_origin_mm) / parent.background_step_mm) * parent.background_step_mm
            gap = abs(parent.transverse_axis_mm - nearest) - (parent.diameter_mm + parent.background_diameter_mm) / 2
            contacts += abs(gap) <= TOL
    if represented != source.keys():
        raise ValueError("source bar references were omitted")
    return {"status": "pass", "source_bar_count": len(source), "represented_source_bar_count": len(represented),
            "missing_source_bar_ids": [], "duplicated_source_bar_ids": [], "background_touch_source_count": contacts,
            "background_contact_broken_count": 0, "background_penetration_count": 0,
            "full_new_diameter_40d_preserved": True, "source_axes_preserved": True, "actual_3d_checked": False}


def _near(first, second):
    return (first.direction == second.direction and abs(first.transverse_axis_mm - second.transverse_axis_mm) + TOL
            < (first.diameter_mm + second.diameter_mm) / 2)


def _overlap(first, second):
    return min(first.installed_interval_mm[1], second.installed_interval_mm[1]) - max(first.installed_interval_mm[0], second.installed_interval_mm[0]) > TOL


def _conflicts(bars, budget=None, *, maximum_pair_checks=None):
    pairs, checked = [], 0
    groups = defaultdict(list)
    for bar in bars:
        groups[bar.direction].append(bar)
    for group in groups.values():
        ordered = sorted(group, key=lambda b: (b.transverse_axis_mm, b.id))
        maximum_radius = max(b.diameter_mm for b in ordered) / 2
        for i, first in enumerate(ordered):
            for j in range(i + 1, len(ordered)):
                second = ordered[j]
                if second.transverse_axis_mm - first.transverse_axis_mm + TOL >= first.diameter_mm / 2 + maximum_radius:
                    break
                checked += 1
                if budget is not None:
                    budget.check(1)
                if maximum_pair_checks is not None and checked > maximum_pair_checks:
                    raise PhysicalNormalizationLimitError("complete mandatory pair audit exceeds budget; not truncated")
                if _near(first, second) and _overlap(first, second):
                    if len(pairs) >= 200_000:
                        if budget is not None:
                            raise _SearchLimit
                        raise PhysicalNormalizationLimitError("complete collision output exceeds 200000 pairs; not truncated")
                    pairs.append((first, second))
    return tuple(pairs)


def _group_schedule(bars):
    counts = Counter((b.steel_class, b.diameter_mm, round(b.installed_length_mm, 6)) for b in bars)
    groups = tuple(BarScheduleGroup(f"group-{i}", diameter, length, count, steel)
                   for i, ((steel, diameter, length), count) in enumerate(sorted(counts.items())))
    membership = {(g.steel_class, g.diameter_mm, g.installed_length_mm): g.source_id for g in groups}
    return groups, membership


def _make_bar(group, source, identifier, config):
    if (len({b.direction for b in group}) != 1 or len({b.steel_class for b in group}) != 1
            or len({b.transverse_axis_mm for b in group}) != 1):
        return None
    ids, diameter, lower, upper = _source_bounds(group, source)
    if not config.allow_diameter_increase and any(source[(group[0].direction, i)].diameter_mm != diameter for i in ids):
        return None
    length = next((value for value in PLATE_11700_CUT_LENGTHS_MM if value >= upper - lower - TOL), None)
    if length is None:
        return None
    extra = (length - (upper - lower)) / 2
    bar = PhysicalBar(identifier, group[0].direction, group[0].steel_class, diameter,
                      group[0].transverse_axis_mm, (lower - extra, upper + extra), ids)
    return bar if _contact_valid(bar, source) else None


def _clusters(bars):
    grouped = defaultdict(list)
    for bar in bars:
        grouped[(bar.direction, bar.steel_class, bar.transverse_axis_mm)].append(bar)
    result = []
    for group in grouped.values():
        pending, upper = [], None
        for bar in sorted(group, key=lambda b: b.installed_interval_mm):
            if pending and bar.installed_interval_mm[0] >= upper - TOL:
                result.append(tuple(pending))
                pending = []
            pending.append(bar)
            upper = max(b.installed_interval_mm[1] for b in pending)
        result.append(tuple(pending))
    return tuple(result)


def _identifier(prefix, occupied):
    value = prefix
    while value in occupied:
        value += "-new"
    occupied.add(value)
    return value


def _whole_merge(bars, source, config, budget, maximum_mass, trace):
    current = []
    for bar in bars:
        candidate = _make_bar((bar,), source, bar.id, config)
        current.append(candidate or bar)
    if _mass(current) > maximum_mass + TOL:
        return bars
    current_mass = _mass(current)
    output = []
    for direction in dict.fromkeys(b.direction for b in current):
        group = [b for b in current if b.direction == direction]
        occupied = {b.id for b in group}
        count = 0
        for iteration in range(len(group) + 1):
            budget.check()
            rebuilt, merged = [], 0
            clusters = _clusters(group)
            for cluster_index, cluster in enumerate(clusters):
                if len(cluster) == 1 or budget.merge_operations >= config.maximum_merge_operations:
                    rebuilt.extend(cluster)
                    continue
                identifier = _identifier(f"required-union-{iteration}-{count}", occupied)
                proposal = _make_bar(cluster, source, identifier, config)
                delta = _mass((proposal,)) - _mass(cluster) if proposal is not None else math.inf
                if proposal is None or current_mass + delta > maximum_mass + TOL:
                    rebuilt.extend(cluster)
                    continue
                # Diameter enlargement can collide with a nearby axis; verify the
                # whole direction, not only the coincident-axis cluster.
                other = [*rebuilt, *(b for remaining in clusters[cluster_index + 1:] for b in remaining)]
                if len(_conflicts((*other, proposal), budget)) > len(_conflicts((*other, *cluster), budget)):
                    rebuilt.extend(cluster)
                    continue
                rebuilt.append(proposal)
                merged += 1
                count += 1
                budget.merge_operations += 1
                current_mass += delta
            group = rebuilt
            if not merged:
                break
        output.extend(group)
    trace.append({"stage": "required_interval_catalog_whole_union", "bar_count": len(output), "mass_kg": _mass(output)})
    return tuple(output)


def _pair_merge(bars, source, config, budget, maximum_mass, trace):
    current = list(bars)
    for iteration in range(len(bars)):
        budget.check()
        if budget.merge_operations >= config.maximum_merge_operations:
            break
        all_mass = _mass(current)
        candidates = []
        for left, right in _conflicts(current, budget):
            if left.transverse_axis_mm != right.transverse_axis_mm or left.steel_class != right.steel_class:
                continue
            occupied = {b.id for b in current if b.direction == left.direction}
            identifier = _identifier(f"pairwise-{iteration}", occupied)
            value = _make_bar((left, right), source, identifier, config)
            if value is None:
                continue
            savings = _mass((left, right)) - _mass((value,))
            if all_mass - savings > maximum_mass + TOL:
                continue
            nearby = [b for b in current if b.direction == left.direction and
                      (abs(b.transverse_axis_mm - left.transverse_axis_mm) + TOL
                       < (b.diameter_mm + value.diameter_mm) / 2)]
            after = [b for b in nearby if b.id not in (left.id, right.id)] + [value]
            reduction = len(_conflicts(nearby, budget)) - len(_conflicts(after, budget))
            if reduction < 0 or reduction == 0 and savings < -TOL:
                continue
            candidates.append((reduction, savings, str(left.direction), left.id, right.id, left, right, value))
        if not candidates:
            break
        chosen = max(candidates, key=lambda v: v[:5])
        reduction, savings, _, _, _, left, right, value = chosen
        current = [b for b in current if not (b.direction == left.direction and b.id in (left.id, right.id))] + [value]
        budget.merge_operations += 1
        trace.append({"stage": "pairwise_absorption", "direction": str(left.direction),
                      "old_bar_ids": [left.id, right.id], "new_bar_id": value.id,
                      "body_pair_reduction": reduction, "mass_reduction_kg": savings})
    return tuple(current)


def _balance(bars, config, budget, maximum_mass, trace):
    if not bars:
        return bars, check_stock_cutting((), time_limit_s=0.1)
    groups, membership = _group_schedule(bars)
    maximum_pct = min(config.maximum_batch_mass_increase_pct, (maximum_mass / _mass(bars) - 1) * 100)
    if maximum_pct < -TOL:
        return None
    balanced = balance_stock_lengths(groups, maximum_mass_increase_pct=max(0, maximum_pct), time_limit_s=budget.seconds())
    trace.append({"stage": "stock_balance", "status": balanced.status, "telemetry": balanced.telemetry})
    if balanced.status != "balanced":
        return None
    lengths = dict(balanced.installed_lengths_mm)
    result = []
    for bar in bars:
        length = lengths[membership[(bar.steel_class, bar.diameter_mm, round(bar.installed_length_mm, 6))]]
        if length < bar.installed_length_mm - TOL:
            raise ValueError("stock balance shortened an existing component")
        extra = (length - bar.installed_length_mm) / 2
        result.append(replace(bar, installed_interval_mm=(bar.installed_interval_mm[0] - extra, bar.installed_interval_mm[1] + extra)))
    actual_groups, _ = _group_schedule(result)
    stock = check_stock_cutting(build_bar_schedule(actual_groups), time_limit_s=budget.seconds())
    if stock["status"] != "pass" or _mass(result) > maximum_mass + TOL:
        return None
    return tuple(result), stock


def _shift(bars, source, budget, trace):
    """Strict global pair-count descent, fixed lengths/axes/count/material."""
    output = []
    for direction in sorted({b.direction for b in bars}, key=str):
        current = sorted((b for b in bars if b.direction == direction), key=lambda b: b.id)
        windows = [_window(b, source) for b in current]
        lengths = [b.installed_length_mm for b in current]
        neighbors = [[] for b in current]
        for i, first in enumerate(current):
            for j in range(i + 1, len(current)):
                budget.check(1)
                if _near(first, current[j]):
                    neighbors[i].append(j)
                    neighbors[j].append(i)

        def local(index, start):
            value = replace(current[index], installed_interval_mm=(start, start + lengths[index]))
            return sum(_overlap(value, current[j]) for j in neighbors[index])

        for _ in range(min(5000, len(current) * 4)):
            budget.check()
            best = None
            for i, bar in enumerate(current):
                before = local(i, bar.installed_interval_mm[0])
                if not before:
                    continue
                lower, upper = windows[i]
                options = {lower, upper, bar.installed_interval_mm[0]}
                for j in neighbors[i]:
                    options.update((min(upper, max(lower, current[j].installed_interval_mm[0] - lengths[i])),
                                    min(upper, max(lower, current[j].installed_interval_mm[1]))))
                for start in sorted(options):
                    budget.check(len(neighbors[i]))
                    after = local(i, start)
                    if after >= before:
                        continue
                    rank = (after - before, abs(start - bar.installed_interval_mm[0]), bar.id, start)
                    if best is None or rank < best[0]:
                        best = rank, i, start, before, after
            if best is None:
                break
            _, i, start, before, after = best
            old = current[i]
            current[i] = replace(old, installed_interval_mm=(start, start + lengths[i]))
            trace.append({"stage": "fixed_length_shift", "direction": str(direction), "bar_id": old.id,
                          "old_start_mm": old.installed_interval_mm[0], "new_start_mm": start,
                          "pair_reduction": before - after})
        output.extend(current)
    return tuple(output)


def _resize(bar, length, source):
    center = sum(bar.installed_interval_mm) / 2
    changed = replace(bar, installed_interval_mm=(center - length / 2, center + length / 2))
    lower, upper = _window(changed, source)
    start = min(upper, max(lower, changed.installed_interval_mm[0]))
    return replace(changed, installed_interval_mm=(start, start + length))


def _exchange(bars, source, config, budget, stock, trace):
    if not config.maximum_exchange_attempts:
        return bars, stock
    pairs = _conflicts(bars, budget)
    blocked = {(b.direction, b.id) for pair in pairs for b in pair}
    # An overlap of the mandatory source+40d cores cannot be repaired by merely
    # reallocating stock surplus. Only the non-mandatory remainder is targeted.
    target_keys = set()
    for first, second in pairs:
        a, b = _window(first, source), _window(second, source)
        core_a = (a[1], a[0] + first.installed_length_mm)
        core_b = (b[1], b[0] + second.installed_length_mm)
        if min(core_a[1], core_b[1]) - max(core_a[0], core_b[0]) <= TOL:
            target_keys.update((v.direction, v.id) for v in bars if _near(first, v) or _near(second, v))
    proposals = []
    for target in bars:
        if (target.direction, target.id) not in target_keys:
            continue
        lower, upper = _window(target, source)
        required = target.installed_length_mm - (upper - lower)
        for shorter in PLATE_11700_CUT_LENGTHS_MM:
            if shorter < required - TOL or shorter >= target.installed_length_mm - TOL:
                continue
            delta = target.installed_length_mm - shorter
            for donor in bars:
                budget.check()
                if ((donor.direction, donor.id) == (target.direction, target.id)
                        or (donor.direction, donor.id) in blocked or donor.steel_class != target.steel_class
                        or donor.diameter_mm != target.diameter_mm):
                    continue
                longer = next((v for v in PLATE_11700_CUT_LENGTHS_MM if abs(v - donor.installed_length_mm - delta) <= TOL), None)
                if longer is None:
                    continue
                swap = abs(donor.installed_length_mm - shorter) <= TOL and abs(longer - target.installed_length_mm) <= TOL
                budget.check(len(bars))
                degree = sum(_near(donor, b) for b in bars) - 1
                row = (not swap, degree, shorter, str(donor.direction), donor.id, target.id, longer, target, donor)
                # Retain only the deterministic best finite proposal set. Never
                # allocate every O(targets*donors*lengths) candidate in memory.
                insort(proposals, row, key=lambda value: value[:7])
                if len(proposals) > config.maximum_exchange_attempts:
                    proposals.pop()
    for attempt, row in enumerate(proposals):
        budget.check()
        _, _, shorter, _, _, _, longer, target, donor = row
        changed = {(target.direction, target.id): _resize(target, shorter, source),
                   (donor.direction, donor.id): _resize(donor, longer, source)}
        candidate = tuple(changed.get((b.direction, b.id), b) for b in bars)
        movement = []
        candidate = _shift(candidate, source, budget, movement)
        if len(_conflicts(candidate, budget)) >= len(pairs):
            continue
        if abs(_mass(candidate) - _mass(bars)) > TOL:
            raise ValueError("same-diameter length exchange changed total installed mass")
        groups, _ = _group_schedule(candidate)
        actual_stock = check_stock_cutting(build_bar_schedule(groups), time_limit_s=budget.seconds())
        if actual_stock["status"] != "pass":
            continue
        trace.append({"stage": "mass_neutral_length_exchange", "attempt": attempt + 1,
                      "target": [str(target.direction), target.id], "donor": [str(donor.direction), donor.id],
                      "old_lengths_mm": [target.installed_length_mm, donor.installed_length_mm],
                      "new_lengths_mm": [shorter, longer],
                      "exact_group_inventory_preserved": _group_schedule(bars)[0] == groups})
        trace.extend(movement)
        return candidate, actual_stock
    return bars, stock


def _tasks(pairs, source):
    tasks = []
    for first, second in pairs:
        a, b = _window(first, source), _window(second, source)
        mandatory = (max(a[1], b[1]), min(a[0] + first.installed_length_mm, b[0] + second.installed_length_mm))
        _, _, lower, upper = _source_bounds((first, second), source)
        tasks.append(PhysicalConflictTask(first.direction, first.id, second.id,
            abs(first.transverse_axis_mm - second.transverse_axis_mm),
            (max(first.installed_interval_mm[0], second.installed_interval_mm[0]),
             min(first.installed_interval_mm[1], second.installed_interval_mm[1])),
            mandatory if mandatory[1] - mandatory[0] > TOL else None,
            a[0] + first.installed_length_mm > b[1] + TOL and b[0] + second.installed_length_mm > a[1] + TOL,
            upper - lower, upper - lower > 11700 + TOL))
    return tuple(tasks)


def normalize_physical_bars(
    source_bars: tuple[PhysicalSourceBar, ...], *, config: PhysicalNormalizationConfig,
) -> PhysicalNormalizationResult:
    """Preserve complete source bars; return a checked incumbent on search limits.

Diameter replacement requires explicit opt-in. All unions retain full NEW 40d;
stock balance only lengthens; longitudinal moves preserve the inventory; the final
same-diameter exchange requires a fresh exact stock pass. No phase, source axis,
material, source reference or engineering exception is silently discarded.
"""
    _validate_config(config)
    source, original = _source_input(source_bars, config)
    initial_pairs = _conflicts(original, maximum_pair_checks=config.maximum_pair_checks)
    budget = _Budget(config)
    maximum_mass = config.maximum_mass_kg if config.maximum_mass_kg is not None else _mass(original)
    trace = []
    original_groups, _ = _group_schedule(original)
    original_stock = check_stock_cutting(build_bar_schedule(original_groups),
                                         time_limit_s=min(config.stock_balance_time_limit_s, config.time_limit_s))
    incumbent, incumbent_stock, incumbent_pairs = original, original_stock, initial_pairs

    def rank(bars, stock, pairs):
        catalog = all(any(abs(b.installed_length_mm - length) <= TOL for length in PLATE_11700_CUT_LENGTHS_MM) for b in bars)
        acceptable = stock["status"] == "pass" and catalog and _mass(bars) <= maximum_mass + TOL
        return not acceptable, len(pairs), _mass(bars), len(bars)

    def checkpoint(bars, stock):
        nonlocal incumbent, incumbent_stock, incumbent_pairs
        _certificate(bars, source)
        pairs = _conflicts(bars, budget)
        if rank(bars, stock, pairs) <= rank(incumbent, incumbent_stock, incumbent_pairs):
            incumbent, incumbent_stock, incumbent_pairs = tuple(bars), stock, pairs
        trace.append({"stage": "independent_checkpoint", "physical_bar_count": len(bars), "mass_kg": _mass(bars),
                      "body_intersection_pair_count": len(pairs), "stock_status": stock["status"]})

    exhausted = False
    if original:
        try:
            normalized = _whole_merge(original, source, config, budget, maximum_mass, trace)
            _certificate(normalized, source)
            balanced = _balance(normalized, config, budget, maximum_mass, trace)
            if balanced is not None:
                current, stock = balanced
                checkpoint(current, stock)
                merged = _pair_merge(current, source, config, budget, maximum_mass, trace)
                _certificate(merged, source)
                second_balance = _balance(merged, config, budget, maximum_mass, trace)
                if second_balance is not None:
                    current, stock = second_balance
                    checkpoint(current, stock)
                    shifted = _shift(current, source, budget, trace)
                    if _group_schedule(shifted)[0] != _group_schedule(current)[0]:
                        raise ValueError("fixed-length shifts changed the stock inventory")
                    checkpoint(shifted, stock)
                    exchanged, stock = _exchange(shifted, source, config, budget, stock, trace)
                    checkpoint(exchanged, stock)
        except _SearchLimit:
            exhausted = True
            trace.append({"stage": "search_budget_exhausted", "pair_checks": budget.pair_checks,
                          "complete_verified_incumbent_retained": True})
    certificate = _certificate(incumbent, source)
    final_pairs = _conflicts(incumbent, maximum_pair_checks=config.maximum_pair_checks)
    if final_pairs != incumbent_pairs:
        raise ValueError("final independent collision audit differs from saved incumbent")
    groups, _ = _group_schedule(incumbent)
    metrics = PhysicalNormalizationMetrics(_mass(incumbent), len(incumbent), len(groups), len(final_pairs),
        len({(b.direction, b.id) for pair in final_pairs for b in pair}))
    acceptable = not rank(incumbent, incumbent_stock, final_pairs)[0]
    status = "budget_exhausted" if exhausted else ("normalized" if incumbent != original else "unchanged")
    if not acceptable:
        status = "stock_not_solved" if incumbent_stock["status"] != "pass" else "input_limits_not_met"
    trace.append({"stage": "final", "search_pair_checks": budget.pair_checks,
                  "merge_operations": budget.merge_operations, "source_mass_cap_kg": maximum_mass,
                  "diameter_increase_opted_in": config.allow_diameter_increase})
    return PhysicalNormalizationResult(incumbent, metrics, incumbent_stock, tuple(trace), certificate,
                                       _tasks(final_pairs, source), status, exhausted)
