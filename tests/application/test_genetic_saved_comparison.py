"""Сохранённые эксперименты нельзя смешать молча или считать пустоту нулевым gap."""

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

load_series = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts" / "compare_genetic_fronts.py"),
)["load_series"]


def _payload():
    return {
        "reference": {"id": "test"},
        "runs": [{"run_id": "run", "population_size": 8, "generations": 3, "random_seed": 7,
                  "operator_policy": "uniform", "complexity_axis": "physical_bar_count"}],
        "candidates": [{"run_id": "run", "valid": True, "under_reinforced_cell_count": 0}],
    }


def test_saved_series_checks_matching_budgets_and_source_fingerprint(tmp_path):
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(_payload()))
    reference, runs, provenance = load_series((path,), 3)
    assert reference["id"] == "test"
    assert len(runs) == 1
    assert len(provenance[0]["sha256"]) == 64
    assert load_series((path,), 12)[1] == {}
    assert load_series((path,), 3, local_search_passes=4)[1] == {}
    with pytest.raises(ValueError, match="неоднозначная"):
        load_series((path, path), 3)


@pytest.mark.parametrize("tamper", ["empty", "invalid", "undercoverage"])
def test_saved_series_refuses_inadmissible_or_empty_front(tmp_path, tamper):
    payload = _payload()
    if tamper == "empty":
        payload["candidates"] = []
    elif tamper == "invalid":
        payload["candidates"][0]["valid"] = False
    else:
        payload["candidates"][0]["under_reinforced_cell_count"] = 1
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="пустой|невалидные"):
        load_series((path,), 3)


def test_cli_refuses_different_normalized_inputs_before_creating_report(tmp_path):
    paths = []
    for label in ("before", "after"):
        payload = _payload()
        payload["runs"][0]["normalized_input_sha256"] = label
        payload["candidates"][0].update(complexity=10, total_mass_kg=20)
        path = tmp_path / f"{label}.json"
        path.write_text(json.dumps(payload))
        paths.append(path)
    destination = tmp_path / "report"
    completed = subprocess.run([
        sys.executable, str(Path(__file__).resolve().parents[2] / "scripts" / "compare_genetic_fronts.py"),
        "--before", str(paths[0]), "--after", str(paths[1]), "--out-dir", str(destination),
    ], capture_output=True, text=True, check=False)
    assert completed.returncode != 0
    assert "инженерные ограничения" in completed.stderr
    assert not destination.exists()
