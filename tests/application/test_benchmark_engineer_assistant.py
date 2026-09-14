"""Economic selection remains separate from full engineering approval."""
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    return importlib.import_module("benchmark_engineer_assistant")


def _candidate(identifier, mass, bars, *, valid=True, missing=0):
    return {"id": identifier, "valid": valid,
            "metrics": {"total_mass_kg": mass, "physical_bar_count": bars,
                        "under_reinforced_cell_count": missing}}


def test_selection_does_not_admit_undercoverage_or_invalid_cheap_candidate(runner):
    candidates = [_candidate("invalid", 1, 1, valid=False), _candidate("missing", 2, 2, missing=1),
                  _candidate("mass", 90, 120), _candidate("balanced", 105, 100),
                  _candidate("fewest", 114, 80), _candidate("too-heavy", 116, 40)]
    assert runner.select_candidates(candidates, 100, 100) == {
        "minimum_mass": "mass", "minimum_bars_under_15pct_mass": "fewest",
        "minimum_mass_without_extra_bars": "balanced",
    }


def test_selection_reports_no_available_economic_choice(runner):
    assert runner.select_candidates([_candidate("only", 120, 101)], 100, 100) == {
        "minimum_mass": "only", "minimum_bars_under_15pct_mass": None,
        "minimum_mass_without_extra_bars": None,
    }
    assert all(value is None for value in runner.select_candidates([], 100, 100).values())


def test_selection_is_stable_at_equal_metrics(runner):
    candidates = [_candidate("b", 100, 100), _candidate("a", 100, 100)]
    assert set(runner.select_candidates(candidates, 100, 100).values()) == {"a"}


def test_benchmark_refuses_existing_output_before_reading_materials(runner, tmp_path):
    target = tmp_path / "saved.json"
    target.write_text("saved", encoding="utf-8")
    with pytest.raises(SystemExit):
        runner.main(["k09-typical-3-14", "--output", str(target)])
    assert target.read_text(encoding="utf-8") == "saved"


@pytest.mark.parametrize("option,value", [("--population", "1"), ("--population", "129"),
                                         ("--generations", "0"), ("--generations", "101")])
def test_benchmark_rejects_unbounded_or_empty_search(runner, tmp_path, option, value):
    with pytest.raises(SystemExit):
        runner.main(["k09-typical-3-14", "--output", str(tmp_path / "new.json"), option, value])
