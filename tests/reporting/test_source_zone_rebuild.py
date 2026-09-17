"""Focused fail-closed checks for the source-only transverse rebuild adapter."""
from types import SimpleNamespace

from rebar.models import Axis
from rebar.models import Direction, Layer
from rebar.legend import parse_recipe
from rebar.optimization.contracts import DemandCell, DemandLevel, DemandMap, LayoutConstraints, LayoutProblem
from rebar.optimization.contracts.plate import PlateProblem
from rebar.optimization.contracts.placement import AxisPlacement, PeriodicAxisPattern, PatternedRebarSet, CompositeLayoutZone, RecipePlacement
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.cutting import PLATE_11700_BATCH_PROFILE, PLATE_11700_CUT_LENGTHS_MM
from rebar.application.composite_revit_export import build_composite_zone_revit_export
from rebar.reporting.source_zone_rebuild import _placement, _windows
from rebar.reporting.source_zone_rebuild import rebuild_source_graphics


def test_placement_preserves_depth_and_periodic_phase():
    raw = {"background": {"placement": {"pattern": {"period_mm": 300, "offsets_mm": [0]}, "origin_mm": 0,
            "axis_depth_from_face_mm": 31}}, "components": [{"placement": {"pattern": {"period_mm": 300,
            "offsets_mm": [100, 200]}, "origin_mm": 0, "axis_depth_from_face_mm": 47}}], "placement_source": "test"}
    result = _placement(raw)
    assert result.background.axis_depth_from_face_mm == 31
    assert result.additions[0].axis_depth_from_face_mm == 47
    assert result.additions[0].pattern.offsets_mm == (100, 200)


def test_windows_keeps_valid_endpoint_before_fe_event_cap():
    cells = tuple(SimpleNamespace(poly=((0, y), (1000, y), (1000, y + 1), (0, y + 1)), level_index=1) for y in range(100))
    level = SimpleNamespace(requires_extra=True)
    demand = SimpleNamespace(cells=cells, level=lambda _: level, direction=SimpleNamespace(axis=Axis.X))
    part = PatternedRebarSet(0, SimpleNamespace(), AxisPlacement(PeriodicAxisPattern(300, (100, 200)), 0),
        (0, 900), (0, 1000), 1000, 1800, 1950, 6, 1)
    zone = CompositeLayoutZone("z", demand.direction, 1, (0, 0, 1000, 100), SimpleNamespace(), SimpleNamespace(), (part,))
    constraints = SimpleNamespace(min_width_cells=2)
    windows = _windows(demand, constraints, zone)
    assert windows and windows[0][1] <= 0 <= windows[0][3]


def test_adapter_fails_closed_for_schema_case_and_source_cell_mismatch():
    original = {"schema_version": "wrong", "source_stage": "original-parametric-zones-before-physical-normalization",
                "units": "mm", "case_id": "case", "directions": []}
    problem = SimpleNamespace(case_id="case", direction_problems=())
    packet, check = rebuild_source_graphics(problem, original)
    assert packet == original and check["status"] == "not_checked"
    wrong_case = {**original, "schema_version": "source-isofields-zones/v1", "directions": [{}, {}, {}, {}]}
    packet, check = rebuild_source_graphics(SimpleNamespace(case_id="other", direction_problems=(1, 2, 3, 4)), wrong_case)
    assert packet == wrong_case and check["reason"] == "direction_contract"


