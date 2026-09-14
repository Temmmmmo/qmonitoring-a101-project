import json
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
