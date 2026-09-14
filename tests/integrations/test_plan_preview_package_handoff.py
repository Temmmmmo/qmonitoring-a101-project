"""Graphic preview ships verified complete input without structural permissions."""
from copy import deepcopy
import hashlib
import importlib
import json
from zipfile import ZipFile

import pytest

import test_physical_package_handoff as physical_fixtures


@pytest.fixture
def physical_package_input(tmp_path, monkeypatch):
    return physical_fixtures.physical_package_input.__wrapped__(tmp_path, monkeypatch)


def test_preview_archive_complete_allowlist_manifest_and_real_input(physical_package_input, tmp_path):
    _, packet_path, packet, review_path, _ = physical_package_input
    module = importlib.import_module("package_revit_plan_preview")
    archive_path = module.build_package(tmp_path / "preview.zip", packet_path, review_path=review_path)
    with ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == len(set(archive.namelist()))
        names = set(archive.namelist())
        assert not any("MVP.panel" in name or "_mvp" in name for name in names)
        assert f"{module.BUTTON}/script.py" in names
        assert "GRAPHIC_PREVIEW_README.md" in names
        assert json.loads(archive.read("physical-bar-plan-trial.json")) == packet
        manifest = json.loads(archive.read("manifest.json"))
        runtime = importlib.import_module("qm_revit_plan_preview")
        assert manifest["version"] == runtime.VERSION == "0.1.1"
        assert manifest["placement_eligible"] is False
        assert manifest["recommended_preview_input"] == "physical-bar-plan-trial.json"
        assert {row["path"] for row in manifest["files"]} == names-{"manifest.json"}
        for row in manifest["files"]:
            content = archive.read(row["path"])
            assert row["bytes"] == len(content)
            assert row["sha256"] == hashlib.sha256(content).hexdigest()
        text = archive.read("README.md").decode("utf-8")
        assert "Plan Preview" in text and "не созданная арматура" in text
        assert "Рабочие наборы удалять не нужно" in text
        assert "ДО сохранения RVT" in text and "0.1.1" in text


def test_preview_archive_rejects_stale_review_and_never_overwrites(physical_package_input, tmp_path):
    _, packet_path, _, review_path, review = physical_package_input
    module = importlib.import_module("package_revit_plan_preview")
    output = module.build_package(tmp_path / "preview.zip", packet_path, review_path=review_path)
    content = output.read_bytes()
    with pytest.raises(FileExistsError):
        module.build_package(output, packet_path, review_path=review_path)
    assert output.read_bytes() == content
    review["packet_sha256"] = "f"*64
    review_path.write_text(json.dumps(review), encoding="utf-8")
    with pytest.raises(ValueError):
        module.build_package(tmp_path / "no.zip", packet_path, review_path=review_path)
    assert not (tmp_path / "no.zip").exists()


@pytest.mark.parametrize("missing", ["relocation_draft_path", "relocation_review_path"])
def test_preview_requires_correction_with_review(physical_package_input, tmp_path, missing):
    _, packet_path, _, review_path, _ = physical_package_input
    module = importlib.import_module("package_revit_plan_preview")
    kwargs = {"relocation_draft_path": tmp_path / "draft.json", "relocation_review_path": tmp_path / "check.json"}
    kwargs[missing] = None
    with pytest.raises(ValueError, match="together"):
        module.build_package(tmp_path / "no.zip", packet_path, review_path=review_path, **kwargs)
    assert not (tmp_path / "no.zip").exists()


def test_preview_uses_only_the_validated_packet_read(physical_package_input, tmp_path, monkeypatch):
    _, packet_path, packet, review_path, _ = physical_package_input
    module = importlib.import_module("package_revit_plan_preview")
    validator = importlib.import_module("qm_physical_packet")
    original = validator.load_packet

    def changed(path):
        result = original(path)
        packet_path.write_text('{"unvalidated_second_read":true}', encoding="utf-8")
        return result

    monkeypatch.setattr(validator, "load_packet", changed)
    output = module.build_package(tmp_path / "preview.zip", packet_path, review_path=review_path)
    with ZipFile(output) as archive:
        assert json.loads(archive.read("physical-bar-plan-trial.json")) == packet


