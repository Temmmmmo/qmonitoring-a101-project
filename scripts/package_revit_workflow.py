"""Deterministic code-only TZ 8.1 workflow; no RFA/RVT/DXF or private packets."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from package_revit_plan_preview import CODE_ONLY_MODULES
from package_revit_probe import SOURCE

BUTTON = "QMonitoring.extension/QMonitoring.tab/Workflow.panel/SourceWorkflow.pushbutton"
MODULES = (*CODE_ONLY_MODULES, "qm_workflow_81.py", "qm_workflow_81_native.py", "qm_workflow_81_transport.py")


def build_package(output):
    sys.path.insert(0, str(SOURCE/"QMonitoring.extension/lib"))
    from qm_workflow_81 import VERSION
    contents = {}
    for name in MODULES:
        contents["QMonitoringWorkflow.extension/lib/"+name] = (SOURCE/"QMonitoring.extension/lib"/name).read_bytes()
    for name in ("script.py", "bundle.yaml"):
        contents["QMonitoringWorkflow.extension/QMonitoringWorkflow.tab/Workflow.panel/SourceWorkflow.pushbutton/"+name] = (SOURCE/BUTTON/name).read_bytes()
    contents["WORKFLOW_81_README.md"] = (SOURCE/"WORKFLOW_81_README.md").read_bytes()
    manifest = {"schema_version": "qmonitoring-workflow-code-package/v1", "version": VERSION,
        "code_only": True, "placement_eligible": False, "engineering_approval": False,
        "native_live_test": "not_verified", "mode": "source-parametric-view-families-not-structural-rebar",
        "files": [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in sorted(contents.items())]}
    contents["manifest.json"] = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode()
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
