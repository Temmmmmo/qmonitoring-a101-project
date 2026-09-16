"""Explicit non-normative auto-depth policy and current-native revalidation."""

from copy import deepcopy
import importlib
from pathlib import Path
import runpy
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "integrations/pyrevit/QMonitoring.extension/lib"))
review = importlib.import_module("qm_rebar_review")


def case(diameter=12, thickness=200):
    doubles = runpy.run_path(str(ROOT / "tests/integrations/test_rebar_review_mvp.py"))
    p = doubles["primitives"]()
    for bar in p["bars"]:
        bar["diameter_mm"] = diameter
    types = {
        review.material_key(b): dict(
            element_id=7, nominal_diameter_mm=diameter, model_diameter_mm=diameter
        )
        for b in p["bars"]
    }
    host = dict(bottom_z_mm=0, top_z_mm=thickness, thickness_mm=thickness)
    return p, host, types


def test_formula_separate_layer_dmax_and_no_native_cover():
    p, h, types = case()
    for bar in p["bars"]:
        if bar["direction"].startswith("top"):
            bar["diameter_mm"] = 16
    types["A500|16"] = dict(element_id=8, nominal_diameter_mm=16, model_diameter_mm=16)
    h["cover_metadata"] = {"unreadable": "must not be used"}
    policy = review.computed_axis_depth_policy(p, h, types)
    assert [policy["computed_depths_mm"][d] for d in review.DIRECTIONS] == [46, 62, 48, 68]
    assert min(policy["minimum_vertical_body_gap_mm_by_pair"].values()) == 4
    assert policy["native_cover_used"] is False and policy["engineering_approval"] is False


def test_empty_direction_never_invents_an_x_layer_or_bar():
    p, h, t = case()
    p["bars"] = [b for b in p["bars"] if b["direction"] == "bottom-Y"]
    policy = review.computed_axis_depth_policy(p, h, t)
    assert policy["computed_depths_mm"]["bottom-Y"] == 46
    assert set(policy["absolute_axis_z_mm_by_direction"]) == {"bottom-Y"}
    assert len(p["bars"]) == 1


def test_large_diameter_profile_requires_native_thickness_and_positive_gap():
    p, h, t = case(36, 236)
    assert list(review.computed_axis_depth_policy(p, h, t)["computed_depths_mm"].values()) == [
        58,
        98,
        58,
        98,
    ]
    h.update(top_z_mm=235, thickness_mm=235)
    with pytest.raises(ValueError, match="4 mm"):
        review.computed_axis_depth_policy(p, h, t)
    h.update(top_z_mm=100, thickness_mm=100)
    with pytest.raises(ValueError, match="thickness"):
        review.computed_axis_depth_policy(p, h, t)


def test_normal_auto_selection_never_prompts_manual_and_manual_cancel_blocks():
    p, h, t = case()

    def forbidden(proposal):
        raise AssertionError("manual prompt in normal auto flow")

    policy = review.select_axis_depth_policy(p, h, t, lambda *_: "auto", forbidden)
    assert policy["mode"] == "auto" and policy["user_confirmed"] is True
    with pytest.raises(ValueError, match="cancelled"):
        review.select_axis_depth_policy(p, h, t, lambda *_: "manual", lambda _: None)
    manual = review.select_axis_depth_policy(p, h, t, lambda *_: "manual", lambda _: "50;70;50;70")
    assert manual["automatic_gap_policy_applied"] is False


@pytest.mark.parametrize("change", ["depth", "host", "type", "profile"])
def test_auto_policy_recomputed_not_metadata_pass(change):
    p, h, t = case()
    policy = review.select_axis_depth_policy(p, h, t, lambda *_: "auto", lambda _: None)
    depths = deepcopy(policy["actual_depths_mm"])
    if change == "depth":
        depths["bottom-Y"] -= 1
    elif change == "host":
        h["top_z_mm"] += 1
    elif change == "type":
        t["A500|12"]["model_diameter_mm"] += 0.0005
    else:
        policy["profile_id"] = "fake-pass"
    with pytest.raises(ValueError):
        review.validate_axis_depth_policy(p, h, t, depths, policy)
