"""Compare a read-only CAD Probe report with its source DXF. Never changes input files."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rebar.application.revit_dxf_calibration import verify_dxf_calibration  # noqa: E402
from rebar.dxf_ingest import read_mosaic  # noqa: E402


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def _reject(value):
    raise ValueError("Non-finite JSON value: " + value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dxf", type=Path, required=True)
    parser.add_argument("--shk", type=Path, required=True, help="Explicit legend for this DXF; no automatic choice")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="New JSON only; stdout if omitted")
    args = parser.parse_args()
    if args.output and (args.output.suffix.lower() != ".json" or args.output.exists()):
        parser.error("--output must be a NEW .json file")
    if args.dxf.stat().st_size > 64 * 1024 * 1024 or args.report.stat().st_size > 32 * 1024 * 1024:
        parser.error("DXF exceeds 64 MiB or report exceeds 32 MiB")
    with args.report.open("rb") as stream:
        raw_report = stream.read(32 * 1024 * 1024 + 1)
    if len(raw_report) > 32 * 1024 * 1024:
        parser.error("Report exceeds 32 MiB")
    report = json.loads(raw_report.decode("utf-8-sig"), object_pairs_hook=_unique, parse_constant=_reject)
    digest = hashlib.sha256(args.dxf.read_bytes()).hexdigest()
    mosaic = read_mosaic(str(args.dxf), str(args.shk))
    if hashlib.sha256(args.dxf.read_bytes()).hexdigest() != digest:
        parser.error("Source DXF changed during analysis")
    result = verify_dxf_calibration(mosaic, report, source_sha256=digest)
    result["source_report_sha256"] = hashlib.sha256(raw_report).hexdigest()
    content = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(content)
        print(args.output)
        print(result["status"])
    else:
        print(content, end="")
    return 0 if result["status"] == "geometry_matches" else 1


if __name__ == "__main__":
    raise SystemExit(main())
