"""Finite straight translations from exact host runs and global FE obligations.

Candidate generation only: no length change, demand transfer or acceptance
certificate. Full body, original FE and joint collisions must be checked later.
"""
from __future__ import annotations

from dataclasses import replace
import math

from rebar.models import Axis
from ..contracts.physical import PhysicalBar
from ..services.physical_host_fit import _host_intervals


def straight_translation_candidates(previous, host, required, *,
        maximum_longitudinal_shift_mm, maximum_positions=32):
    """Return complete bars plus explicit finite-choice truncation information.

    `required` contains the original regions not served by any other sufficient
    current bar, not the old owner's immutable rectangle. Its bounding interval
    is only a necessary search restriction; final coverage uses full polygons.
    The zero-shift policy generates no additional candidates.
    """
    limit = maximum_longitudinal_shift_mm
    if (isinstance(limit, bool) or not isinstance(limit, (int, float))
            or not math.isfinite(limit) or not 0 <= limit <= 11700
            or type(maximum_positions) is not int or not 1 <= maximum_positions <= 256):
        raise ValueError("Finite bounded longitudinal search limits required")
    if not isinstance(previous, PhysicalBar) or not isinstance(required, tuple) or len(required) > 10000:
        raise ValueError("Typed bar and bounded immutable source obligations required")
    if not limit:
        return (), False
    along = 0 if previous.direction.axis is Axis.X else 1
    length = previous.installed_length_mm
    origin = previous.installed_interval_mm[0]
    lower, upper = origin-limit, origin+limit
    for diameter, step, shape in required:
        if (type(diameter) is not int or diameter <= 0 or type(step) is not int or step <= 0
                or not shape.is_valid or shape.is_empty or shape.area <= 0):
            raise ValueError("Valid positive original demand polygons required")
        lower = max(lower, shape.bounds[along+2]+40*previous.diameter_mm-length)
        upper = min(upper, shape.bounds[along]-40*previous.diameter_mm)
    if lower > upper:
        return (), False
    # Conservative common material across all sections. Search may miss a
    # height-specific candidate but cannot turn it into a false containment pass.
    material = host.sections[0].footprint
    for section in host.sections[1:]:
        material = material.intersection(section.footprint)
    starts = set()
    for first, last in _host_intervals(previous, material, host.side_cover_mm, 4096):
        lo, hi = max(lower, first), min(upper, last)
        if lo > hi:
            continue
        closest = min(max(origin, lo), hi)
        for value in (lo, hi, closest, (lo+hi)/2, lo+.1, hi-.1):
            if lo <= value <= hi and value != origin:
                starts.add(value)
    ordered = sorted(starts, key=lambda s: (abs(s-origin), s))
    return tuple(replace(previous, installed_interval_mm=(s, s+length))
                 for s in ordered[:maximum_positions]), len(ordered) > maximum_positions
