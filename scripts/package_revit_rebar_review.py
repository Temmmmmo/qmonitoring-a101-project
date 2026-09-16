"""Deterministic code-only native Rebar Review MVP; no project or input data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from package_revit_plan_preview import CODE_ONLY_MODULES
from package_revit_probe import SOURCE

BUTTON = "QMonitoring.extension/QMonitoring.tab/Review.panel/RebarReview.pushbutton"
MODULES = (*CODE_ONLY_MODULES, "qm_rebar_review.py", "qm_revit_rebar_review.py")


def build_package(output):
    sys.path.insert(0, str(SOURCE / "QMonitoring.extension/lib"))
    from qm_rebar_review import INPUT_SCHEMAS, REPORT_SCHEMA, VERSION
    contents = {}
    for name in MODULES:
        contents["QMonitoringRebarReview.extension/lib/" + name] = (
            SOURCE / "QMonitoring.extension/lib" / name).read_bytes()
    for name in ("script.py", "bundle.yaml"):
        target = "QMonitoringRebarReview.extension/QMonitoringRebarReview.tab/Review.panel/RebarReview.pushbutton/" + name
        contents[target] = (SOURCE / BUTTON / name).read_bytes()
    contents["REBAR_REVIEW_MVP_README.md"] = (SOURCE / "REBAR_REVIEW_MVP_README.md").read_bytes()
    manifest = {"schema_version": "qmonitoring-rebar-review-code-package/v1", "version": VERSION,
        "report_schema": REPORT_SCHEMA, "supported_input_schemas": list(INPUT_SCHEMAS),
        "code_only": True, "creates_structural_rebar": True, "review_only": True,
        "placement_eligible": False, "engineering_approval": False, "native_live_test": "not_verified",
        "files": [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(contents.items())]}
    contents["manifest.json"] = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(contents.items()):
            info = ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type, info.external_attr = ZIP_DEFLATED, 0o100644 << 16
            archive.writestr(info, data)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(build_package(parser.parse_args().output))
