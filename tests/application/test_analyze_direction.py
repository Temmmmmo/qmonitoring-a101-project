"""Тесты прикладного сценария одного DXF."""

from __future__ import annotations

import importlib

import pytest

from rebar.application import (
    analyze_direction,
    available_cutting_profile_ids,
    available_mapping_ids,
)

scenario = importlib.import_module("rebar.application.analyze_direction")


def test_analyze_direction_runs_selected_algorithm(monkeypatch, direction_mosaic):
    direction_mosaic.cells[0].band = direction_mosaic.legend[1]
    direction_mosaic.cells[0].aci = direction_mosaic.legend[1].aci
    monkeypatch.setattr(scenario, "read_mosaic", lambda *_args, **_kwargs: direction_mosaic)

    analysis = analyze_direction(
        "Нижнее армирование вдоль ОСИ Х.dxf",
        algorithm_names=("bbox",),
        max_details=2,
        min_width_cells=1,
    )

    assert analysis.mosaic is direction_mosaic
    assert analysis.problem.constraints.min_width_cells == 1
    assert [solution.algorithm for solution in analysis.solutions] == ["bbox"]
    assert analysis.solutions[0].request.max_details == 2
    assert analysis.front is not None
    assert len(analysis.front.candidates) == 1
    assert analysis.front.candidates[0].solution.algorithm == "bbox"


def test_analyze_direction_validates_choices_before_reading(monkeypatch):
    def fail_read(*_args, **_kwargs):
        raise AssertionError("DXF не должен читаться при ошибочных параметрах")

    monkeypatch.setattr(scenario, "read_mosaic", fail_read)

    with pytest.raises(ValueError, match="хотя бы один"):
        analyze_direction("Нижняя по Х.dxf", algorithm_names=())
    with pytest.raises(ValueError, match="неизвестные алгоритмы"):
        analyze_direction("Нижняя по Х.dxf", algorithm_names=("magic",))
    with pytest.raises(ValueError, match="неизвестный профиль раскроя"):
        analyze_direction("Нижняя по Х.dxf", cutting_profile="magic")
    with pytest.raises(ValueError, match="одновременно"):
        analyze_direction(
            "Нижняя по Х.dxf",
            shk_path="scale.shk",
            mapping_id="plate-zero-d12-v1",
        )


def test_manual_mapping_catalog_is_explicit():
    assert available_mapping_ids() == (
        "k09-above-3-d10-v1",
        "k09-minus-2-d12-v1",
        "plate-zero-d12-v1",
    )
    assert available_cutting_profile_ids() == ("continuous", "plate-11700")


def test_analyze_direction_applies_explicit_cutting_profile(monkeypatch, direction_mosaic):
    monkeypatch.setattr(scenario, "read_mosaic", lambda *_args, **_kwargs: direction_mosaic)

    analysis = analyze_direction(
        "Нижнее армирование вдоль ОСИ Х.dxf",
        algorithm_names=("bbox",),
        min_width_cells=1,
        cutting_profile="plate-11700",
    )

    assert analysis.problem.constraints.cutting_profile == "plate-11700"
    assert analysis.problem.constraints.allowed_cut_lengths_mm[-1] == 11700


def test_production_preserves_isolated_demand_until_explicit_sto_processing(monkeypatch, direction_mosaic):
    monkeypatch.setattr(scenario, "read_mosaic", lambda *_a, **_k: direction_mosaic)
    original = analyze_direction("Нижнее армирование вдоль ОСИ Х.dxf", algorithm_names=("bbox",),
                                 min_width_cells=1)
    research = analyze_direction("Нижнее армирование вдоль ОСИ Х.dxf", algorithm_names=("bbox",),
                                 min_width_cells=1, single_cell_policy="legacy-research")
    assert [c.level_index for c in original.problem.demand.cells] == [0, 1]
    assert original.problem.meta["single_cell_preprocessing"]["changed_count"] == 0
    assert research.problem.meta["single_cell_preprocessing"]["changed_count"] == 1
    from rebar.application import assess_layout_gates
    checks = {c.id: c for c in assess_layout_gates(research.problem, research.solutions[0]).items}
    assert checks["source-demand-preserved"].status == "fail"
