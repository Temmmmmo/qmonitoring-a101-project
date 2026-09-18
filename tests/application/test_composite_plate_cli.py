import json
import importlib
from pathlib import Path
import subprocess
import sys

from rebar.optimization.contracts.plate import PLATE_DIRECTIONS


def test_cli_runs_all_four_real_parsers_and_refuses_to_overwrite(composite_plate_sources, tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts/analyze_composite_plate.py"
    output = tmp_path / "полный результат.json"
    arguments = [sys.executable, str(script)]
    for source, direction in zip(composite_plate_sources, PLATE_DIRECTIONS):
        key = f"{direction.layer.value}-{direction.axis.value.lower()}"
        arguments.extend(("--dxf-" + key, str(source.dxf_path), "--shk-" + key, str(source.shk_path), "--origin-" + key, "0"))
    arguments.extend(("--first-300-offset", "150", "--second-offset", "50", "--steel-class", "A500",
        "--phase-source", "Synthetic CLI test only", "--maximum-candidates", "32", "--solver-time-limit", "2",
        "--cutting-profile", "continuous", "--output", str(output)))
    completed = subprocess.run(arguments, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text())
    assert len(report["directions"]) == 4 and report["front"] and not report["placement_eligible"]
    before = output.read_bytes()
    repeated = subprocess.run(arguments, capture_output=True, text=True, timeout=20)
    assert repeated.returncode == 2 and output.read_bytes() == before
    assert "не перезаписываются" in repeated.stderr


def test_cli_explicit_zone_merge_mode_writes_four_direction_front(composite_plate_sources, tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts/analyze_composite_plate.py"
    output = tmp_path / "зоны.json"
    arguments = [sys.executable, str(script)]
    for source, direction in zip(composite_plate_sources, PLATE_DIRECTIONS):
        key = f"{direction.layer.value}-{direction.axis.value.lower()}"
        arguments.extend(("--dxf-" + key, str(source.dxf_path), "--shk-" + key, str(source.shk_path),
                          "--origin-" + key, "0"))
    arguments.extend(("--first-300-offset", "150", "--second-offset", "50", "--steel-class", "A500",
                      "--phase-source", "Synthetic CLI test only", "--search-mode", "zone-merge",
                      "--solver-time-limit", "3", "--cutting-profile", "continuous", "--output", str(output)))
    completed = subprocess.run(arguments, capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text())
    assert report["search_mode"] == "zone-merge" and report["front"]
    assert report["selected_index"] == report["zone_tradeoff"]["knee"]["index"]
    assert len(report["directions"]) == 4


def test_cli_scale_alias_dispatches_png_and_legacy_shk_without_ocr(composite_plate_sources, tmp_path, monkeypatch):
    cli = importlib.import_module("scripts.analyze_composite_plate")
    captured = []

    def fake_analysis(sources, settings, **kwargs):
        captured.extend(sources)
        assert len(settings) == 4
        assert kwargs["search_mode"] == "zone-merge"
        return {"status": "full_coverage_candidates_found", "front": [{
            "additional_mass_kg": 1.0, "zone_count": 4, "position_count": 4,
            "physical_bar_count": 4, "stock_cutting": {"status": "not_checked"}}]}

    monkeypatch.setattr(cli, "analyze_composite_plate", fake_analysis)
    arguments = []
    for index, (source, direction) in enumerate(zip(composite_plate_sources, PLATE_DIRECTIONS)):
        key = f"{direction.layer.value}-{direction.axis.value.lower()}"
        scale = source.shk_path if index % 2 else tmp_path / f"scale-{index}.png"
        if scale.suffix == ".png":
            scale.write_bytes(b"PNG placeholder: parser deliberately stubbed")
        arguments.extend(("--dxf-" + key, str(source.dxf_path),
                          ("--shk-" if index % 2 else "--scale-") + key, str(scale),
                          "--origin-" + key, "0"))
    output = tmp_path / "result.json"
    arguments.extend(("--first-300-offset", "150", "--second-offset", "50", "--steel-class", "A500",
                      "--phase-source", "Explicit CLI input", "--search-mode", "zone-merge", "--output", str(output)))
    assert cli.main(arguments) == 0
    assert output.exists()
    assert [(source.png_path is not None, source.shk_path is not None) for source in captured] == [
        (True, False), (False, True), (True, False), (False, True)]
