"""Проверки политик анкеровки и проектных длин отрезков."""

import pytest

from rebar import Rebar
from rebar.optimization import LayoutConstraints, build_layout_problem, build_zone
from rebar.optimization.services import (
    PLATE_11700_CATALOG,
    FixedDiameterAnchoragePolicy,
)


def test_fixed_diameter_anchorage_policy_is_explicit():
    policy = FixedDiameterAnchoragePolicy(40)

    assert policy.name == "fixed-diameter"
    assert policy.extension_each_end_mm(Rebar(step=100, diameter=20)) == 800


def test_builder_rounds_anchored_length_to_explicit_catalog(mosaic_with_legend):
    problem = build_layout_problem(
        mosaic_with_legend,
        LayoutConstraints(
            min_width_cells=1,
            allowed_cut_lengths_mm=(2500.0, 3000.0, 3900.0),
            cutting_profile="test-catalog",
        ),
    )

    zone = build_zone(problem, (1,), 1, "cut")

    assert zone.required_length_mm == 500
    assert zone.anchored_length_mm == 2500
    assert zone.installed_length_mm == 2500
    assert zone.meta["cutting_profile"] == "test-catalog"


def test_catalog_selects_next_available_length():
    assert PLATE_11700_CATALOG.select_length_mm(3900.0) == 3900.0
    assert PLATE_11700_CATALOG.select_length_mm(3900.1) == 4875.0


def test_builder_rejects_length_above_catalog(mosaic_with_legend):
    problem = build_layout_problem(
        mosaic_with_legend,
        LayoutConstraints(
            min_width_cells=1,
            allowed_cut_lengths_mm=(1170.0, 1950.0),
            cutting_profile="too-short",
        ),
    )

    with pytest.raises(ValueError, match="превышает максимум каталога"):
        build_zone(problem, (1,), 1, "too-long")
