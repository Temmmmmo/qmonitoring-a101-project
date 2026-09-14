"""The physical-plan ZIP preserves provenance and unresolved tasks; no save code."""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from rebar.optimization.services.detailing import rebar_mass_kg

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def physical_package_input(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    monkeypatch.syspath_prepend(str(ROOT / "integrations/pyrevit/QMonitoring.extension/lib"))
    module = importlib.import_module("package_revit_physical_trial")
    packet = {"schema_version": "physical-bar-plan-trial/v1", "mode": "commit-readback-rollback",
        "units": "mm", "placement_eligible": False, "case_id": "Рабочая плита",
        "source_report_sha256": "a" * 64, "raw_report_sha256": "b" * 64,
        "source_blockers": ["host-boundary-cover-openings", "engineering-approval"],
        "source_zones": [], "directions": [], "manual_joint_tasks": [],
        "expected": {"source_zone_count": 4, "execution_group_count": 4, "run_count": 4,
            "physical_bar_count": 8, "additional_mass_kg": rebar_mass_kg(10, 1950, 8), "position_count": 1}}
    for name in ("bottom-X", "bottom-Y", "top-X", "top-Y"):
        axis = 0 if name.endswith("X") else 1
        start, end = [100.0, 100.0], [100.0, 100.0]
        start[axis], end[axis] = 500.0, 2450.0
        packet["source_zones"].append({"direction": name, "zone_id": "source-zone", "components": [{
            "component_index": 0, "source_bar_count": 2, "diameter_mm": 10,
            "required_interval_mm": [1000.0, 1500.0], "axis_coordinates_mm": [100.0, 400.0],
            "steel_class": "A500", "background_diameter_mm": 10, "background_origin_mm": 0.0}]})
        packet["directions"].append({"direction": name, "source": name + ".dxf", "runs": [{
            "id": name + ":run", "execution_group_id": "physical-group", "steel_class": "A500",
            "diameter_mm": 10, "start_xy_mm": start, "end_xy_mm": end, "bar_count": 2, "spacing_mm": 300.0,
            "bar_sources": [{"bar_id": "bar-" + str(i), "source_refs": [{"zone_id": "source-zone",
                "component_index": 0, "bar_index": i}]} for i in range(2)]}]})
    serialize = importlib.import_module("qm_revit_probe").serialize_report_utf8
    content = serialize(packet)
    packet_path = tmp_path / "physical.json"
    packet_path.write_bytes(content)
    review = {"schema_version": "physical-bar-plan-review/v1", "placement_eligible": False,
        "source_report_sha256": packet["source_report_sha256"], "raw_report_sha256": packet["raw_report_sha256"],
        "packet_sha256": hashlib.sha256(content).hexdigest(), "expected": packet["expected"],
        "manual_joint_tasks": packet["manual_joint_tasks"]}
    review_path = tmp_path / "review.json"
    review_path.write_bytes(serialize(review))
    return module, packet_path, packet, review_path, review


def test_new_button_exact_files_and_unambiguous_counts(physical_package_input, tmp_path):
    module, packet_path, packet, review_path, review = physical_package_input
    output = module.build_package(tmp_path / "physical.zip", packet_path, review_path=review_path)
    with ZipFile(output) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert set(names) == set(module.PROBE_FILES) | set(module.PLATE_FILES) | set(module.NEW_FILES) | {
            "REFERENCE_README.md", "physical-bar-plan-trial.json", "engineer-review.json"}
        assert not any("MVP.panel" in name or "_mvp" in name for name in names)
        assert "full-plate-trial.json" not in names
        assert json.loads(archive.read("physical-bar-plan-trial.json")) == packet
        assert json.loads(archive.read("engineer-review.json")) == review
        readme = archive.read("README.md").decode("utf-8")
        for text in ("Physical Plan Trial", "8 стержней", "Исходных параметрических зон: 4",
                     "Групп одинаковой физической геометрии: 4", "0 нерешённых пересечений",
                     "локальное создание обязательно откатится", "не сохраняй копию", "Custom Extension Directories",
                     "заимствование плиты", "WORKSHARING_README.md", "Рабочие наборы удалять не нужно"):
            assert text in readme
        assert "399" not in readme and "2531" not in readme


@pytest.mark.parametrize("change", ["source_hash", "raw_hash", "packet_hash", "eligible", "count", "tasks",
                                    "duplicate", "nan", "infinite", "malformed"])
def test_wrong_or_lossy_review_never_packaged(physical_package_input, tmp_path, change):
    module, packet_path, _, review_path, review = physical_package_input
    if change in ("source_hash", "raw_hash", "packet_hash"):
        review[{"source_hash": "source_report_sha256", "raw_hash": "raw_report_sha256",
                "packet_hash": "packet_sha256"}[change]] = "c" * 64
    elif change == "eligible":
        review["placement_eligible"] = True
    elif change == "count":
        review["expected"]["physical_bar_count"] += 1
    elif change == "tasks":
        review["manual_joint_tasks"] = [{"status": "unresolved"}]
    content = json.dumps(review)
    if change == "duplicate":
        content = '{"placement_eligible":false,"placement_eligible":false}'
    elif change == "nan":
        content = '{"value":NaN}'
    elif change == "infinite":
        content = '{"value":1e9999}'
    elif change == "malformed":
        content = "{"
    review_path.write_text(content, encoding="utf-8")
    output = tmp_path / "no.zip"
    with pytest.raises(ValueError):
        module.build_package(output, packet_path, review_path=review_path)
    assert not output.exists()


def test_packet_geometry_tamper_and_no_overwrite(physical_package_input, tmp_path):
    module, packet_path, packet, review_path, _ = physical_package_input
    output = module.build_package(tmp_path / "one.zip", packet_path, review_path=review_path)
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        module.build_package(output, packet_path, review_path=review_path)
    assert output.read_bytes() == before
    packet["directions"][0]["runs"][0]["start_xy_mm"][0] += 500
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    with pytest.raises(ValueError):
        module.build_package(tmp_path / "tampered.zip", packet_path, review_path=review_path)
    assert not (tmp_path / "tampered.zip").exists()


def test_package_uses_validated_packet_bytes(physical_package_input, tmp_path, monkeypatch):
    module, packet_path, packet, review_path, _ = physical_package_input
    validator = importlib.import_module("qm_physical_packet")
    original = validator.load_packet

    def change_after_read(path):
        parsed = original(path)
        packet_path.write_text('{"unvalidated":"second read"}', encoding="utf-8")
        return parsed

    monkeypatch.setattr(validator, "load_packet", change_after_read)
    output = module.build_package(tmp_path / "validated.zip", packet_path, review_path=review_path)
    with ZipFile(output) as archive:
        assert json.loads(archive.read("physical-bar-plan-trial.json")) == packet
