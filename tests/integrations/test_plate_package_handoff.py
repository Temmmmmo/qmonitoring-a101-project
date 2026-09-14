"""A safe full-trial ZIP describes its actual packet and binds optional review."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from rebar.optimization.services.detailing import rebar_mass_kg

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def package_input(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    monkeypatch.syspath_prepend(str(ROOT / "integrations/pyrevit/QMonitoring.extension/lib"))
    module = importlib.import_module("package_revit_plate_trial")
    packet = {"schema_version": "qmonitoring-full-plate-trial/v1", "mode": "commit-readback-rollback",
        "units": "mm", "placement_eligible": False, "case_id": "Обычная плита над третьим",
        "source_report_sha256": "a" * 64, "source_blockers": ["stock-cutting", "host-boundary-cover-openings"],
        "directions": [], "expected": {"zone_count": 4, "run_count": 4, "physical_bar_count": 8,
                                        "additional_mass_kg": rebar_mass_kg(18, 2000, 8)}}
    for name in ("bottom-X", "bottom-Y", "top-X", "top-Y"):
        axis = 0 if name.endswith("X") else 1
        end = [1000, 1000]
        end[axis] += 2000
        packet["directions"].append({"direction": name, "source": name + ".dxf", "runs": [{
            "id": name, "zone_id": "zone", "component_index": 0, "steel_class": "A500",
            "diameter_mm": 18, "start_xy_mm": [1000, 1000], "end_xy_mm": end,
            "bar_count": 2, "spacing_mm": 300}]})
    source = tmp_path / "source.json"
    source.write_text(json.dumps(packet), encoding="utf-8")
    review = {"schema_version": "qmonitoring-layout-plate-review/v1", "placement_eligible": False,
        "source_report_sha256": packet["source_report_sha256"], "expected": packet["expected"],
        "source_metrics": {"direction_count": 4, "zone_count": 4, "physical_bar_count": 8,
                           "total_mass_kg": packet["expected"]["additional_mass_kg"]}}
    review_path = tmp_path / "a-filename-not-used-in-archive.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")
    notes = tmp_path / "arbitrary-source-name.md"
    notes.write_text("# Проверка выбранной плиты\nВсе направления, обязательный откат.\n", encoding="utf-8")
    return module, source, packet, review_path, review, notes


def test_default_whitelist_unchanged_and_generated_readme_uses_packet(package_input, tmp_path):
    module, source, packet, _, _, _ = package_input
    target = module.build_package(tmp_path / "default.zip", source)
    with ZipFile(target) as archive:
        expected = set(module.PROBE_FILES) | set(module.NEW_FILES) | {"REFERENCE_README.md", "full-plate-trial.json"}
        assert set(archive.namelist()) == expected
        readme = archive.read("README.md").decode("utf-8")
        assert packet["case_id"] in readme
        assert "4 зон / 4 Rebar-наборов" in readme
        assert "8 физических стержней" in readme
        assert "37 942" not in readme and "2531" not in readme and "990" not in readme
        assert "A500 Ø18" in readme
        assert "Custom Extension Directories" in readme
        assert "Diagnostics → Full Plate Trial" in readme
        assert "обязательно откатится" in readme
        assert "заимствование плиты может остаться" in readme
        assert "Рабочие наборы удалять не нужно" in readme
        assert "не сохраняй копию" in readme
        assert not any("MVP.panel" in name or "_mvp" in name for name in archive.namelist())
        for name in module.NEW_FILES:
            assert archive.read(name) == (module.SOURCE / name).read_bytes()


def test_review_and_case_readme_only_use_fixed_names(package_input, tmp_path):
    module, source, packet, review_path, review, notes = package_input
    target = module.build_package(tmp_path / "with-review.zip", source,
                                  review_path=review_path, case_readme_path=notes)
    with ZipFile(target) as archive:
        expected = set(module.PROBE_FILES) | set(module.NEW_FILES) | {
            "REFERENCE_README.md", "full-plate-trial.json", "engineer-review.json", "CASE_README.md"}
        assert set(archive.namelist()) == expected
        assert json.loads(archive.read("full-plate-trial.json")) == packet
        assert json.loads(archive.read("engineer-review.json")) == review
        assert archive.read("CASE_README.md") == notes.read_bytes()
        assert "engineer-review.json" in archive.read("README.md").decode()


@pytest.mark.parametrize("change", ["hash", "eligible", "mass", "quantity", "directions", "metric_type", "malformed", "duplicate", "nan"])
def test_stale_or_malformed_review_rejected_before_zip_creation(package_input, tmp_path, change):
    module, source, _, review_path, review, _ = package_input
    if change == "hash":
        review["source_report_sha256"] = "b" * 64
    elif change == "eligible":
        review["placement_eligible"] = True
    elif change == "mass":
        review["source_metrics"]["total_mass_kg"] += 1
    elif change == "quantity":
        review["expected"]["physical_bar_count"] += 1
    elif change == "directions":
        review["source_metrics"]["direction_count"] = 3
    elif change == "metric_type":
        review["metrics"] = []
    content = json.dumps(review)
    if change == "malformed":
        content = "{"
    elif change == "duplicate":
        content = '{"placement_eligible":false,"placement_eligible":false}'
    elif change == "nan":
        content = '{"placement_eligible":false,"bad":NaN}'
    review_path.write_text(content, encoding="utf-8")
    target = tmp_path / "must-not-exist.zip"
    with pytest.raises(ValueError):
        module.build_package(target, source, review_path=review_path)
    assert not target.exists()


@pytest.mark.parametrize("change", ["missing", "binary", "empty", "extension"])
def test_case_readme_rejected_before_archive_creation(package_input, tmp_path, change):
    module, source, _, _, _, notes = package_input
    if change == "missing":
        notes = tmp_path / "missing.md"
    elif change == "binary":
        notes.write_bytes(b"\xff\xfe")
    elif change == "empty":
        notes.write_bytes(b"")
    else:
        notes = tmp_path / "wrong.txt"
        notes.write_text("not markdown", encoding="utf-8")
    target = tmp_path / "must-not-exist.zip"
    with pytest.raises((ValueError, OSError)):
        module.build_package(target, source, case_readme_path=notes)
    assert not target.exists()


def test_bounded_attachments(package_input, tmp_path, monkeypatch):
    module, source, _, review_path, _, notes = package_input
    monkeypatch.setattr(module, "MAX_REVIEW_BYTES", 8)
    monkeypatch.setattr(module, "MAX_README_BYTES", 8)
    with pytest.raises(ValueError, match="oversized"):
        module.build_package(tmp_path / "no-review.zip", source, review_path=review_path)
    with pytest.raises(ValueError, match="oversized"):
        module.build_package(tmp_path / "no-readme.zip", source, case_readme_path=notes)


def test_no_overwrite_with_optional_attachments(package_input, tmp_path):
    module, source, _, review_path, _, notes = package_input
    target = module.build_package(tmp_path / "one.zip", source,
                                  review_path=review_path, case_readme_path=notes)
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        module.build_package(target, source, review_path=review_path, case_readme_path=notes)
    assert target.read_bytes() == before


def test_readme_and_packet_come_from_validated_bytes_not_a_second_read(package_input, tmp_path, monkeypatch):
    module, source, packet, _, _, _ = package_input
    validator = importlib.import_module("qm_plate_packet")
    original = validator.load_packet

    def load_and_change(path):
        parsed = original(path)
        source.write_text('{"case_id":"WRONG SECOND READ"}', encoding="utf-8")
        return parsed

    monkeypatch.setattr(validator, "load_packet", load_and_change)
    target = module.build_package(tmp_path / "validated.zip", source)
    with ZipFile(target) as archive:
        assert json.loads(archive.read("full-plate-trial.json")) == packet
        assert "WRONG SECOND READ" not in archive.read("README.md").decode()
        assert packet["case_id"] in archive.read("README.md").decode()