def test_adapter_executes_real_plate_problem_with_four_background_directions():
    problems, rows = [], []
    for layer in (Layer.BOTTOM, Layer.TOP):
        for axis in (Axis.X, Axis.Y):
            direction = Direction(layer, axis)
            recipe = parse_recipe("s300d10")
            level = DemandLevel(0, 1, None, None, "s300d10", None, False, recipe)
            cell = DemandCell(1, ((0, 0), (1000, 0), (1000, 1000), (0, 1000)), (500, 500), 1, 0)
            demand = DemandMap(direction, (level,), (cell,), (0, 0, 1000, 1000))
            problems.append(LayoutProblem(demand, LayoutConstraints()))
            rows.append({"direction": {"layer": layer.value, "axis": axis.value}, "source_bbox_mm": [0, 0, 1000, 1000],
                         "cells": [{"cell_id": 1, "polygon_mm": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]], "aci": 1, "level_index": 0}],
                         "zone_drafts": []})
    packet = {"schema_version": "source-isofields-zones/v1", "source_stage": "original-parametric-zones-before-physical-normalization",
              "units": "mm", "case_id": "real-contract", "directions": rows}
    output, check = rebuild_source_graphics(PlateProblem(tuple(problems), case_id="real-contract"), packet)
    assert output["directions"] == packet["directions"] and "zone_rebuild" in output
    assert check["status"] == "pass" and check["inventory_unchanged"]
    assert check["before_metrics"] == check["after_metrics"] == {"zone_count": 0, "component_count": 0, "bar_count": 0, "mass_kg": 0, "position_count": 0}


def test_nonempty_transverse_relocation_preserves_batch_inventory_and_phase():
    background, extra = parse_recipe("s300d10"), parse_recipe("s300d10+s300d10")
    constraints = LayoutConstraints(min_width_cells=2, allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM, cutting_profile=PLATE_11700_BATCH_PROFILE)
    problems, rows, original = [], [], None
    for layer in (Layer.BOTTOM, Layer.TOP):
        for axis in (Axis.X, Axis.Y):
            direction = Direction(layer, axis)
            levels = (DemandLevel(0, 1, None, None, "s300d10", None, False, background),
                      DemandLevel(1, 2, None, None, "s300d10+s300d10", extra.additions[0], True, extra))
            cells, raw_cells = [], []
            for x in range(0, 4000, 500):
                for y in range(0, 3000, 500):
                    positive = direction == Direction(Layer.BOTTOM, Axis.X) and (x, y) == (1000, 2500)
                    poly = ((x, y), (x + 500, y), (x + 500, y + 500), (x, y + 500))
                    level = 1 if positive else 0
                    cells.append(DemandCell(len(cells), poly, (x + 250, y + 250), level + 1, level))
                    raw_cells.append({"cell_id": len(cells)-1, "polygon_mm": [list(point) for point in poly], "aci": level + 1, "level_index": level})
            demand = DemandMap(direction, levels, tuple(cells), (0, 0, 4000, 3000))
            problems.append(LayoutProblem(demand, constraints))
            zones = []
            if direction == Direction(Layer.BOTTOM, Axis.X):
                placement = RecipePlacement(AxisPlacement(PeriodicAxisPattern.uniform(300), 0, 31),
                    (AxisPlacement(PeriodicAxisPattern.uniform(300), 200, 47),), "synthetic")
                zone = build_composite_zone(demand, (1000, 2200, 1500, 3400), 1, "edge-zone", placement,
                    constraints=constraints, installed_lengths_mm=(1950,))
                zones = [build_composite_zone_revit_export(demand, zone, constraints=constraints)]
                original = zones[0]
            rows.append({"direction": {"layer": layer.value, "axis": axis.value}, "source_bbox_mm": [0, 0, 4000, 3000],
                         "cells": raw_cells, "zone_drafts": zones})
    packet = {"schema_version": "source-isofields-zones/v1", "source_stage": "original-parametric-zones-before-physical-normalization",
              "units": "mm", "case_id": "nonempty", "directions": rows}
    output, check = rebuild_source_graphics(PlateProblem(tuple(problems), case_id="nonempty"), packet)
    changed = output["directions"][0]["zone_drafts"][0]
    assert check["status"] == "pass" and check["before_outside_count"] == 1 and check["after_outside_count"] == 0
    assert changed["source_zone_id"] == original["source_zone_id"]
    assert changed["components"][0]["installed_length_mm"] == original["components"][0]["installed_length_mm"] == 1950
    assert changed["components"][0]["bar_count"] == original["components"][0]["bar_count"] == 4
    assert changed["components"][0]["placement"]["axis_depth_from_face_mm"] == 47
    assert output["zone_rebuild"] == check and check["directions"][0]["changes"]
    assert packet["directions"][0]["zone_drafts"][0] == original
    assert all(not row["zone_drafts"] for row in output["directions"][1:])
