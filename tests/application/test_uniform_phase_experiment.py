"""The opt-in phase report rereads source files and distrusts stored metric flags."""

import importlib.util
import json
from pathlib import Path

import pytest

from rebar.application import layout_snapshot
from test_layout_snapshot import snapshot as shared_snapshot_fixture


@pytest.fixture(name="snapshot")
def phase_snapshot_fixture(tmp_path, monkeypatch, direction_mosaic):
    return shared_snapshot_fixture.__wrapped__(tmp_path, monkeypatch, direction_mosaic)


@pytest.fixture
def experiment_module(snapshot, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/experiment_uniform_phase_repair.py"
    spec = importlib.util.spec_from_file_location("uniform_phase_experiment_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load_direction_mosaic", layout_snapshot.load_direction_mosaic)
    return module


def test_phase_experiment_keeps_complete_demand_and_does_not_authorize_placement(snapshot, experiment_module):
    path, data = snapshot
    report = experiment_module.experiment(path, "plate:1")
    assert len(report["directions"]) == 4
    assert report["candidate_id"] == "plate:1"
    assert report["after_metrics"]["total_mass_kg"] == data["candidates"][0]["metrics"]["total_mass_kg"]
    assert report["after_metrics"]["physical_bar_count"] == report["before_metrics"]["physical_bar_count"]
    assert report["after_metrics"]["under_reinforced_cell_count"] == 0
    assert not report["placement_eligible"]
    assert "@150" in report["scope"]


@pytest.mark.parametrize("change", ("source", "constraints", "demand", "mass", "duplicate_candidate", "missing_direction"))
def test_phase_experiment_rejects_changed_sources_or_forged_snapshot(snapshot, experiment_module, change):
    path, data = snapshot
    if change == "source":
        Path(data["source_dxf"][0]["path"]).write_bytes(b"changed source")
    elif change == "constraints":
        data["problems"]["direction_problems"][0]["constraints"]["anchorage_diameters"] = 15
    elif change == "demand":
        data["problems"]["direction_problems"][0]["demand"]["cells"][0]["level_index"] = 1
    elif change == "mass":
        data["candidates"][0]["solution"]["direction_solutions"][0]["solution"]["metrics"]["total_mass_kg"] += 1
    elif change == "duplicate_candidate":
        data["candidates"].append(data["candidates"][0])
    else:
        data["candidates"][0]["solution"]["direction_solutions"].pop()
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        experiment_module.experiment(path, "plate:1")


def test_phase_experiment_cli_refuses_to_overwrite_existing_report(snapshot, experiment_module, tmp_path, monkeypatch):
    output = tmp_path / "existing.json"
    output.write_text("keep this report", encoding="utf-8")
    monkeypatch.setattr(experiment_module.sys, "argv", ["experiment", str(snapshot[0]),
        "--candidate-id", "plate:1", "--output", str(output)])
    with pytest.raises(SystemExit) as caught:
        experiment_module.main()
    assert caught.value.code == 2
    assert output.read_text(encoding="utf-8") == "keep this report"
