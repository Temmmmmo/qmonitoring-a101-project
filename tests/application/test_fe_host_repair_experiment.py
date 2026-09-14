"""CLI provenance and write guards; geometric certification has its own tests.

The small fixture replaces expensive source restoration/search, not JSON parsing,
file hashes, source-change detection, permission flags or exclusive output writes.
It also records that independent pre/post-checks surround the joint search.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebar.application.physical_layout_recovery import _bytes
from rebar.optimization.contracts.physical import PhysicalBar
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS


@pytest.fixture
def cli(monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "fe_host_repair_experiment_test", scripts / "experiment_fe_host_repair.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def raw_bars():
    return {str(direction): [{"id": "bar", "steel_class": "A500", "diameter_mm": 10,
        "coordinate_mm": 100, "longitudinal_mm": [0, 3900],
        "source_bar_ids": ["zone/0/0"]}] for direction in PLATE_DIRECTIONS}


@pytest.fixture
def run_case(cli, tmp_path, monkeypatch):
    args = SimpleNamespace(pipeline_dir=tmp_path / "pipeline", snapshot=tmp_path / "snapshot.json",
        candidate_id="plate:fixture", working_host_report=tmp_path / "host.json",
        relocation_draft=tmp_path / "draft.json", relocation_review=tmp_path / "review.json",
        reassign_lengths=False, time_limit_s=3, maximum_candidates_per_bar=16,
        maximum_total_candidates=128, output=tmp_path / "new-output" / "experiment.json")
    args.snapshot.write_bytes(b"unchanged source snapshot")
    args.working_host_report.write_bytes(_bytes({"host": "fixture"}))
    dxf = tmp_path / "original.dxf"
    dxf.write_bytes(b"unchanged original DXF")
    source_records = [cli.source_record(dxf, role="dxf"),
        cli.source_record(args.snapshot, role="source-snapshot")]
    recovery = SimpleNamespace(packet_bytes=b"unchanged legacy packet",
        patterned_report={"fixture": "fresh source geometry"},
        normalization_report={"accepted": {"raw_bars_by_direction": raw_bars()}})
    draft = {"schema_version": "physical-bar-relocation-draft/v1", "placement_eligible": False,
        "structural_placement_supported": False, "source_to_revit_xy_mm": [0, 0],
        "source_packet_sha256": hashlib.sha256(recovery.packet_bytes).hexdigest(),
        "source_host_report_sha256": hashlib.sha256(args.working_host_report.read_bytes()).hexdigest(),
        "raw_bars_by_direction": raw_bars()}

    def write_draft():
        content = _bytes(draft)
        args.relocation_draft.write_bytes(content)
        args.relocation_review.write_bytes(_bytes({"draft_sha256": hashlib.sha256(content).hexdigest()}))

    write_draft()
    loaded = SimpleNamespace(problem=SimpleNamespace(case_id="fixture"),
        snapshot={"candidate_id": "plate:fixture"}, source_sha256=source_records[-1]["sha256"])
    lanes, host, obligations = object(), object(), {"frozen": "before search"}
    calls = []
    checks = {"status": "research_checks_passed_not_placement_approved", "host_blocked_before": 1,
        "host_blocked_after": 0, "physical_bar_count": 4, "additional_mass_kg": 9.61,
        "changed_bar_count": 4, "same_direction_body_pairs_after": 0, "new_body_pairs": 0,
        "source_coverage": {"status": "pass", "uncovered_cell_count": 0},
        "stock_cutting": {"status": "pass"}, "placement_eligible": False}
    telemetry = {"status": "mock finite search", "complete_layout_retained": True}

    def load(path, *, candidate_id):
        calls.append(("load", path, candidate_id))
        return loaded

    def restore(arguments, source):
        assert arguments is args and source is loaded
        calls.append(("restore",))
        return recovery, {}, list(source_records)

    def source_lanes(patterned, problem):
        assert patterned is recovery.patterned_report and problem is loaded.problem
        calls.append(("lanes",))
        return lanes

    def precheck(before, after, actual_lanes, problem, actual_host):
        assert before == after == cli.decode_bars(raw_bars())
        assert actual_lanes is lanes and problem is loaded.problem and actual_host is host
        calls.append(("precheck", before, after))

    def derive(before, actual_lanes, problem):
        assert actual_lanes is lanes and problem is loaded.problem
        calls.append(("derive", before))
        return obligations

    def solve(before, frozen, actual_host, **kwargs):
        assert frozen is obligations and actual_host is host
        calls.append(("solve", before, kwargs))
        return tuple(replace(bar, installed_interval_mm=(25, 3925)) for bar in before), telemetry

    def postcheck(before, after, actual_lanes, problem, actual_host):
        assert before != after
        assert actual_lanes is lanes and problem is loaded.problem and actual_host is host
        calls.append(("postcheck", before, after))
        return deepcopy(checks)

    monkeypatch.setattr(cli, "_code_digest", lambda: "a" * 64)
    monkeypatch.setattr(cli, "load_layout_snapshot", load)
    monkeypatch.setattr(cli, "_load_recovery", restore)
    monkeypatch.setattr(cli, "load_working_host_json", lambda content, **kw: json.loads(content))
    monkeypatch.setattr(cli, "inspect_working_solid", lambda report: (host, {}))
    monkeypatch.setattr(cli, "source_service_lanes", source_lanes)
    monkeypatch.setattr(cli, "check_relocation", precheck)
    monkeypatch.setattr(cli, "derive_fe_obligations", derive)
    monkeypatch.setattr(cli, "solve_fe_host_repair", solve)
    monkeypatch.setattr(cli, "check_fe_host_repair", postcheck)
    return SimpleNamespace(args=args, draft=draft, write_draft=write_draft, dxf=dxf,
        recovery=recovery, loaded=loaded, calls=calls, source_records=source_records,
        telemetry=telemetry, checks=checks)


def test_decode_preserves_all_four_typed_directions_and_every_source_owner(cli):
    raw = raw_bars()
    raw[str(PLATE_DIRECTIONS[0])][0]["source_bar_ids"].append("second-zone/0/0")
    original = deepcopy(raw)
    bars = cli.decode_bars(dict(reversed(tuple(raw.items()))))
    assert tuple(bar.direction for bar in bars) == PLATE_DIRECTIONS
    assert all(isinstance(bar, PhysicalBar) for bar in bars)
    assert all(bar.installed_interval_mm == (0, 3900) for bar in bars)
    assert bars[0].source_bar_ids == ("zone/0/0", "second-zone/0/0")
    assert raw == original


@pytest.mark.parametrize("change", ("missing", "unknown", "case_changed", "typed_keys"))
def test_decode_rejects_missing_or_unknown_direction_keys(cli, change):
    raw = raw_bars()
    key = str(PLATE_DIRECTIONS[0])
    if change == "missing":
        raw.pop(key)
    elif change == "unknown":
        raw["unknown"] = []
    elif change == "case_changed":
        raw[key.upper()] = raw.pop(key)
    else:
        raw = {direction: raw[str(direction)] for direction in PLATE_DIRECTIONS}
    with pytest.raises(ValueError, match="four complete physical directions"):
        cli.decode_bars(raw)


def test_run_writes_only_new_explicitly_unapproved_research_with_exact_provenance(cli, run_case):
    case = run_case
    original_files = {p: p.read_bytes() for p in (case.args.snapshot, case.dxf,
        case.args.working_host_report, case.args.relocation_draft, case.args.relocation_review)}
    report = cli.run(case.args)
    assert case.args.output.read_bytes() == _bytes(report)
    assert report["schema_version"] == "fe-host-repair-experiment/v1"
    for key in ("placement_eligible", "structural_placement_supported", "engineering_approval",
                "source_demand_removed", "source_demand_values_changed"):
        assert report[key] is False
    assert report["source_to_revit_xy_mm"] == [0, 0]
    assert report["candidate_id"] == "plate:fixture" and report["case_id"] == "fixture"
    assert report["code_sha256"] == "a" * 64
    assert report["checks"] == case.checks and report["search"] == case.telemetry
    assert "Not a Revit packet" in report["warning"]
    assert set(report["raw_bars_by_direction"]) == set(map(str, PLATE_DIRECTIONS))
    assert all(rows[0]["longitudinal_mm"] == [25, 3925]
               for rows in report["raw_bars_by_direction"].values())
    assert [call[0] for call in case.calls] == ["load", "restore", "lanes", "precheck", "derive", "solve", "postcheck"]
    assert case.calls[0] == ("load", case.args.snapshot, case.args.candidate_id)
    assert case.calls[4][1] == case.calls[5][1] == case.calls[6][1]
    for record in report["source_files"]:
        assert record == cli.source_record(record["path"], role=record["role"])
    assert set(original_files) <= {Path(record["path"]) for record in report["source_files"]}
    assert any(row["role"] == "experiment-code" for row in report["source_files"])
    assert {p: p.read_bytes() for p in original_files} == original_files


@pytest.mark.parametrize("exchange", (False, True))
def test_run_forwards_explicit_search_bounds_and_inventory_exchange_switch(cli, run_case, exchange):
    run_case.args.reassign_lengths = exchange
    report = cli.run(run_case.args)
    solve = next(call for call in run_case.calls if call[0] == "solve")
    assert solve[2] == {"reassign_lengths": exchange, "time_limit_s": 3,
        "maximum_candidates_per_bar": 16, "maximum_total_candidates": 128}
    assert report["reassign_existing_lengths"] is exchange


@pytest.mark.parametrize("directory", (False, True))
def test_existing_output_is_never_overwritten_or_used_as_a_directory(cli, run_case, directory):
    target = run_case.args.output
    target.parent.mkdir()
    if directory:
        target.mkdir()
        sentinel = target / "keep.txt"
    else:
        sentinel = target
    sentinel.write_bytes(b"user output")
    with pytest.raises(ValueError, match="must not be overwritten"):
        cli.run(run_case.args)
    assert sentinel.read_bytes() == b"user output"
    assert run_case.calls == []


@pytest.mark.parametrize(("key", "value"), (
    ("schema_version", "physical-bar-plan-trial/v1"),
    ("placement_eligible", True), ("placement_eligible", 0),
    ("structural_placement_supported", True), ("structural_placement_supported", 0),
    ("source_to_revit_xy_mm", [1, 0]), ("source_to_revit_xy_mm", [0, -1]),
    ("source_packet_sha256", "0" * 64), ("source_host_report_sha256", "0" * 64),
))
def test_draft_schema_permissions_identity_xy_and_exact_sha_are_required(cli, run_case, key, value):
    run_case.draft[key] = value
    run_case.write_draft()
    with pytest.raises(ValueError, match="Exact linked research draft"):
        cli.run(run_case.args)
    assert not run_case.args.output.parent.exists()
    assert [call[0] for call in run_case.calls] == ["load", "restore"]


def test_review_must_bind_exact_draft_bytes(cli, run_case):
    run_case.args.relocation_review.write_bytes(_bytes({"draft_sha256": "0" * 64}))
    with pytest.raises(ValueError, match="Exact linked research draft"):
        cli.run(run_case.args)
    assert not run_case.args.output.parent.exists()


@pytest.mark.parametrize("content", (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}',
                                     b'[]', b'', b'{"x": 1}'))
def test_run_uses_strict_canonical_bounded_json_reader(cli, run_case, content):
    run_case.args.relocation_draft.write_bytes(content)
    with pytest.raises(ValueError):
        cli.run(run_case.args)
    assert not run_case.args.output.parent.exists()
    assert all(call[0] != "solve" for call in run_case.calls)


def test_host_is_rehashed_between_source_record_and_actual_read(cli, run_case, monkeypatch):
    original = cli.source_record

    def mutate_after_hash(path, *, role):
        record = original(path, role=role)
        if role == "working-host-snapshot":
            run_case.args.working_host_report.write_bytes(b"changed host")
        return record

    monkeypatch.setattr(cli, "source_record", mutate_after_hash)
    with pytest.raises(ValueError, match="Host snapshot exceeds budget or changed"):
        cli.run(run_case.args)
    assert not run_case.args.output.parent.exists()


def test_host_read_is_bounded_before_geometry_loader(cli, run_case, monkeypatch):
    monkeypatch.setattr(cli, "MAX_WORKING_REPORT_BYTES", 1)
    with pytest.raises(ValueError, match="Host snapshot exceeds budget or changed"):
        cli.run(run_case.args)
    assert not run_case.args.output.parent.exists()


@pytest.mark.parametrize("source", ("dxf", "snapshot", "working_host_report", "relocation_draft", "relocation_review"))
def test_any_recorded_source_change_during_search_prevents_output(cli, run_case, monkeypatch, source):
    original = cli.solve_fe_host_repair
    path = run_case.dxf if source == "dxf" else getattr(run_case.args, source)

    def mutate_after_search(*args, **kwargs):
        result = original(*args, **kwargs)
        path.write_bytes(b"changed after search")
        return result

    monkeypatch.setattr(cli, "solve_fe_host_repair", mutate_after_search)
    with pytest.raises(ValueError, match="Source changed during calculation"):
        cli.run(run_case.args)
    assert not run_case.args.output.parent.exists()


def test_snapshot_record_is_added_even_when_upstream_pipeline_does_not_record_it(cli, run_case):
    run_case.source_records[:] = [row for row in run_case.source_records if row["role"] != "source-snapshot"]
    report = cli.run(run_case.args)
    snapshots = [row for row in report["source_files"] if row["role"] == "source-snapshot"]
    assert snapshots == [cli.source_record(run_case.args.snapshot, role="source-snapshot")]


@pytest.mark.parametrize("stage", ("after_load", "during_search"))
def test_unrecorded_upstream_snapshot_cannot_change_after_fresh_restoration(cli, run_case, monkeypatch, stage):
    run_case.source_records[:] = [row for row in run_case.source_records if row["role"] != "source-snapshot"]
    function = "load_layout_snapshot" if stage == "after_load" else "solve_fe_host_repair"
    original = getattr(cli, function)

    def mutate_after_call(*args, **kwargs):
        result = original(*args, **kwargs)
        run_case.args.snapshot.write_bytes(b"changed source snapshot")
        return result

    monkeypatch.setattr(cli, function, mutate_after_call)
    with pytest.raises(ValueError, match="Snapshot changed|Source changed"):
        cli.run(run_case.args)
    assert not run_case.args.output.parent.exists()


def test_core_code_change_during_search_prevents_output(cli, run_case, monkeypatch):
    digests = iter(("a" * 64, "b" * 64))
    monkeypatch.setattr(cli, "_code_digest", lambda: next(digests))
    with pytest.raises(ValueError, match="Core code changed"):
        cli.run(run_case.args)
    assert not run_case.args.output.parent.exists()


@pytest.mark.parametrize("checker", ("check_relocation", "derive_fe_obligations", "check_fe_host_repair"))
def test_independent_checker_failure_is_not_converted_to_a_successful_report(cli, run_case, monkeypatch, checker):
    def reject(*args, **kwargs):
        raise ValueError("independent validation failed")

    monkeypatch.setattr(cli, checker, reject)
    with pytest.raises(ValueError, match="independent validation failed"):
        cli.run(run_case.args)
    assert not run_case.args.output.parent.exists()
    if checker != "check_fe_host_repair":
        assert all(call[0] != "solve" for call in run_case.calls)


def test_output_created_after_initial_check_is_still_never_overwritten(cli, run_case, monkeypatch):
    original = cli.verify_source_records

    def another_writer(records):
        original(records)
        run_case.args.output.parent.mkdir()
        run_case.args.output.write_bytes(b"another writer owns this")

    monkeypatch.setattr(cli, "verify_source_records", another_writer)
    with pytest.raises(FileExistsError):
        cli.run(run_case.args)
    assert run_case.args.output.read_bytes() == b"another writer owns this"


def test_main_reports_guard_error_as_nonzero_cli_exit(cli, tmp_path, monkeypatch, capsys):
    def reject(args):
        assert args.reassign_lengths is True
        raise ValueError("explicit guard failure")

    monkeypatch.setattr(cli, "run", reject)
    with pytest.raises(SystemExit) as caught:
        cli.main(["--pipeline-dir", str(tmp_path), "--snapshot", str(tmp_path / "source.json"),
            "--candidate-id", "plate:1", "--working-host-report", str(tmp_path / "host.json"),
            "--relocation-draft", str(tmp_path / "draft.json"),
            "--relocation-review", str(tmp_path / "review.json"), "--reassign-lengths",
            "--output", str(tmp_path / "new.json")])
    assert caught.value.code == 2
    assert "explicit guard failure" in capsys.readouterr().err
