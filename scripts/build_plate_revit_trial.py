"""Export every zone of a chosen full-plate result for a rollback-only Revit run."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from rebar.application.plate_revit_trial import build_full_plate_trial

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=int, help="Explicit zero-based full front index")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with args.analysis.open("rb") as stream:
        content = stream.read(64 * 1024 * 1024 + 1)
    if len(content) > 64 * 1024 * 1024:
        parser.error("Analysis exceeds 64 MiB")
    sys.path.insert(0, str(ROOT / "integrations/pyrevit/QMonitoring.extension/lib"))
    from qm_plate_packet import validate_packet
    from qm_trial_input import _reject_constant, _unique_object
    report = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    packet = validate_packet(build_full_plate_trial(report, args.candidate, hashlib.sha256(content).hexdigest()))
    if args.output.suffix.lower() != ".json":
        parser.error("Output must be a new JSON file")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(packet, stream, ensure_ascii=False, allow_nan=False, indent=2)
    print(json.dumps({"output": str(args.output), **packet["expected"], "placement_eligible": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
