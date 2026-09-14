"""Standalone read-only native Floor reinforcement probe; no model/data payloads."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "integrations/pyrevit"
EXTENSION = "QMonitoring.extension"
BUTTON = f"{EXTENSION}/QMonitoring.tab/Diagnostics.panel/WorkingRebarProbe.pushbutton"
FILES = (
    f"{EXTENSION}/lib/qm_probe_geometry.py",
    f"{EXTENSION}/lib/qm_revit_probe.py",
    f"{EXTENSION}/lib/qm_working_rebar_probe.py",
    f"{BUTTON}/script.py",
    f"{BUTTON}/bundle.yaml",
)
ARCHIVE_FILES = tuple(name.replace("QMonitoring.extension/", "QMonitoringReadOnly.extension/", 1)
    .replace("/QMonitoring.tab/", "/QMonitoringReadOnly.tab/", 1) for name in FILES)


def build_package(output: Path) -> Path:
    contents = [(archive_name, (SOURCE / source_name).read_bytes())
        for source_name, archive_name in zip(FILES, ARCHIVE_FILES, strict=True)]
    contents.append(("README.md", (SOURCE / "WORKING_REBAR_PROBE_README.md").read_bytes()))
    if len({name for name, _ in contents}) != len(contents):
        raise ValueError("Duplicate package entry")
    manifest = {"schema_version": "working-rebar-probe-package/v1", "version": "0.1.0",
        "read_only": True, "placement_eligible": False,
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in contents}}
    contents.append(("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2,
        sort_keys=True).encode("utf-8")))
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in contents:
            archive.writestr(name, content)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT /
        "artifacts/revit_working_rebar_2026_09_14/qmonitoring-working-rebar-probe-0.1.0.zip")
    print(build_package(parser.parse_args().output))


if __name__ == "__main__":
    main()
