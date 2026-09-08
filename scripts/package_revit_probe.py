"""Package the standalone pyRevit probe without private materials or Python dependencies."""
from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "integrations" / "pyrevit"
EXTENSION = "QMonitoring.extension"
BUTTON = f"{EXTENSION}/QMonitoring.tab/Diagnostics.panel/ReferenceProbe.pushbutton"
TRIAL_BUTTON = f"{EXTENSION}/QMonitoring.tab/Diagnostics.panel/CreationTrial.pushbutton"
JSON_BUTTON = f"{EXTENSION}/QMonitoring.tab/Diagnostics.panel/JsonTrial.pushbutton"
FILES = (
    "README.md",
    f"{EXTENSION}/lib/qm_probe_geometry.py",
    f"{EXTENSION}/lib/qm_revit_probe.py",
    f"{EXTENSION}/lib/qm_trial_geometry.py",
    f"{EXTENSION}/lib/qm_revit_trial.py",
    f"{BUTTON}/bundle.yaml",
    f"{BUTTON}/script.py",
    f"{TRIAL_BUTTON}/bundle.yaml",
    f"{TRIAL_BUTTON}/script.py",
    f"{EXTENSION}/lib/qm_trial_input.py",
    f"{JSON_BUTTON}/bundle.yaml",
    f"{JSON_BUTTON}/script.py",
    "samples/single-zone-trial.json",
)


def build_package(output: Path) -> Path:
    contents = [(name, (SOURCE / name).read_bytes()) for name in FILES]
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:
        for name, content in contents:
            archive.writestr(name, content)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "artifacts/revit_probe/qmonitoring-revit-probe-0.3.0.zip")
    args = parser.parse_args()
    print(build_package(args.output))


if __name__ == "__main__":
    main()
