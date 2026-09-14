"""Original-pattern adaptation, immutable hash chains and thin correction CLI."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebar.application.opening_relocation import correct_small_openings, source_service_lanes
from rebar.application.physical_layout_recovery import _bytes, recover_physical_layout_from_report
from rebar.optimization.contracts.opening_relocation import OpeningRelocationConfig
from rebar.optimization.contracts.physical import PhysicalNormalizationConfig
from test_physical_bar_trial import physical_source_case
from test_physical_host_recovery import box_snapshot
from test_physical_layout_recovery import _deduplicate


@pytest.fixture(scope="module")
def source():
    return physical_source_case()


@pytest.fixture
def recovery(source, monkeypatch):
    monkeypatch.setattr("rebar.application.physical_layout_recovery.normalize_physical_bars", _deduplicate)
    return recover_physical_layout_from_report(source[1], source[0],
        normalization_config=PhysicalNormalizationConfig(), stock_time_limit_s=5)


def correct(source, recovery, host=None, **kwargs):
    return correct_small_openings(recovery, source[0], host or box_snapshot(-2000, 5000),
        **{**dict(config=OpeningRelocationConfig(), offset_x_mm=0, offset_y_mm=0,
            binding_source="synthetic test only", source_report_sha256="b"*64, stock_time_limit_s=5), **kwargs})


def test_finite_lanes_are_reconstructed_from_actual_100_200_period_not_nominal_150(source):
    lanes = source_service_lanes(source[1], source[0])
    assert len(lanes) == 64
    assert {lane.nominal_step_mm for lane in lanes} == {150}
    assert {lane.service_half_widths_mm for lane in lanes} == {(100, 50), (50, 100)}
    assert len({(lane.source.direction, lane.source.id) for lane in lanes}) == 64
    assert {str(lane.source.direction) for lane in lanes} == {"bottom-X", "bottom-Y", "top-X", "top-Y"}
    assert all(lane.axis_window_mm[0] <= lane.source.transverse_axis_mm <= lane.axis_window_mm[1] for lane in lanes)


def test_100_contact_pattern_widths_are_not_guessed_as_uniform_50():
    problem, report, _ = physical_source_case(100)
    lanes = source_service_lanes(report, problem)
    assert {lane.nominal_step_mm for lane in lanes} == {100}
    assert {lane.service_half_widths_mm for lane in lanes} == {(59, 50), (50, 41), (41, 59)}


def test_new_draft_retains_entire_original_physical_plan_and_does_not_mutate_legacy(source, recovery):
    before = deepcopy(recovery)
    result = correct(source, recovery)
    assert recovery == before
    assert result.draft["schema_version"] == "physical-bar-relocation-draft/v1"
    assert result.review["schema_version"] == "small-opening-relocation-review/v1"
    assert result.draft["original_source_zones"] == recovery.packet["source_zones"]
    assert result.draft["raw_bars_by_direction"] == recovery.normalization_report["accepted"]["raw_bars_by_direction"]
    assert result.review["stock_cutting"]["status"] == "pass"
    assert result.review["source_coverage_after"]["status"] == "pass"
    assert result.review["moved_bar_count"] == 0
    assert result.review["host_blocked_after"] == 0
    assert result.review["physical_bar_count"] == 32
    assert result.review["new_same_direction_body_pairs"] == 0
    assert result.review["draft_sha256"] == hashlib.sha256(result.draft_bytes).hexdigest()
    assert result.draft_bytes == _bytes(result.draft)
    assert result.review_bytes == _bytes(result.review)
    assert not result.draft["structural_placement_supported"]
    assert not result.draft["placement_eligible"]
    assert not result.review["engineering_approval"]


def test_host_offset_applies_to_host_only_not_source_positions(source, recovery):
    first = correct(source, recovery)
    second = correct(source, recovery, box_snapshot(-2000, 5000, dx=1234, dy=-5678),
                     offset_x_mm=1234, offset_y_mm=-5678)
    assert first.draft["raw_bars_by_direction"] == second.draft["raw_bars_by_direction"]
    assert second.review["host_blocked_after"] == 0
    assert second.draft["source_to_revit_xy_mm"] == [1234, -5678]


@pytest.mark.parametrize("kwargs", ({"binding_source": ""}, {"source_report_sha256": ""},
    {"source_report_sha256": "Z"*64}, {"offset_x_mm": float("nan")}, {"offset_y_mm": True}))
def test_binding_cannot_be_missing_or_nonfinite(source, recovery, kwargs):
    with pytest.raises(ValueError):
        correct(source, recovery, **kwargs)


@pytest.mark.parametrize("field", ("packet", "review", "patterned_report", "normalization_report"))
def test_mutation_after_hash_binding_cannot_authorize_correction(source, recovery, field):
    changed = deepcopy(getattr(recovery, field))
    changed["extra-unbound-key"] = True
    with pytest.raises(ValueError, match="bound bytes"):
        correct(source, replace(recovery, **{field: changed}))


def test_forged_packet_sha_rejected_even_when_recoded_as_canonical_json(source, recovery):
    packet = deepcopy(recovery.packet)
    packet["raw_report_sha256"] = "0"*64
    with pytest.raises(ValueError, match="hash chain"):
        correct(source, replace(recovery, packet=packet, packet_bytes=_bytes(packet)))


def test_reconstructed_source_fails_if_periodic_axes_or_fe_are_tampered(source):
    problem, report, _ = deepcopy(source)
    report["directions"][0]["candidates"][-1]["zone_drafts"][0]["components"][0]["axis_coordinates_mm"][0] += 1
    with pytest.raises(ValueError):
        source_service_lanes(report, problem)


@pytest.fixture
def cli():
    path = Path(__file__).resolve().parents[2] / "scripts/correct_small_openings.py"
    spec = importlib.util.spec_from_file_location("small_opening_cli_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def args_for(cli, tmp_path):
    pipeline = tmp_path / "pipeline"
    pipeline.mkdir(exist_ok=True)
    return cli._parser().parse_args(["--pipeline-dir", str(pipeline), "--snapshot", str(tmp_path / "snapshot.json"),
        "--candidate-id", "chosen", "--working-host-report", str(tmp_path / "host.json"),
        "--host-offset-x-mm", "0", "--host-offset-y-mm", "0", "--host-binding-source", "test only",
        "--output-dir", str(tmp_path / "new-output")])


def test_cli_never_overwrites_existing_output(cli, tmp_path):
    args = args_for(cli, tmp_path)
    args.output_dir.mkdir()
    sentinel = args.output_dir / "keep.txt"
    sentinel.write_text("user data")
    with pytest.raises(ValueError, match="already exists"):
        cli.run(args)
    assert sentinel.read_text() == "user data"


@pytest.mark.parametrize("content", (b'{"x":1,"x":2}', b'{"x": NaN}', b'{"x": 1e999}', b'[]', b'', b'{"x":1}'))
def test_cli_reader_rejects_duplicate_nonfinite_empty_or_noncanonical_json(cli, tmp_path, content):
    path = tmp_path / "source.json"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        cli._read(path)


def test_cli_reader_binds_exact_canonical_bytes_and_limits_size(cli, tmp_path):
    path = tmp_path / "source.json"
    content = _bytes({"кириллица": [1, 2, False]})
    path.write_bytes(content)
    value, bound, record = cli._read(path)
    assert _bytes(value) == bound == content
    assert record["sha256"] == hashlib.sha256(content).hexdigest()
    with pytest.raises(ValueError, match="oversized"):
        cli._read(path, maximum=10)


@pytest.mark.parametrize(("key", "value"), (("maximum_shift_mm", 301), ("host_offset_x_mm", float("inf")),
    ("maximum_candidates_per_bar", 129), ("maximum_total_candidates", 0), ("time_limit_s", -1),
    ("host_binding_source", " "), ("stock_time_limit_s", float("nan"))))
def test_cli_validates_finite_limits_before_reading_sources(cli, tmp_path, key, value):
    args = args_for(cli, tmp_path)
    setattr(args, key, value)
    with pytest.raises(ValueError):
        cli.run(args)
    assert not args.output_dir.exists()


def test_human_result_does_not_claim_structural_placement(cli, source, recovery):
    result = correct(source, recovery)
    text = cli._result_md(result, {"candidate_id": "fixture"})
    assert "32 стержней" in text
    assert "НЕ команда создания Rebar" in text
    assert "placement_eligible=false" in text
    assert "Загибы, муфты" in text


def test_cli_input_files_changing_after_calculation_prevent_all_output(cli, source, recovery, tmp_path, monkeypatch):
    args = args_for(cli, tmp_path)
    args.snapshot.write_bytes(b"snapshot")
    args.working_host_report.write_bytes(_bytes(box_snapshot(-2000, 5000)))
    record = cli.source_record(args.snapshot, role="source-snapshot")
    loaded = SimpleNamespace(problem=source[0], source_sha256=record["sha256"], snapshot={"candidate_id": "fixture"})
    monkeypatch.setattr(cli, "load_layout_snapshot", lambda *a, **kw: loaded)
    monkeypatch.setattr(cli, "_load_recovery", lambda *a: (recovery, {}, []))
    actual = cli.correct_small_openings
    def mutating(*args_, **kwargs):
        result = actual(*args_, **kwargs)
        args.snapshot.write_bytes(b"changed source")
        return result
    monkeypatch.setattr(cli, "correct_small_openings", mutating)
    with pytest.raises(ValueError, match="Source changed"):
        cli.run(args)
    assert not args.output_dir.exists()
