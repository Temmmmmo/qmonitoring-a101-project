"""Input geometry is decoded explicitly; no implicit fallback curve shapes."""
from copy import deepcopy
import importlib

import pytest


@pytest.fixture
def cli(monkeypatch):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]/"scripts"))
    return importlib.import_module("experiment_tz_outer_scope")


def record():
    return {"id": "a", "direction": "top-X", "steel_class": "A500", "diameter_mm": 10,
        "source_bar_ids": ["zone/0/0"], "segments": [{"kind": "Line3D",
        "start_mm": [0, 100, 150], "end_mm": [2925, 100, 150]}], "shape_kind": "straight",
        "demand_segment_indexes": [0], "geometry_profile_id": "straight-centreline/research-v1",
        "placement_profile_id": "profile", "selected_cut_length_mm": 2925}


def test_complete_segment_data_roundtrips_without_mutating_input(cli):
    source = record()
    original = deepcopy(source)
    bars = cli.decode_shaped_bars([source])
    assert isinstance(bars, tuple) and bars[0].segments[0].end_mm == (2925, 100, 150)
    assert str(bars[0].direction) == "top-X"
    assert source == original


@pytest.mark.parametrize("mutation", ("unsupported", "missing", "empty", "direction"))
def test_malformed_input_does_not_guess_a_curve_or_direction(cli, mutation):
    source = record()
    if mutation == "unsupported":
        source["segments"][0]["kind"] = "Spline"
    elif mutation == "missing":
        del source["segments"][0]["end_mm"]
    elif mutation == "empty":
        source["segments"] = []
    else:
        source["direction"] = "arbitrary"
    with pytest.raises((ValueError, KeyError)):
        cli.decode_shaped_bars([source])
