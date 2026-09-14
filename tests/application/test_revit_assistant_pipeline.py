"""One-command preparation delegates to the real physical pipeline, never saved raw bars."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from rebar.application.assistant_inputs import AssistantSourceSelection

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("prepare_revit_assistant")


def _args(cli, output, *extra):
    return cli._parser().parse_args(["--case", "k09-typical-3-14", "--background-origin-mm", "0",
        "--first-300-offset-mm", "100", "--contact-side", "left", "--steel-class", "A500",
        "--phase-source", "Explicit synthetic test", "--output-dir", str(output), *extra])


@pytest.fixture
def pipeline_source(cli, monkeypatch):
    spec = importlib.util.spec_from_file_location("assistant_pipeline_fixture",
        Path(__file__).with_name("test_patterned_layout_recovery.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    problem, solution, _ = module._source()
    source = AssistantSourceSelection(problem, solution,
        {"source_files": [], "candidate_id": "synthetic:1", "placement_eligible": False},
        {"mass_kg": 1000.0, "physical_bar_count": 50, "specification_rows": 5})
    monkeypatch.setattr(cli, "_load_sources", lambda _args: source)
    monkeypatch.setattr(cli, "_code_digest", lambda: "a" * 64)
    return source


def test_complete_pipeline_needs_no_precomputed_normalization_and_emits_safe_zip(cli, pipeline_source, tmp_path):
    output = tmp_path / "complete"
    summary = cli.run_pipeline(_args(cli, output))
    assert summary["status"] == "prepared_rollback_only"
    assert summary["expected"]["physical_bar_count"] == 32
    assert summary["unresolved_intersection_pair_count"] == 0
    assert summary["placement_eligible"] is False
    assert summary["engineer_comparison"]["physical_bar_count"] == 50
    assert json.loads((output / "pipeline-summary.json").read_bytes()) == summary
    packet_content = (output / "physical-bar-plan-trial.json").read_bytes()
    packet = json.loads(packet_content)
    review = json.loads((output / "engineer-review.json").read_bytes())
    normalization = json.loads((output / "physical-normalization.json").read_bytes())
    assert normalization["accepted"]["raw_bars_by_direction"]
    assert packet["source_report_sha256"] == hashlib.sha256((output / "patterned-analysis.json").read_bytes()).hexdigest()
    assert packet["raw_report_sha256"] == hashlib.sha256((output / "physical-normalization.json").read_bytes()).hexdigest()
    assert review["packet_sha256"] == hashlib.sha256(packet_content).hexdigest()
    assert review["pipeline"]["automatic_normalization"] is True
    assert review["stock_cutting"]["status"] == "pass"
    with ZipFile(summary["archive"]) as archive:
        assert archive.read("physical-bar-plan-trial.json") == packet_content
        assert json.loads(archive.read("engineer-review.json")) == review
        assert not any("MVP.panel" in name for name in archive.namelist())
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    with pytest.raises(ValueError, match="already exists"):
        cli.run_pipeline(_args(cli, output))
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before


@pytest.mark.parametrize("change", ["code", "input"])
def test_drift_prevents_publication(cli, pipeline_source, tmp_path, monkeypatch, change):
    if change == "code":
        values = iter(("a" * 64, "b" * 64))
        monkeypatch.setattr(cli, "_code_digest", lambda: next(values))
    else:
        def changed(_records):
            raise ValueError("Source changed")

        monkeypatch.setattr(cli, "verify_source_records", changed)
    output = tmp_path / "drift"
    with pytest.raises(ValueError, match="changed"):
        cli.run_pipeline(_args(cli, output))
    assert not output.exists()


def test_blocked_patterned_stock_writes_diagnostics_without_a_revit_packet(cli, pipeline_source, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from rebar.application import physical_layout_recovery as recovery
    patterned = recovery.recover_patterned_layout(pipeline_source.problem, pipeline_source.solution,
        tuple(recovery.CompositeDirectionSettings(d, 0, 100, 0, "A500", "test", "left")
              for d in recovery.PLATE_DIRECTIONS))
    monkeypatch.setattr(recovery, "recover_patterned_layout", lambda *_a, **_kw: SimpleNamespace(
        stock_balanced=False, report=patterned.report))
    output = tmp_path / "blocked"
    result = cli.run_pipeline(_args(cli, output))
    assert result["status"] == "blocked_patterned_stock"
    assert "archive" not in result
    assert not (output / "physical-bar-plan-trial.json").exists()
    assert (output / "engineer-review.json").exists()
    assert "Пакет Revit не создан" in (output / "README.md").read_text()


@pytest.mark.parametrize("options", [("--population", "3"), ("--seed", "-1"),
    ("--normalization-time-limit-s", "601"), ("--stock-time-limit-s", "nan"),
    ("--maximum-exchange-attempts", "301"), ("--first-300-offset-mm", "300"),
    ("--maximum-source-bars", "0"), ("--maximum-source-mass-kg", "inf"),
    ("--candidate-id", "not-a-snapshot"), ("--mapping", "not-custom-dxf"), ("--steel-class", " ")])
def test_bad_cli_options_fail_before_reading_or_writing(cli, tmp_path, monkeypatch, options):
    def forbidden(_args):
        pytest.fail("input read must not start for invalid controls")

    monkeypatch.setattr(cli, "_load_sources", forbidden)
    output = tmp_path / "invalid"
    with pytest.raises(ValueError):
        cli.run_pipeline(_args(cli, output, *options))
    assert not output.exists()


def test_generic_dxf_requires_explicit_scale_context(cli, tmp_path):
    common = ["--dxf", "a.dxf", "b.dxf", "c.dxf", "d.dxf", "--background-origin-mm", "0",
        "--first-300-offset-mm", "100", "--contact-side", "left", "--steel-class", "A500",
        "--phase-source", "test", "--output-dir", str(tmp_path / "new")]
    with pytest.raises(ValueError, match="case-id"):
        cli._validate_args(cli._parser().parse_args(common))
    args = cli._parser().parse_args([*common, "--case-id", "own-case", "--mapping", "explicit"])
    cli._validate_args(args)


@pytest.mark.parametrize("options", [
    ("--host-offset-x-mm", "0"), ("--host-conservative-whole-height",),
    ("--working-host-report", "host.json"),
    ("--working-host-report", "host.json", "--host-offset-x-mm", "0", "--host-offset-y-mm", "0",
     "--host-binding-source", "explicit test"),
])
def test_host_options_never_infer_coordinates_or_depths(cli, tmp_path, options):
    with pytest.raises(ValueError):
        cli._validate_args(_args(cli, tmp_path / "out", *options))


@pytest.mark.parametrize("lower,upper,expected_status,exit_code", [
    (-800, 4000, "prepared_rollback_only", 0), (0, 1500, "blocked_working_host", 2),
])
def test_pipeline_host_fit_revalidates_full_inventory_and_reports_blocked_status(
    cli, pipeline_source, tmp_path, lower, upper, expected_status, exit_code,
):
    from test_physical_host_recovery import box_snapshot
    host = tmp_path / "host.json"
    host.write_text(json.dumps(box_snapshot(lower, upper)), encoding="utf-8")
    output = tmp_path / "host-result"
    args = _args(cli, output, "--working-host-report", str(host),
        "--host-offset-x-mm", "0", "--host-offset-y-mm", "0", "--host-binding-source", "synthetic test",
        "--host-conservative-whole-height")
    summary = cli.run_pipeline(args)
    assert summary["status"] == expected_status
    assert summary["expected"]["physical_bar_count"] == 32
    review = json.loads((output / "working-host-fit.json").read_bytes())
    assert review["blocked_before"] == 32
    assert review["blocked_after"] == (0 if exit_code == 0 else 32)
    assert review["source_host_report_sha256"] == hashlib.sha256(host.read_bytes()).hexdigest()
    assert not review["placement_eligible"]
    assert len(json.loads((output / "physical-bar-plan-trial.json").read_bytes())["directions"]) == 4
