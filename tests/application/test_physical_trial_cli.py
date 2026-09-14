"""Bounded strict input handling for the separate physical plan export CLI."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("export_physical_bar_trial")


@pytest.mark.parametrize("content", [b"{", b"[]", b'{"x":1,"x":2}', b'{"x":NaN}',
                                    b'{"x":Infinity}', b'{"x":1e9999}', b"\xff", b""])
def test_strict_json_rejects_malformed_duplicates_nonfinite_and_empty(cli, tmp_path, content):
    path = tmp_path / "input.json"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        cli._read_json(path)


def test_json_hash_is_of_original_bytes_including_bom(cli, tmp_path):
    content = b'\xef\xbb\xbf{"mm": 12.5}\n'
    path = tmp_path / "input.json"
    path.write_bytes(content)
    parsed, digest = cli._read_json(path)
    assert parsed == {"mm": 12.5}
    assert digest == hashlib.sha256(content).hexdigest()


def test_input_bound_and_extension(cli, tmp_path, monkeypatch):
    path = tmp_path / "input.json"
    path.write_bytes(b'{"test":123}')
    monkeypatch.setattr(cli, "MAX_INPUT_BYTES", 4)
    with pytest.raises(ValueError, match="oversized"):
        cli._read_json(path)
    for other in (tmp_path / "absent.json", tmp_path / "input.txt", tmp_path):
        with pytest.raises(ValueError, match="existing local JSON"):
            cli._read_json(other)


@pytest.fixture
def export_inputs(cli, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("physical_cli_fixture",
        Path(__file__).with_name("test_physical_bar_trial.py"))
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    problem, report, raw = fixture.physical_source_case()
    snapshot = {"path": str(tmp_path / "snapshot.json"), "candidate_id": "plate:1",
                "sha256": "c" * 64, "source_dxf": [], "source_pdf": {}}
    report["source_snapshot"] = snapshot
    source_path, normalized_path = tmp_path / "source.json", tmp_path / "normalized.json"
    source_path.write_text(json.dumps(report), encoding="utf-8")
    normalized = {"placement_eligible": False, "engineering_approval": False,
                  "source_demand_removed": False, "accepted": {"raw_bars_by_direction": raw}}
    normalized_path.write_text(json.dumps(normalized), encoding="utf-8")
    loaded = SimpleNamespace(problem=problem, source_sha256=snapshot["sha256"], snapshot=snapshot,
        engineer_comparison={"mass_kg": 1000.0, "physical_bar_count": 40, "specification_rows": 5})
    # Only filesystem ingest is isolated: source reconstruction, physical builder,
    # stock check, packet validation, serialization and review binding are real.
    monkeypatch.setattr(cli, "load_layout_snapshot", lambda *_a, **_kw: loaded)
    monkeypatch.setattr(cli, "_source_record", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli, "_code_digest", lambda: "d" * 64)
    return source_path, normalized_path, report, normalized, loaded


def test_export_writes_checked_packet_and_review_without_overwriting(cli, export_inputs, tmp_path):
    source, normalized, _, _, _ = export_inputs
    output = tmp_path / "ready"
    result = cli.export_physical_trial(source, normalized, output)
    assert result["physical_bar_count"] == 32
    assert result["source_zone_count"] == 8 and result["execution_group_count"] == 4
    assert result["placement_eligible"] is False
    assert set(p.name for p in output.iterdir()) == {"physical-bar-plan-trial.json", "engineer-review.json", "README.md"}
    packet = (output / "physical-bar-plan-trial.json").read_bytes()
    review = json.loads((output / "engineer-review.json").read_bytes())
    assert review["packet_sha256"] == hashlib.sha256(packet).hexdigest() == result["packet_sha256"]
    assert review["engineer_comparison"]["physical_bar_count"] == 40
    assert review["source_original_coverage"][0]["status"] == "pass"
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    with pytest.raises(ValueError, match="already exists"):
        cli.export_physical_trial(source, normalized, output)
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before


@pytest.mark.parametrize("change", ["source_hash", "removed", "approved", "case", "geometry", "code_drift", "input_drift"])
def test_invalid_or_changed_export_never_creates_handoff(cli, export_inputs, tmp_path, monkeypatch, change):
    source_path, raw_path, report, raw, _ = export_inputs
    if change == "source_hash":
        report["source_snapshot"]["sha256"] = "e" * 64
    elif change == "removed":
        raw["source_demand_removed"] = True
    elif change == "approved":
        raw["engineering_approval"] = True
    elif change == "case":
        report["case_id"] = "another-project"
    elif change == "geometry":
        raw["accepted"]["raw_bars_by_direction"]["bottom-X"][0]["coordinate_mm"] += 1
    elif change == "code_drift":
        values = iter(("a" * 64, "b" * 64))
        monkeypatch.setattr(cli, "_code_digest", lambda: next(values))
    elif change == "input_drift":
        original_read = cli._read_json
        count = 0

        def changed_read(path):
            nonlocal count
            count += 1
            value, digest = original_read(path)
            return value, digest if count <= 2 else "e" * 64

        monkeypatch.setattr(cli, "_read_json", changed_read)
    source_path.write_text(json.dumps(report), encoding="utf-8")
    raw_path.write_text(json.dumps(raw), encoding="utf-8")
    output = tmp_path / "must-not-exist"
    with pytest.raises(ValueError):
        cli.export_physical_trial(source_path, raw_path, output)
    assert not output.exists()
