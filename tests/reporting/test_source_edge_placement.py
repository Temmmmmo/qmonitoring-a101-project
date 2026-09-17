from rebar.reporting.source_edge_placement import apply_source_edge_placement


def packet():
    cells = [{"polygon_mm": [[0, 0], [2000, 0], [2000, 1000], [0, 1000]]}]
    directions = []
    for layer, axis in (("bottom", "X"), ("bottom", "Y"), ("top", "X"), ("top", "Y")):
        component = {"component_index": 0, "diameter_mm": 10, "installed_length_mm": 1300,
            "bar_axis_bbox_mm": [-100, 100, 1200, 200] if axis == "X" else [100, -100, 200, 1200],
            "straight_bar_body_bbox_mm": [-100, 95, 1200, 205] if axis == "X" else [95, -100, 205, 1200]}
        zone = {"source_zone_id": "z", "demand_bbox_mm": [100, 100, 200, 200], "components": [component],
                "checks": {"geometry_and_schedule": "pass"}}
        directions.append({"direction": {"layer": layer, "axis": axis}, "cells": cells, "zone_drafts": [zone]})
    return {"schema_version": "source-isofields-zones/v1", "units": "mm", "source_stage": "original-parametric-zones-before-physical-normalization", "directions": directions, "note": "x"}


def test_adapter_is_immutable_idempotent_and_keeps_body_synced():
    original = packet()
    shifted, check = apply_source_edge_placement(original)
    assert original["directions"][0]["zone_drafts"][0]["components"][0]["bar_axis_bbox_mm"][0] == -100
    component = shifted["directions"][0]["zone_drafts"][0]["components"][0]
    original_component = original["directions"][0]["zone_drafts"][0]["components"][0]
    assert component["bar_axis_bbox_mm"][0] - original_component["bar_axis_bbox_mm"][0] == component["straight_bar_body_bbox_mm"][0] - original_component["straight_bar_body_bbox_mm"][0]
    assert shifted["directions"][0]["zone_drafts"][0]["checks"]["geometry_and_schedule"] == "not_checked"
    again, _ = apply_source_edge_placement(shifted)
    assert again["directions"] == shifted["directions"] and check["placed_count"] > 0


def test_empty_or_multipart_is_not_fake_pass():
    value = packet()
    value["directions"][0]["cells"] = []
    _, check = apply_source_edge_placement(value)
    assert check["unknown_count"] == 1
    assert check["total_extension_status"] == check["longitudinal_core_containment_status"] == "not_checked"


def test_forged_metadata_does_not_skip_recalculation_and_body_is_optional():
    value = packet()
    value["edge_placement"] = {"policy_id": "source-edge-total-80d/research-v1", "after_outside_count": 0}
    del value["directions"][0]["zone_drafts"][0]["components"][0]["straight_bar_body_bbox_mm"]
    shifted, check = apply_source_edge_placement(value)
    assert check["component_count"] == 4 and check["checks"][0]["before_bounds_mm"][0] == -100
    assert "straight_bar_body_bbox_mm" not in shifted["directions"][0]["zone_drafts"][0]["components"][0]


def test_holes_are_ignored_only_by_packet_adapter_policy():
    value = packet()
    value["directions"][0]["cells"] = [
        {"polygon_mm": [[0, 0], [2000, 0], [2000, 1000], [0, 1000]]},
        {"polygon_mm": [[900, 0], [1100, 0], [1100, 300], [900, 300]]},
    ]
    _, check = apply_source_edge_placement(value)
    assert check["unknown_count"] == 0
