"""CLI provenance/permission guards and bounded solver integration smoke tests."""
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebar.application.assistant_inputs import source_record
from rebar.application.physical_layout_recovery import _bytes, _raw


@pytest.fixture
def cli(monkeypatch):
    scripts = Path(__file__).resolve().parents[2]/"scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("collision_replacement_cli_test", scripts/"experiment_collision_replacement.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _case():
    path = Path(__file__).resolve().parents[1]/"optimization/test_collision_replacement.py"
    spec = importlib.util.spec_from_file_location("collision_test_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.case()


@pytest.fixture
def setup(cli, tmp_path, monkeypatch):
    import research_fe_inputs
    bars, lanes, problem, host, op = _case()
    source = tmp_path/"source.json"
    source.write_bytes(b"original source")
    record = source_record(source, role="source-snapshot")
    args = SimpleNamespace(shifted_dir=tmp_path, snapshot=source, candidate_id="plate:synthetic",
        working_host_report=tmp_path/"host.json", combined_repair=tmp_path/"fe.json", output=tmp_path/"new"/"out.json",
        confirm_identity_xy=True, stock_time_limit_s=30, maximum_mass_kg=1000,
        maximum_bar_count=20, maximum_position_count=20, maximum_operations=64, maximum_residue_states=11700,
        transverse_report=None)
    restored = SimpleNamespace(bars=bars, lanes=lanes, problem=problem, host=host,
        source_files=(record,), input_sha256="a"*64, loaded=SimpleNamespace(snapshot={"candidate_id": args.candidate_id}))
    calls = []

    def loader(**kwargs):
        calls.append(kwargs)
        return restored

    monkeypatch.setattr(research_fe_inputs, "load_fe_research_inputs", loader)
    monkeypatch.setattr(cli, "_code_digest", lambda: "f"*64)
    # Keep independent checking real; isolate only finite search choice.
    monkeypatch.setattr(cli, "propose_operations", lambda *a, **kw: ((op,), {}, []))
    monkeypatch.setattr(cli, "balance_new_descendant_lengths", lambda before, ops, *a, **kw: (ops, []))
    return SimpleNamespace(args=args, restored=restored, source=source, calls=calls, op=op)


def test_fresh_shared_loader_and_independent_full_checks_precede_new_false_schema(cli, setup):
    value = cli.run(setup.args)
    assert setup.calls == [{"shifted_dir": setup.args.shifted_dir, "snapshot": setup.args.snapshot,
        "candidate_id": setup.args.candidate_id, "working_host_report": setup.args.working_host_report,
        "fe_report": setup.args.combined_repair, "confirm_identity_xy": True, "stock_time_limit_s": 30}]
    assert value["schema_version"] == "collision-replacement-experiment/v1"
    assert value["source_report_sha256"] == setup.restored.input_sha256
    assert not value["placement_eligible"] and not value["engineering_approval"]
    assert not value["legacy_source_certificate_reused"]
    assert value["checks"]["source_coverage"]["status"] == "pass"
    assert value["checks"]["stock_cutting"]["status"] == "pass"
    assert value["checks"]["physical_bar_count"] == 3
    for field in ("source_material_mismatch", "straight_40d_obstruction"):
        assert value[field]["placement_eligible"] is False
        assert value[field]["source_demand_removed"] is False
        assert value[field]["source_demand_values_changed"] is False
    assert value["straight_40d_obstruction"]["full40d_is_normatively_required"] == "not_asserted"
    assert sum(map(len, value["raw_bars_by_direction"].values())) == 3
    assert json.loads(setup.args.output.read_bytes())["checks"]["new_body_pairs"] == 0


def test_no_overwrite_even_before_loader(cli, setup):
    setup.args.output.parent.mkdir()
    setup.args.output.write_bytes(b"keep")
    with pytest.raises(ValueError, match="must not be overwritten"):
        cli.run(setup.args)
    assert not setup.calls and setup.args.output.read_bytes() == b"keep"


@pytest.mark.parametrize("name,value", (("confirm_identity_xy", False), ("confirm_identity_xy", 1),
    ("stock_time_limit_s", float("nan")), ("stock_time_limit_s", True), ("stock_time_limit_s", 0),
    ("maximum_mass_kg", float("inf")), ("maximum_mass_kg", False), ("maximum_bar_count", 0),
    ("maximum_bar_count", 1.5), ("maximum_position_count", True), ("maximum_operations", 65),
    ("maximum_residue_states", 11701)))
def test_cli_bounded_values_and_explicit_identity_are_fail_closed(cli, setup, name, value):
    setattr(setup.args, name, value)
    with pytest.raises(ValueError):
        cli.run(setup.args)
    assert not setup.calls and not setup.args.output.exists()


def test_source_change_during_solver_prevents_writing(cli, setup, monkeypatch):
    original = cli.check_collision_replacement

    def checking(*args, **kwargs):
        value = original(*args, **kwargs)
        setup.source.write_bytes(b"changed")
        return value

    monkeypatch.setattr(cli, "check_collision_replacement", checking)
    with pytest.raises(ValueError):
        cli.run(setup.args)
    assert not setup.args.output.exists()


def test_code_change_during_experiment_prevents_writing(cli, setup, monkeypatch):
    values = iter(("a"*64, "b"*64))
    monkeypatch.setattr(cli, "_code_digest", lambda: next(values))
    with pytest.raises(ValueError, match="Core code changed"):
        cli.run(setup.args)
    assert not setup.args.output.exists()


def test_real_bounded_generator_fuses_synthetic_pair_and_stock_lengths_are_rechecked(cli):
    bars, lanes, problem, host, _ = _case()
    operations, frozen, trace = cli.propose_operations(bars, lanes, problem, host)
    assert len(operations) == 1 and operations[0].kind == "fuse"
    balanced, telemetry = cli.balance_new_descendant_lengths(bars, operations, frozen, lanes, host)
    assert balanced[0].added_bars[0].installed_length_mm == 11700
    after, checked = cli.check_collision_replacement(bars, balanced, lanes, problem, host,
        limits=cli.CollisionReplacementLimits(1000, 20, 20))
    assert len(after) == 1 and checked["stock_cutting"]["status"] == "pass"
    assert trace[0]["status"] == "proposed" and telemetry[0]["added_length_mm"] > 0


def test_residue_limit_cannot_return_truncated_cutting_proof(cli):
    bars, lanes, problem, host, _ = _case()
    operations, frozen, _ = cli.propose_operations(bars, lanes, problem, host)
    with pytest.raises(ValueError, match="Residue state budget"):
        cli.balance_new_descendant_lengths(bars, operations, frozen, lanes, host, maximum_residue_states=1)


@pytest.fixture
def transverse(setup, tmp_path):
    value = {"schema_version": "fe-transverse-repair-experiment/v1", "units": "mm", "source_to_revit_xy_mm": [0, 0],
        "placement_eligible": False, "structural_placement_supported": False, "engineering_approval": False,
        "source_demand_removed": False, "source_demand_values_changed": False,
        "source_report_sha256": setup.restored.input_sha256, "source_files": list(setup.restored.source_files),
        "case_id": setup.restored.problem.case_id, "candidate_id": setup.args.candidate_id,
        "raw_bars_by_direction": _raw(setup.restored.bars), "configuration": {"maximum_shift_mm": 300},
        "checks": {"claimed_pass_not_trusted": True}}
    path = tmp_path/"transverse.json"
    path.write_bytes(_bytes(value))
    return path, value


def test_optional_transverse_plan_is_independently_checked_not_its_claimed_status(cli, setup, transverse):
    path, _ = transverse
    after, checks, records = cli.restore_transverse_input(path, setup.restored)
    assert after == setup.restored.bars and checks["source_coverage"]["status"] == "pass"
    assert "claimed_pass_not_trusted" not in checks and len(records) == 2


@pytest.mark.parametrize("field,value", (("source_report_sha256", "b"*64), ("case_id", "other"),
    ("candidate_id", "other"), ("placement_eligible", 0), ("engineering_approval", True),
    ("source_demand_removed", True), ("source_demand_values_changed", 0), ("units", "m"),
    ("source_to_revit_xy_mm", [False, 0]), ("source_files", []), ("schema_version", "joint-nine/v1")))
def test_optional_transverse_input_wrong_binding_or_permission_rejected(cli, setup, transverse, field, value):
    path, report = transverse
    report = deepcopy(report)
    report[field] = value
    path.write_bytes(_bytes(report))
    with pytest.raises(ValueError):
        cli.restore_transverse_input(path, setup.restored)


def test_optional_transverse_plan_cannot_change_lengths_even_with_forged_success(cli, setup, transverse):
    path, report = transverse
    changed = (replace(setup.restored.bars[0], installed_interval_mm=(600, 3525)), setup.restored.bars[1])
    report["raw_bars_by_direction"] = _raw(changed)
    path.write_bytes(_bytes(report))
    with pytest.raises(ValueError):
        cli.restore_transverse_input(path, setup.restored)


def test_main_requires_paths_and_explicit_inputs(cli):
    with pytest.raises(SystemExit) as error:
        cli.main([])
    assert error.value.code == 2
