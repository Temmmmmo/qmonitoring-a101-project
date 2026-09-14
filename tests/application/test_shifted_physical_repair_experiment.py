"""Exact shifted-source chain and two-stage CLI guards without expensive searches."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebar.application.physical_layout_recovery import _bytes
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS


@pytest.fixture
def cli(monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("shifted_physical_repair_test",
        scripts/"experiment_shifted_physical_repair.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup(cli, tmp_path, monkeypatch):
    base = tmp_path/"shifted"
    base.mkdir()
    args = SimpleNamespace(shifted_dir=base, snapshot=tmp_path/"snapshot.json",
        candidate_id="plate:fixture", working_host_report=tmp_path/"host.json", confirm_identity_xy=True,
        output=tmp_path/"new"/"result.json", opening_time_limit_s=60, fe_time_limit_s=60,
        stock_time_limit_s=30, maximum_fe_candidates_per_bar=512, maximum_total_fe_candidates=50000)
    args.snapshot.write_bytes(b"source snapshot")
    args.working_host_report.write_bytes(_bytes({"host": "fixture"}))
    originals = tmp_path/"originals"
    originals.mkdir()
    records = []
    for name, role in ((*((f"{direction}.dxf", "dxf") for direction in PLATE_DIRECTIONS),
                        ("engineer.pdf", "engineer-pdf"), ("patterned-analysis.json", "pipeline-artifact"))):
        path = originals/name
        path.write_bytes(name.encode())
        records.append(cli.source_record(path, role=role))
    snap = cli.source_record(args.snapshot, role="source-snapshot")
    host_record = cli.source_record(args.working_host_report, role="working-host-snapshot")
    provenance = {"candidate_id": args.candidate_id, "snapshot_sha256": snap["sha256"]}
    loaded = SimpleNamespace(problem=SimpleNamespace(case_id="fixture"), source_sha256=snap["sha256"],
        snapshot={"candidate_id": args.candidate_id,
            "source_dxf": [r for r in records if r["role"] == "dxf"],
            "source_pdf": next(r for r in records if r["role"] == "engineer-pdf")})
    expected = {"physical_bar_count": 4, "source_zone_count": 4, "additional_mass_kg": 9.6,
        "position_count": 1, "execution_group_count": 4, "run_count": 4}
    coverage = [{"direction": str(d), "status": "pass", "demanded_cell_count": 1,
        "uncovered_cell_count": 0} for d in PLATE_DIRECTIONS]
    raw = {str(d): [{"id": "bar", "steel_class": "A500", "diameter_mm": 10,
        "coordinate_mm": 100, "longitudinal_mm": [0, 3900], "source_bar_ids": ["zone/0/0"]}]
        for d in PLATE_DIRECTIONS}
    binding = {"source_to_revit_xy_mm": [0, 0], "source_host_report_sha256": host_record["sha256"], "blocked_after": 2}
    source = {"schema_version": "composite-plate-analysis/v1", "units": "mm", "placement_eligible": False,
        "source_demand_preserved": True, "case_id": "fixture", "source_provenance": provenance,
        "zone_translation": {"source_patterned_report_sha256": records[-1]["sha256"], "placement_eligible": False,
            "source_demand_preserved": True, "method": "asymmetric_fixed_phase_fixed_inventory_zone_shift"},
        "front": [{"physical_bar_count": 4}]}
    normal = {"schema_version": "physical-layout-normalization/v1", "units": "mm", "placement_eligible": False,
        "engineering_approval": False, "source_demand_removed": False, "source_provenance": provenance,
        "host_fit": binding, "accepted": {"raw_bars_by_direction": raw}}
    review = {"schema_version": "physical-bar-plan-review/v1", "units": "mm", "placement_eligible": False,
        "engineering_approval": False, "source_demand_preserved": True, "source_provenance": provenance,
        "host_fit": binding, "expected": deepcopy(expected), "source_original_coverage": deepcopy(coverage),
        "permanent_blockers": ["original-research", "working-host-fit-incomplete"]}
    experiment = {"schema_version": "asymmetric-zone-shift-experiment/v1", "placement_eligible": False,
        "engineering_approval": False, "source_demand_removed": False, "source_demand_values_changed": False,
        "source_to_revit_xy_mm": [0, 0], "case_id": "fixture", "source_files": [*records, snap, host_record],
        "source_batch_after": source["front"][0], "normalized": {"expected": deepcopy(expected), "host_fit": binding}}
    state = SimpleNamespace(args=args, loaded=loaded, host_record=host_record, source=source, normal=normal,
        review=review, experiment=experiment, expected=expected, coverage=coverage, calls=[], on_solve=None)

    def packet(source_sha, raw_sha):
        return {"expected": deepcopy(expected), "source_report_sha256": source_sha,
            "raw_report_sha256": raw_sha, "source_blockers": ["original-research"]}

    def save():
        source_bytes = _bytes(source)
        source_sha = hashlib.sha256(source_bytes).hexdigest()
        normal["source_report_sha256"] = source_sha
        normal_bytes = _bytes(normal)
        normal_sha = hashlib.sha256(normal_bytes).hexdigest()
        review.update(source_report_sha256=source_sha, raw_report_sha256=normal_sha)
        bound = packet(source_sha, normal_sha)
        bound["source_blockers"] = review["permanent_blockers"]
        review["packet_sha256"] = hashlib.sha256(_bytes(bound)).hexdigest()
        for name, value in zip(cli.INPUT_NAMES, (experiment, source, normal, review), strict=True):
            (base/name).write_bytes(_bytes(value))

    state.save = save
    save()

    def fresh(source_, raw_, *, original_problem, source_report_sha256, raw_report_sha256, **kwargs):
        assert original_problem is loaded.problem
        assert source_report_sha256 == hashlib.sha256((base/"shifted-patterned-analysis.json").read_bytes()).hexdigest()
        assert raw_report_sha256 == hashlib.sha256((base/"normalization-report.json").read_bytes()).hexdigest()
        state.calls.append("fresh-source-and-bars")
        return SimpleNamespace(packet=packet(source_report_sha256, raw_report_sha256),
            review={"source_original_coverage": deepcopy(coverage), "stock_cutting": {"status": "pass"}})

    def opening(bars, lanes, problem, host, *, config):
        assert config.maximum_candidates_per_bar == 128 and config.maximum_total_candidates == 20000
        assert len(bars) == 4 and problem is loaded.problem
        state.calls.append("opening")
        return SimpleNamespace(bars=bars, review={"search": {"status": "fixture"}})

    def opening_check(before, after, *args, **kwargs):
        assert before == after and len(before) == 4
        state.calls.append("opening-check")
        return {"host_blocked_before": 2, "host_blocked_after": 1, "physical_bar_count": 4}

    def obligations(*args):
        state.calls.append("freeze-FE")
        return {"frozen": True}

    def solve(bars, frozen, host, **kwargs):
        assert frozen == {"frozen": True}
        assert kwargs == {"reassign_lengths": True, "time_limit_s": 60,
            "maximum_candidates_per_bar": 512, "maximum_total_candidates": 50000}
        state.calls.append("FE-solve")
        if state.on_solve:
            state.on_solve()
        return bars, {"status": "fixture"}

    def fe_check(before, after, *args, **kwargs):
        assert before == after and len(after) == 4
        state.calls.append("FE-check")
        return {"physical_bar_count": 4, "host_blocked_after": 0, "status": "research-only",
            "source_coverage": {"status": "pass"}, "stock_cutting": {"status": "pass"}}

    monkeypatch.setattr(cli, "_code_digest", lambda: "a"*64)
    monkeypatch.setattr(cli, "load_layout_snapshot", lambda *a, **kw: loaded)
    monkeypatch.setattr(cli, "build_physical_bar_trial", fresh)
    monkeypatch.setattr(cli, "load_working_host_json", lambda content, **kw: json.loads(content))
    monkeypatch.setattr(cli, "inspect_working_solid", lambda report: (object(), {}))
    monkeypatch.setattr(cli, "source_service_lanes", lambda *a: object())
    monkeypatch.setattr(cli, "relocate_small_openings", opening)
    monkeypatch.setattr(cli, "check_relocation", opening_check)
    monkeypatch.setattr(cli, "derive_fe_obligations", obligations)
    monkeypatch.setattr(cli, "solve_fe_host_repair", solve)
    monkeypatch.setattr(cli, "check_fe_host_repair", fe_check)
    return state


def test_full_shifted_source_is_revalidated_then_both_stages_are_independently_checked(cli, setup):
    original = {p: p.read_bytes() for p in setup.args.shifted_dir.iterdir()}
    report = cli.run(setup.args)
    assert setup.calls == ["fresh-source-and-bars", "opening", "opening-check", "freeze-FE", "FE-solve", "FE-check"]
    assert report["schema_version"] == "shifted-physical-repair-experiment/v1"
    for flag in ("placement_eligible", "structural_placement_supported", "engineering_approval",
                 "source_demand_removed", "source_demand_values_changed"):
        assert report[flag] is False
    assert set(report["raw_bars_by_direction"]) == set(map(str, PLATE_DIRECTIONS))
    assert sum(map(len, report["raw_bars_by_direction"].values())) == 4
    assert report["stages"]["restored_shifted_normalized"]["expected"] == setup.expected
    assert report["checks"]["physical_bar_count"] == 4 and "Not a Revit packet" in report["warning"]
    assert "packet" not in report
    assert setup.args.output.read_bytes() == _bytes(report)
    assert {p: p.read_bytes() for p in original} == original
    assert {Path(row["path"]) for row in report["source_files"]} >= set(original)


@pytest.mark.parametrize(("filename", "field", "value"), (
    ("normalization-report.json", "source_report_sha256", "0"*64),
    ("normalized-physical-review.json", "source_report_sha256", "0"*64),
    ("normalized-physical-review.json", "raw_report_sha256", "0"*64),
    ("normalized-physical-review.json", "packet_sha256", "0"*64),
    ("experiment.json", "schema_version", "unknown/v1"),
    ("experiment.json", "placement_eligible", 0),
    ("shifted-patterned-analysis.json", "placement_eligible", True),
    ("normalization-report.json", "engineering_approval", True),
    ("experiment.json", "source_demand_removed", True),
    ("experiment.json", "source_to_revit_xy_mm", [1, 0]),
    ("experiment.json", "source_to_revit_xy_mm", [False, False]),
))
def test_exact_chain_permissions_schema_and_coordinate_flags_are_required(cli, setup, filename, field, value):
    path = setup.args.shifted_dir/filename
    content = json.loads(path.read_bytes())
    content[field] = value
    path.write_bytes(_bytes(content))
    with pytest.raises(ValueError):
        cli.run(setup.args)
    assert not setup.args.output.parent.exists() and "opening" not in setup.calls


def test_fresh_snapshot_must_bind_all_four_dxf_and_engineer_pdf(cli, setup):
    setup.loaded.snapshot["source_dxf"] = setup.loaded.snapshot["source_dxf"][:-1]
    with pytest.raises(ValueError, match="DXF/engineer inputs differ"):
        cli.run(setup.args)
    assert not setup.args.output.parent.exists()


@pytest.mark.parametrize("change", ("count", "missing_direction", "coverage", "host"))
def test_missing_physical_or_source_coverage_and_wrong_host_cannot_pass_restoration(cli, setup, change):
    if change == "count":
        setup.review["expected"]["physical_bar_count"] = 3
    elif change == "missing_direction":
        setup.normal["accepted"]["raw_bars_by_direction"].pop(str(PLATE_DIRECTIONS[-1]))
    elif change == "coverage":
        setup.review["source_original_coverage"][0]["uncovered_cell_count"] = 1
    else:
        setup.normal["host_fit"]["source_host_report_sha256"] = "0"*64
    setup.save()
    with pytest.raises(ValueError):
        cli.run(setup.args)
    assert not setup.args.output.parent.exists() and "opening" not in setup.calls


@pytest.mark.parametrize(("field", "value"), (("confirm_identity_xy", False), ("confirm_identity_xy", 1),
    ("opening_time_limit_s", float("nan")), ("opening_time_limit_s", 0), ("fe_time_limit_s", 601),
    ("stock_time_limit_s", 61), ("maximum_fe_candidates_per_bar", True),
    ("maximum_total_fe_candidates", 0)))
def test_cli_limits_fail_before_any_input_or_search_work(cli, setup, field, value):
    setattr(setup.args, field, value)
    with pytest.raises(ValueError):
        cli.run(setup.args)
    assert setup.calls == [] and not setup.args.output.parent.exists()


def test_existing_output_is_never_overwritten(cli, setup):
    setup.args.output.parent.mkdir()
    setup.args.output.write_bytes(b"user result")
    with pytest.raises(ValueError, match="must not be overwritten"):
        cli.run(setup.args)
    assert setup.args.output.read_bytes() == b"user result" and setup.calls == []


@pytest.mark.parametrize("target", ("snapshot", "source", "normalization", "review", "dxf"))
def test_source_change_during_search_prevents_output(cli, setup, target):
    paths = {"snapshot": setup.args.snapshot, "source": setup.args.shifted_dir/"shifted-patterned-analysis.json",
        "normalization": setup.args.shifted_dir/"normalization-report.json",
        "review": setup.args.shifted_dir/"normalized-physical-review.json",
        "dxf": Path(setup.loaded.snapshot["source_dxf"][0]["path"])}
    setup.on_solve = lambda: paths[target].write_bytes(b"changed during search")
    with pytest.raises(ValueError, match="Source changed"):
        cli.run(setup.args)
    assert not setup.args.output.parent.exists()


def test_core_change_during_search_prevents_output(cli, setup, monkeypatch):
    values = iter(("a"*64, "b"*64))
    monkeypatch.setattr(cli, "_code_digest", lambda: next(values))
    with pytest.raises(ValueError, match="Core code changed"):
        cli.run(setup.args)
    assert not setup.args.output.parent.exists()


def test_checker_failure_never_produces_partial_output(cli, setup, monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("independent final validation failed")
    monkeypatch.setattr(cli, "check_fe_host_repair", fail)
    with pytest.raises(ValueError, match="independent final validation"):
        cli.run(setup.args)
    assert not setup.args.output.parent.exists()
