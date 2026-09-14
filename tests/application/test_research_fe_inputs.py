"""Shared post-FE research input chain: real I/O/decoding, mocked expensive proofs.

These tests do not run a search, reuse an archived cutting witness for acceptance,
or treat restoration as structural placement permission.
"""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from rebar.application.physical_layout_recovery import _bytes, _raw
from rebar.optimization.contracts.physical import PhysicalBar
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS


FLAGS = ("placement_eligible", "structural_placement_supported", "engineering_approval",
    "source_demand_removed", "source_demand_values_changed")


@pytest.fixture
def loader(monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    name = "research_fe_inputs_test"
    spec = importlib.util.spec_from_file_location(name, scripts / "research_fe_inputs.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def inputs(loader, tmp_path, monkeypatch):
    shifted = tmp_path / "shifted"
    shifted.mkdir()
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_bytes(b"original immutable snapshot")
    host_path = tmp_path / "host.json"
    host_path.write_bytes(_bytes({"host": "fixture"}))
    snapshot_record = loader.source_record(snapshot, role="source-snapshot")
    host_record = loader.source_record(host_path, role="working-host-snapshot")
    upstream = []
    for name in ("experiment.json", "shifted-patterned-analysis.json",
                 "normalization-report.json", "normalized-physical-review.json"):
        path = shifted / name
        path.write_bytes(_bytes({"source_name": name}))
        upstream.append(loader.source_record(path, role="pipeline-artifact"))
    problem = SimpleNamespace(case_id="fixture")
    loaded = SimpleNamespace(problem=problem, source_sha256=snapshot_record["sha256"],
        snapshot={"candidate_id": "plate:fixture"})
    normalized = tuple(PhysicalBar("bar", d, "A500", 10, 100, (0, 3900), ("zone/0/0",))
        for d in PLATE_DIRECTIONS)
    before_fe = (replace(normalized[0], transverse_axis_mm=200), *normalized[1:])
    bars = (replace(before_fe[0], installed_interval_mm=(100, 4000)), *before_fe[1:])
    changes = [{"direction": str(bars[0].direction), "bar_id": "bar",
        "before_interval_mm": [0, 3900], "after_interval_mm": [100, 4000]}]
    expected = {"physical_bar_count": 4, "source_zone_count": 4,
        "position_count": 1, "additional_mass_kg": 9.6}
    opening_checks = {"physical_bar_count": 4, "host_blocked_before": 3,
        "host_blocked_after": 2, "moved_bar_count": 1, "placement_eligible": False}
    fe_checks = {**dict.fromkeys(FLAGS, False), "physical_bar_count": 4,
        "changed_bar_count": 1, "changes": changes, "host_blocked_before": 2,
        "host_blocked_after": 1, "same_direction_body_pairs_after": 0,
        "source_coverage": {"status": "pass", "directions": [
            {"direction": str(d), "uncovered_cell_count": 0} for d in PLATE_DIRECTIONS]},
        "stock_cutting": {"status": "pass", "witness": "fresh-valid-witness"}}
    report = {**dict.fromkeys(FLAGS, False), "schema_version": "shifted-physical-repair-experiment/v1",
        "units": "mm", "case_id": problem.case_id, "candidate_id": loaded.snapshot["candidate_id"],
        "source_to_revit_xy_mm": [0, 0], "source_host_report_sha256": host_record["sha256"],
        "source_files": deepcopy([*upstream, snapshot_record, host_record]),
        "stages": {"restored_shifted_normalized": {"expected": deepcopy(expected)},
            "small_openings": {"configuration": {"maximum_shift_mm": 300},
                "checks": deepcopy(opening_checks)}},
        "raw_bars_by_direction": _raw(bars), "checks": deepcopy(fe_checks)}
    report["checks"]["stock_cutting"]["witness"] = "archived-different-valid-witness"
    report_path = tmp_path / "fe-report.json"
    source = {"case_id": problem.case_id, "source": "fresh shifted parametric zones"}
    state = SimpleNamespace(kwargs={"shifted_dir": shifted, "snapshot": snapshot,
        "candidate_id": loaded.snapshot["candidate_id"], "working_host_report": host_path,
        "fe_report": report_path, "confirm_identity_xy": True, "stock_time_limit_s": 17},
        loaded=loaded, source=source, upstream=upstream, snapshot_record=snapshot_record,
        host_record=host_record, host=object(), lanes=(object(),), expected=expected,
        normalized=normalized, before_fe=before_fe, bars=bars, report=report,
        opening_checks=opening_checks, fe_checks=fe_checks, calls=[],
        on_restore=None, on_opening=None, on_fe=None)

    def save():
        report_path.write_bytes(_bytes(report))

    state.save = save
    save()

    def load(path, *, candidate_id):
        assert path == snapshot and candidate_id == state.kwargs["candidate_id"]
        state.calls.append("load-snapshot")
        return loaded

    def restore(args, actual_loaded, actual_host_record):
        assert actual_loaded is loaded and actual_host_record == host_record
        assert args.shifted_dir == shifted and args.stock_time_limit_s == state.kwargs["stock_time_limit_s"]
        state.calls.append("restore-shifted-chain")
        if state.on_restore:
            state.on_restore()
        return source, normalized, SimpleNamespace(packet={"expected": deepcopy(expected)}), upstream

    def inspect_host(parsed):
        assert parsed == {"host": "fixture"}
        state.calls.append("inspect-host")
        return state.host, {}

    def lanes(actual_source, actual_problem):
        assert actual_source is source and actual_problem is problem
        state.calls.append("restore-source-lanes")
        return state.lanes

    def opening(original, previous, actual_lanes, actual_problem, actual_host, *, maximum_shift_mm):
        assert original == normalized and previous == state.before_fe
        assert actual_lanes is state.lanes and actual_problem is problem and actual_host is state.host
        assert maximum_shift_mm == report["stages"]["small_openings"]["configuration"]["maximum_shift_mm"]
        state.calls.append("fresh-opening-proof")
        if state.on_opening:
            state.on_opening()
        return deepcopy(state.opening_checks)

    def fe(previous, actual_bars, actual_lanes, actual_problem, actual_host, *, stock_time_limit_s):
        assert previous == state.before_fe and actual_bars == state.bars
        assert actual_lanes is state.lanes and actual_problem is problem and actual_host is state.host
        assert stock_time_limit_s == state.kwargs["stock_time_limit_s"]
        state.calls.append("fresh-FE-and-stock-proof")
        if state.on_fe:
            state.on_fe()
        return deepcopy(state.fe_checks)

    monkeypatch.setattr(loader, "load_layout_snapshot", load)
    monkeypatch.setattr(loader, "restore_shifted_inputs", restore)
    monkeypatch.setattr(loader, "load_working_host_json", lambda content, **kwargs: json.loads(content))
    monkeypatch.setattr(loader, "inspect_working_solid", inspect_host)
    monkeypatch.setattr(loader, "source_service_lanes", lanes)
    monkeypatch.setattr(loader, "check_relocation", opening)
    monkeypatch.setattr(loader, "check_fe_host_repair", fe)
    return state


def test_read_only_restoration_reverses_FE_changes_and_rechecks_both_stages(loader, inputs):
    files_before = {p: p.read_bytes() for p in inputs.kwargs["shifted_dir"].parent.rglob("*") if p.is_file()}
    result = loader.load_fe_research_inputs(**inputs.kwargs)
    assert inputs.calls == ["load-snapshot", "restore-shifted-chain", "inspect-host",
        "restore-source-lanes", "fresh-opening-proof", "fresh-FE-and-stock-proof"]
    assert result.problem is inputs.loaded.problem and result.loaded is inputs.loaded
    assert result.lanes is inputs.lanes and result.host is inputs.host and result.bars == inputs.bars
    assert result.source_report is inputs.source and result.input_report == inputs.report
    assert result.input_sha256 == hashlib.sha256(inputs.kwargs["fe_report"].read_bytes()).hexdigest()
    assert isinstance(result.source_files, tuple)
    assert {row["role"] for row in result.source_files} >= {
        "pipeline-artifact", "source-snapshot", "working-host-snapshot", "research-input-loader-code"}
    assert result.checks["placement_eligible"] is False
    assert result.checks["archived_stock_witness_reused"] is False
    assert result.checks["fe_host_repair"]["stock_cutting"]["witness"] == "fresh-valid-witness"
    assert {p: p.read_bytes() for p in files_before} == files_before
    assert set(p for p in inputs.kwargs["shifted_dir"].parent.rglob("*") if p.is_file()) == set(files_before)
    with pytest.raises(FrozenInstanceError):
        result.bars = ()


@pytest.mark.parametrize(("field", "value"), (
    ("confirm_identity_xy", False), ("confirm_identity_xy", 1), ("confirm_identity_xy", "true"),
    ("stock_time_limit_s", True), ("stock_time_limit_s", None), ("stock_time_limit_s", "30"),
    ("stock_time_limit_s", 0), ("stock_time_limit_s", -1), ("stock_time_limit_s", 0.0009),
    ("stock_time_limit_s", 60.001), ("stock_time_limit_s", float("nan")),
    ("stock_time_limit_s", float("inf")),
))
def test_invalid_limits_or_implicit_coordinate_binding_fail_before_io(loader, inputs, field, value):
    inputs.kwargs[field] = value
    with pytest.raises(ValueError):
        loader.load_fe_research_inputs(**inputs.kwargs)
    assert inputs.calls == []


@pytest.mark.parametrize("budget", (0.001, 60))
def test_inclusive_stock_budget_bounds(loader, inputs, budget):
    inputs.kwargs["stock_time_limit_s"] = budget
    assert loader.load_fe_research_inputs(**inputs.kwargs).bars == inputs.bars


@pytest.mark.parametrize("scope", ("report", "checks"))
@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize("value", (True, 0, None))
def test_every_research_and_unchanged_demand_flag_requires_literal_false(loader, inputs, scope, flag, value):
    target = inputs.report if scope == "report" else inputs.report["checks"]
    target[flag] = value
    inputs.save()
    with pytest.raises(ValueError, match="Literal false"):
        loader.load_fe_research_inputs(**inputs.kwargs)
    assert "fresh-opening-proof" not in inputs.calls


@pytest.mark.parametrize(("field", "value"), (
    ("schema_version", "fe-host-repair-experiment/v1"), ("units", "m"),
    ("case_id", "other"), ("candidate_id", "plate:other"),
    ("source_to_revit_xy_mm", [0, 1]), ("source_to_revit_xy_mm", [False, False]),
    ("source_host_report_sha256", "0" * 64),
))
def test_report_must_bind_exact_schema_case_candidate_host_and_coordinate_identity(loader, inputs, field, value):
    inputs.report[field] = value
    inputs.save()
    with pytest.raises(ValueError):
        loader.load_fe_research_inputs(**inputs.kwargs)
    assert "fresh-opening-proof" not in inputs.calls


@pytest.mark.parametrize("change", ("missing-file", "wrong-role", "wrong-sha", "empty", "too-many"))
def test_all_exact_upstream_records_and_roles_are_required(loader, inputs, change):
    rows = inputs.report["source_files"]
    if change == "missing-file":
        rows.pop(0)
    elif change == "wrong-role":
        rows[0]["role"] = "untrusted-other-role"
    elif change == "wrong-sha":
        rows[0]["sha256"] = "0" * 64
    elif change == "empty":
        rows.clear()
    else:
        rows[:] = [rows[0]] * 201
    inputs.save()
    with pytest.raises(ValueError):
        loader.load_fe_research_inputs(**inputs.kwargs)
    assert "fresh-opening-proof" not in inputs.calls


def test_binding_requires_snapshot_even_when_caller_does_not_list_it_as_required(loader, inputs):
    inputs.report["source_files"] = [r for r in inputs.report["source_files"] if r["role"] != "source-snapshot"]
    with pytest.raises(ValueError, match="snapshot SHA missing"):
        loader.validate_report_binding(inputs.report, loaded=inputs.loaded,
            candidate_id=inputs.kwargs["candidate_id"], host_record=inputs.host_record,
            required_records=inputs.upstream)


def test_restored_inventory_must_match_fresh_source_proof(loader, inputs):
    inputs.report["stages"]["restored_shifted_normalized"]["expected"]["physical_bar_count"] = 3
    inputs.save()
    with pytest.raises(ValueError, match="normalized inventory"):
        loader.load_fe_research_inputs(**inputs.kwargs)
    assert "fresh-opening-proof" not in inputs.calls


@pytest.mark.parametrize("maximum", (True, -1, 300.001, "300", None))
def test_prior_opening_shift_must_be_bounded_and_explicit(loader, inputs, maximum):
    inputs.report["stages"]["small_openings"]["configuration"]["maximum_shift_mm"] = maximum
    inputs.save()
    with pytest.raises(ValueError, match="prior opening shift"):
        loader.load_fe_research_inputs(**inputs.kwargs)
    assert "fresh-opening-proof" not in inputs.calls


def test_recorded_opening_checks_are_not_trusted(loader, inputs):
    inputs.report["stages"]["small_openings"]["checks"]["host_blocked_after"] = 0
    inputs.save()
    with pytest.raises(ValueError, match="opening stage does not reproduce"):
        loader.load_fe_research_inputs(**inputs.kwargs)
    assert "fresh-FE-and-stock-proof" not in inputs.calls


@pytest.mark.parametrize("change", ("host", "coverage", "collision", "count", "missing-field"))
def test_full_FE_proof_is_compared_not_only_summary_status(loader, inputs, change):
    checks = inputs.report["checks"]
    if change == "host":
        checks["host_blocked_after"] = 0
    elif change == "coverage":
        checks["source_coverage"]["directions"].pop()
    elif change == "collision":
        checks["same_direction_body_pairs_after"] = 2
    elif change == "count":
        checks["physical_bar_count"] = 3
    else:
        checks.pop("source_coverage")
    inputs.save()
    with pytest.raises(ValueError, match="Full current FE"):
        loader.load_fe_research_inputs(**inputs.kwargs)


@pytest.mark.parametrize("scope", ("fresh", "archived"))
def test_both_stock_checks_must_pass_but_witnesses_need_not_match(loader, inputs, scope):
    checks = inputs.fe_checks if scope == "fresh" else inputs.report["checks"]
    checks["stock_cutting"]["status"] = "incomplete"
    inputs.save()
    with pytest.raises(ValueError, match="Full current FE"):
        loader.load_fe_research_inputs(**inputs.kwargs)


def test_snapshot_race_detected_before_restoration(loader, inputs):
    inputs.kwargs["snapshot"].write_bytes(b"changed original snapshot")
    with pytest.raises(ValueError, match="Snapshot changed"):
        loader.load_fe_research_inputs(**inputs.kwargs)
    assert inputs.calls == ["load-snapshot"]


def test_host_read_budget_is_enforced_before_geometry(loader, inputs, monkeypatch):
    monkeypatch.setattr(loader, "MAX_WORKING_REPORT_BYTES", 1)
    with pytest.raises(ValueError, match="Host exceeds budget"):
        loader.load_fe_research_inputs(**inputs.kwargs)
    assert "inspect-host" not in inputs.calls


def test_host_is_rehashed_after_binding_before_geometry(loader, inputs, monkeypatch):
    real_validate = loader.validate_report_binding

    def mutate_after_binding(*args, **kwargs):
        real_validate(*args, **kwargs)
        inputs.kwargs["working_host_report"].write_bytes(_bytes({"host": "different"}))

    monkeypatch.setattr(loader, "validate_report_binding", mutate_after_binding)
    with pytest.raises(ValueError, match="Host exceeds budget or changed"):
        loader.load_fe_research_inputs(**inputs.kwargs)
    assert "inspect-host" not in inputs.calls


@pytest.mark.parametrize("stage", ("restore_shifted_inputs", "check_relocation", "check_fe_host_repair"))
def test_proof_failure_is_propagated_without_partial_result(loader, inputs, monkeypatch, stage):
    def fail(*args, **kwargs):
        raise ValueError("independent checker rejected fixture")

    monkeypatch.setattr(loader, stage, fail)
    with pytest.raises(ValueError, match="independent checker rejected"):
        loader.load_fe_research_inputs(**inputs.kwargs)


@pytest.mark.parametrize("target", ("upstream", "snapshot", "host", "fe-report"))
def test_files_reverified_at_tail_cannot_change_during_expensive_proof(loader, inputs, target):
    paths = {"upstream": Path(inputs.upstream[0]["path"]), "snapshot": inputs.kwargs["snapshot"],
        "host": inputs.kwargs["working_host_report"], "fe-report": inputs.kwargs["fe_report"]}
    inputs.on_fe = lambda: paths[target].write_bytes(b"changed during proof")
    with pytest.raises(ValueError, match="Source changed during calculation"):
        loader.load_fe_research_inputs(**inputs.kwargs)
    assert inputs.calls[-1] == "fresh-FE-and-stock-proof"


@pytest.mark.parametrize("content", (b"[]", b"{\"x\":1,\"x\":2}", b"{\"x\":NaN}",
    b"{\"x\":1e999}", b""))
def test_report_must_be_finite_unique_canonical_object(loader, inputs, content):
    inputs.kwargs["fe_report"].write_bytes(content)
    with pytest.raises(ValueError):
        loader.load_fe_research_inputs(**inputs.kwargs)
    assert "inspect-host" not in inputs.calls


def test_canonical_object_without_required_report_schema_fails_closed(loader, inputs):
    inputs.kwargs["fe_report"].write_bytes(_bytes({}))
    # A malformed object has no public exception-class promise; either strict
    # schema error must propagate, never become a partial accepted input.
    with pytest.raises((ValueError, KeyError)):
        loader.load_fe_research_inputs(**inputs.kwargs)
    assert "inspect-host" not in inputs.calls


def test_complete_four_direction_output_is_required(loader, inputs):
    inputs.report["raw_bars_by_direction"].pop(str(PLATE_DIRECTIONS[-1]))
    inputs.save()
    with pytest.raises(ValueError, match="four complete physical directions"):
        loader.load_fe_research_inputs(**inputs.kwargs)


def test_reverse_preserves_order_directional_identity_axis_owners_and_inventory(loader, inputs):
    current = inputs.bars
    result = loader.reverse_recorded_fe_changes(current, inputs.report["checks"]["changes"])
    assert result == inputs.before_fe
    assert current == inputs.bars and current[0].installed_interval_mm == (100, 4000)
    assert result[0].transverse_axis_mm == 200
    assert [(b.direction, b.id, b.diameter_mm, b.source_bar_ids) for b in result] == [
        (b.direction, b.id, b.diameter_mm, b.source_bar_ids) for b in current]
    assert loader.reverse_recorded_fe_changes(current, []) == current


@pytest.mark.parametrize("change", ("duplicate", "unknown-id", "wrong-direction", "wrong-after",
    "bool-after", "same-before", "reversed-before", "too-long-before", "bool-before", "nan-before"))
def test_reverse_rejects_duplicate_unknown_or_nonphysical_change_records(loader, inputs, change):
    rows = deepcopy(inputs.report["checks"]["changes"])
    row = rows[0]
    if change == "duplicate":
        rows.append(deepcopy(row))
    elif change == "unknown-id":
        row["bar_id"] = "not-in-current-output"
    elif change == "wrong-direction":
        row["direction"] = "sideways"
    elif change == "wrong-after":
        row["after_interval_mm"][0] += 1
    elif change == "bool-after":
        row["after_interval_mm"][0] = False
    elif change == "same-before":
        row["before_interval_mm"] = row["after_interval_mm"][:]
    elif change == "reversed-before":
        row["before_interval_mm"] = [3900, 0]
    elif change == "too-long-before":
        row["before_interval_mm"] = [0, 11701]
    elif change == "bool-before":
        row["before_interval_mm"] = [False, 3900]
    else:
        row["before_interval_mm"] = [float("nan"), 3900]
    with pytest.raises(ValueError):
        loader.reverse_recorded_fe_changes(inputs.bars, rows)


@pytest.mark.parametrize("changes", (None, (), {}, "changes", [None] * 5))
def test_reverse_requires_bounded_list(loader, inputs, changes):
    with pytest.raises(ValueError, match="Bounded complete FE changes list"):
        loader.reverse_recorded_fe_changes(inputs.bars, changes)


def test_reverse_does_not_silently_coalesce_duplicate_current_ids(loader, inputs):
    with pytest.raises(ValueError, match="unique physical identities"):
        loader.reverse_recorded_fe_changes((inputs.bars[0], inputs.bars[0]), [])
