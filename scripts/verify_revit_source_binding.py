"""Verify selected DXF/SHK against stored Revit meshes under an explicit source-authority policy."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rebar.application.revit_source_binding import SOURCE_BINDING_POLICY, verify_source_binding  # noqa: E402
from rebar.dxf_ingest import read_mosaic  # noqa: E402

MAX_REPORT_BYTES = 32 * 1024 * 1024


def _read(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if not data or len(data) > limit:
        raise ValueError(f"{path.name}: empty file or input limit exceeded")
    return data


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def _reject(value):
    raise ValueError("Non-finite JSON constant: " + value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dxf", type=Path, required=True)
    parser.add_argument("--shk", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--policy", choices=[SOURCE_BINDING_POLICY], required=True)
    parser.add_argument("--expected-mosaic-copies", type=int, choices=[1, 2], required=True,
                        help="Explicit expected geometry multiplicity, not a count of reinforcement layers")
    parser.add_argument("--output", type=Path, help="NEW .json only; stdout if omitted")
    args = parser.parse_args()
    if args.output and (args.output.suffix.lower() != ".json" or args.output.exists()):
        parser.error("--output must be a NEW .json file")
    try:
        if (args.dxf.suffix.lower() != ".dxf" or args.shk.suffix.lower() != ".shk"
                or args.report.suffix.lower() != ".json"):
            raise ValueError("Expected DXF, SHK and JSON input files")
        sources = ((args.dxf, 64 * 1024 * 1024), (args.shk, 1024 * 1024), (args.report, MAX_REPORT_BYTES))
        raw = [_read(path, limit) for path, limit in sources]
        before = [hashlib.sha256(data).hexdigest() for data in raw]
        report = json.loads(raw[2].decode("utf-8-sig"), object_pairs_hook=_unique, parse_constant=_reject)
        if not isinstance(report, dict):
            raise ValueError("Report must be a JSON object")
        mosaic = read_mosaic(str(args.dxf), str(args.shk))
        result = verify_source_binding(mosaic, report, policy_id=args.policy,
            expected_mosaic_copies=args.expected_mosaic_copies, source_sha256=before[0],
            shk_sha256=before[1], report_sha256=before[2])
        after = [hashlib.sha256(_read(path, limit)).hexdigest() for path, limit in sources]
        if before != after:
            raise ValueError("Source inputs changed during verification")
        result["source_files"] = {key: path.name for key, (path, _) in zip(("dxf", "shk", "report"), sources)}
        result["inputs_unchanged"] = True
        content = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(content)
        else:
            print(content, end="")
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        parser.error(str(exc))
    if args.output:
        print(args.output)
        print(result["status"])
        print("Размещение запрещено; подтверждается только снимок привязки исходного DXF")
    return 0 if result["status"] == "source_snapshot_matches" else 1


if __name__ == "__main__":
    raise SystemExit(main())
