"""Regression checks for the read-only source-zone tightening experiment."""
import runpy
from pathlib import Path

from shapely.geometry import box


EXPERIMENT = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/experiment_source_zone_tightening.py"))
demand_from = EXPERIMENT["demand_from"]
raw_zone = EXPERIMENT["raw_zone"]
component_box = EXPERIMENT["component_box"]
SPLIT = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/experiment_source_zone_split.py"))
split_probe = SPLIT["split_probe"]
EDGE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/experiment_source_zone_edge_redistribution.py"))
choose_shift = EDGE["choose_shift"]
shifted_frame = EDGE["shifted_frame"]


def source_row():
    return {"direction": {"layer": "bottom", "axis": "X"}, "source_bbox_mm": [0, 0, 1000, 800],
        "legend": [{"level_index": 0, "aci": 1, "label": "s300d10"},
                   {"level_index": 1, "aci": 2, "label": "s300d10+s150d10"}],
        "cells": [{"cell_id": 1, "polygon_mm": [[0, 0], [1000, 0], [1000, 400], [0, 400]], "aci": 2, "level_index": 1},
                  {"cell_id": 2, "polygon_mm": [[0, 400], [1000, 400], [1000, 800], [0, 800]], "aci": 2, "level_index": 1}]}


def source_zone():
    placement = {"pattern": {"period_mm": 300, "offsets_mm": [0]}, "origin_mm": 0, "axis_depth_from_face_mm": None}
    addition = {"pattern": {"period_mm": 300, "offsets_mm": [100, 200]}, "origin_mm": 0, "axis_depth_from_face_mm": None}
    return {"source_zone_id": "Z", "level_index": 1, "demand_bbox_mm": [100, 0, 200, 800],
        "background": {"placement": placement}, "placement_source": "test", "components": [{
            "placement": addition, "installed_length_mm": 1300, "bar_axis_bbox_mm": [-400, 100, 900, 800],
            "bar_count": 6, "mass_kg": 1.0}]}


def test_raw_rehydration_preserves_longer_catalogue_length_and_actual_axes():
    zone = raw_zone(demand_from(source_row()), source_zone())
    component = zone.components[0]
    assert component.installed_length_mm == 1300
    assert component_box(zone, component).bounds == (-400.0, 100.0, 900.0, 800.0)


def test_source_row_recovers_background_without_addition_and_extra_level():
    demand = demand_from(source_row())
    assert demand.level(0).requires_extra is False
    assert demand.level(1).requires_extra is True


def tall_source(rows=5):
    value = source_row()
    value["source_bbox_mm"] = [0, 0, 1000, rows * 400]
    value["cells"] = [{"cell_id": index, "polygon_mm": [[0, index*400], [1000, index*400], [1000, (index+1)*400], [0, (index+1)*400]],
                       "aci": 2, "level_index": 1} for index in range(rows)]
    return value


def tall_zone(rows=5):
    value = source_zone()
    value["demand_bbox_mm"] = [100, 0, 200, rows * 400]
    value["components"][0]["bar_axis_bbox_mm"] = [-400, 100, 900, rows * 400]
    return value


def test_split_probe_rejects_children_below_existing_two_cell_minimum():
    demand = demand_from(source_row())
    result = split_probe(demand, (raw_zone(demand, source_zone()),), 0, box(0, 0, 1000, 800))
    assert result["accepted"] == []
    assert result["rejected"]["detailing_or_minwidth"] > 0


def test_split_probe_rejects_axis_changes_or_children_still_outside():
    demand = demand_from(tall_source())
    result = split_probe(demand, (raw_zone(demand, tall_zone()),), 0, box(0, 0, 1000, 2000))
    assert result["accepted"] == []
    assert result["rejected"]["changed_or_duplicate_axes"] > 0
    assert result["rejected"]["children_outside"] > 0


def test_edge_redistribution_keeps_core_inside_while_moving_total_tail_inward():
    border = box(0, 0, 1400, 300)
    source_bbox = [-400, 100, 900, 200]
    shift = choose_shift(border, source_bbox, EXPERIMENT["Axis"].X, (100, 200), 1300, 400)
    assert shift is not None
    assert border.covers(shifted_frame(source_bbox, EXPERIMENT["Axis"].X, shift))


def test_edge_redistribution_can_trade_per_end_40d_for_total_80d():
    border = box(0, 0, 180, 300)
    shift = choose_shift(border, [-40, 100, 140, 200], EXPERIMENT["Axis"].X, (0, 100), 180, 40)
    assert shift == 40
    assert (0 - (-40 + shift), (140 + shift) - 100) == (0, 80)


def test_edge_redistribution_rejects_insufficient_total_or_missing_fit():
    border = box(0, 0, 180, 300)
    assert choose_shift(border, [-35, 100, 135, 200], EXPERIMENT["Axis"].X, (0, 100), 170, 40) is None
    assert choose_shift(box(0, 0, 160, 300), [-40, 100, 140, 200], EXPERIMENT["Axis"].X, (0, 100), 180, 40) is None
