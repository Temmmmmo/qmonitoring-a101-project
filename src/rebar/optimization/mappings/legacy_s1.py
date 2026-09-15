"""S1 t800: explicit PNG recipes, matched to all four original DXF scales."""
from math import pi

from rebar.models import Rebar
from rebar.standards import A101_243_FOUNDATION_T800_900

from .contracts import RebarBandMapping, RebarMapping

_BACKGROUND = Rebar(step=300, diameter=18)
_ADDITIONS = (None, (18, 300), (18, 150), (20, 150), (20, 100),
              (25, 100), (28, 100), (32, 100), (36, 100))


def _band(addition):
    extra = Rebar(diameter=addition[0], step=addition[1]) if addition else None
    label = "s300d18" + ("+s{}d{}".format(extra.step, extra.diameter) if extra else "")
    capacity = pi * 18**2 / 4 * 10 / 300
    if extra:
        capacity += pi * extra.diameter**2 / 4 * 10 / extra.step
    return RebarBandMapping(label, capacity, _BACKGROUND, extra)


LEGACY_S1_D18 = RebarMapping(
    id="legacy-s1-t800-d18-v1",
    source="1-КЖ00.С1-2: four С1_t_800 PNG legends; full nine-band lower-X legend; "
           "four original DXF scales checked 2026-09-15. A101 table 2.4.3 t800-900.",
    status="mvp_assumption",
    expected_scale_bounds_as=(6.7, 8.5, 17, 25, 29, 40, 58, 70, 89, 110),
    bands=tuple(_band(addition) for addition in _ADDITIONS),
    a101_profile_id=A101_243_FOUNDATION_T800_900.id,
)
