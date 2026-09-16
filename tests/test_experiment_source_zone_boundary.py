"""Small contracts for the read-only source-frame boundary experiment."""
import runpy
from pathlib import Path


EXPERIMENT = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/experiment_source_zone_boundary.py"))
inspect_packet = EXPERIMENT["inspect_packet"]


def packet(bounds, demand=(100, 100, 900, 200), axis="X", length=1110):
    return {"source_stage": "original-parametric-zones-before-physical-normalization", "units": "mm", "directions": [{
        "direction": {"layer": "bottom", "axis": axis}, "cells": [{"polygon_mm": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]]}],
        "zone_drafts": [{"source_zone_id": "Z", "demand_bbox_mm": list(demand), "components": [{
            "diameter_mm": 10, "installed_length_mm": length, "bar_axis_bbox_mm": list(bounds)}]}]}]}


def test_outside_current_frame_but_tight_frame_inside_is_separate_repair_case():
    result = inspect_packet("test", packet((-100, 100, 1100, 200), (400, 100, 600, 200), length=1200), .01)
    assert result["existing_outside"] == result["outside_tight_inside"] == 1
    assert result["has_shorter_catalogue_candidate"] == 1


def test_expansion_at_edge_keeps_base_frame_inside_but_tight_frame_outside():
    result = inspect_packet("test", packet((-10, 100, 1010, 200)), .01)
    assert result["outside_tight_outside_base_inside"] == 1
    assert result["outside_tight_outside_base_outside"] == 0


def test_nonrectangular_or_transverse_fit_is_separate_from_edge_expansion():
    result = inspect_packet("test", packet((0, -10, 1000, 200)), .01)
    assert result["outside_tight_outside_base_outside"] == 1
    assert result["outside_tight_outside_base_inside"] == 0


def test_y_tight_frame_changes_only_the_longitudinal_axis():
    frame = EXPERIMENT["frame"]({"bar_axis_bbox_mm": [10, 20, 30, 40]}, [0, 100, 50, 200], "Y", 400)
    assert frame.bounds == (10.0, -300.0, 30.0, 600.0)