@pytest.fixture
def corrected_preview_input(physical_package_input, tmp_path):
    _, packet_path, packet, review_path, review = physical_package_input
    correction = {"policy_id": "closed-small-opening-transverse-translation/research-v1",
        "moved_bar_count": 0, "moves": [], "host_blocked_before": 0, "host_blocked_after": 0,
        "new_same_direction_body_pairs": 0}
    binding = {"source_packet_sha256": review["packet_sha256"],
        "source_report_sha256": "a"*64, "source_host_report_sha256": "c"*64,
        "source_to_revit_xy_mm": [0, 0], "binding_source": "synthetic packaging fixture only"}
    draft = {"schema_version": "physical-bar-relocation-draft/v1", "units": "mm",
        "placement_eligible": False, "structural_placement_supported": False, "case_id": packet["case_id"],
        **binding, "original_source_zones": deepcopy(packet["source_zones"]), "raw_bars_by_direction": {},
        "expected": {key: packet["expected"][key] for key in ("physical_bar_count", "additional_mass_kg",
            "source_zone_count", "position_count")}, "status": "checked_research_draft", "correction": correction}
    for direction in packet["directions"]:
        name = direction["direction"]
        axis = 0 if name.endswith("X") else 1
        run = direction["runs"][0]
        draft["raw_bars_by_direction"][name] = [{"id": owner["bar_id"], "steel_class": run["steel_class"],
            "diameter_mm": run["diameter_mm"], "coordinate_mm": run["start_xy_mm"][1-axis]+i*run["spacing_mm"],
            "longitudinal_mm": [run["start_xy_mm"][axis], run["end_xy_mm"][axis]],
            "source_bar_ids": [f"source-zone/0/{i}"]} for i, owner in enumerate(run["bar_sources"])]
    draft_content = json.dumps(draft, ensure_ascii=False, allow_nan=False).encode("utf-8")
    correction_review = {"schema_version": "small-opening-relocation-review/v1", **binding, **correction,
        "placement_eligible": False, "engineering_approval": False,
        "draft_sha256": hashlib.sha256(draft_content).hexdigest()}
    draft_path, correction_path = tmp_path / "draft.json", tmp_path / "correction.json"
    draft_path.write_bytes(draft_content)
    correction_path.write_text(json.dumps(correction_review), encoding="utf-8")
    return packet_path, review_path, draft_path, correction_path, correction_review


def test_corrected_graphic_input_ships_alongside_unchanged_original(corrected_preview_input, tmp_path):
    packet_path, review_path, draft_path, correction_path, _ = corrected_preview_input
    module = importlib.import_module("package_revit_plan_preview")
    output = module.build_package(tmp_path / "corrected.zip", packet_path, review_path=review_path,
        relocation_draft_path=draft_path, relocation_review_path=correction_path)
    with ZipFile(output) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["recommended_preview_input"] == "physical-bar-relocation-draft.json"
        assert archive.read("physical-bar-plan-trial.json") == packet_path.read_bytes()
        assert archive.read("physical-bar-relocation-draft.json") == draft_path.read_bytes()
        assert archive.read("opening-relocation-review.json") == correction_path.read_bytes()


@pytest.mark.parametrize(("field", "value"), [("schema_version", "wrong"), ("draft_sha256", "f"*64),
    ("source_packet_sha256", "f"*64), ("engineering_approval", True), ("placement_eligible", True),
    ("host_blocked_after", 8), ("source_to_revit_xy_mm", [1, 0])])
def test_corrected_graphic_review_cannot_be_rebound_silently(corrected_preview_input, tmp_path, field, value):
    packet_path, review_path, draft_path, correction_path, review = corrected_preview_input
    review[field] = value
    correction_path.write_text(json.dumps(review), encoding="utf-8")
    module = importlib.import_module("package_revit_plan_preview")
    with pytest.raises(ValueError):
        module.build_package(tmp_path / "no.zip", packet_path, review_path=review_path,
            relocation_draft_path=draft_path, relocation_review_path=correction_path)
    assert not (tmp_path / "no.zip").exists()
