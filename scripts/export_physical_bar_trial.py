"""Verify a normalized physical plan against original DXF demand and export a rollback trial.

This is a checked export of an explicit research result, not another optimizer and
not a permanent-placement authorization. The source LayoutZones remain intact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rebar.application.layout_snapshot import (
    _reject_constant, _source_record, _unique_object, load_layout_snapshot,
)

MAX_INPUT_BYTES = 32 * 1024 * 1024


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite JSON number")
    return result


def _read_json(path: Path) -> tuple[dict, str]:
    if path.suffix.lower() != ".json" or not path.is_file():
        raise ValueError("Expected an existing local JSON file")
    with path.open("rb") as stream:
        content = stream.read(MAX_INPUT_BYTES + 1)
    if not content or len(content) > MAX_INPUT_BYTES:
        raise ValueError("Empty or oversized physical-plan input")
    result = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                        parse_constant=_reject_constant, parse_float=_finite_float)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object")
    return result, hashlib.sha256(content).hexdigest()


def _code_digest() -> str:
    digest = hashlib.sha256()
    paths = [*sorted((ROOT / "src").rglob("*.py")),
             *sorted((ROOT / "integrations/pyrevit/QMonitoring.extension/lib").glob("*.py")),
             Path(__file__).resolve()]
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def export_physical_trial(source_report_path: Path, normalized_report_path: Path,
                          output_dir: Path, *, stock_time_limit_s: float = 10.0) -> dict:
    """No overwrite, no implicit optimization and no trust in artifact check flags."""
    from rebar.application.physical_bar_trial import build_physical_bar_trial

    if output_dir.exists():
        raise ValueError("Output directory already exists; choose a new path")
    code_before = _code_digest()
    source, source_hash = _read_json(source_report_path)
    normalized, raw_hash = _read_json(normalized_report_path)
    if (normalized.get("placement_eligible") is not False
            or normalized.get("engineering_approval") is not False
            or normalized.get("source_demand_removed") is not False):
        raise ValueError("Expected an explicitly unapproved research plan preserving source demand")
    raw_bars = normalized["accepted"]["raw_bars_by_direction"]
    snapshot = source["source_snapshot"]
    loaded = load_layout_snapshot(snapshot["path"], candidate_id=snapshot["candidate_id"])
    if (loaded.source_sha256 != snapshot["sha256"]
            or loaded.problem.case_id != source["case_id"]
            or loaded.snapshot["candidate_id"] != snapshot["candidate_id"]
            or loaded.snapshot["source_dxf"] != snapshot["source_dxf"]
            or loaded.snapshot["source_pdf"] != snapshot["source_pdf"]):
        raise ValueError("Patterned source report is not bound to the freshly validated original snapshot")
    result = build_physical_bar_trial(source, raw_bars, original_problem=loaded.problem,
        source_report_sha256=source_hash, raw_report_sha256=raw_hash,
        stock_time_limit_s=stock_time_limit_s)
    packet, review = result.packet, result.review
    sys.path.insert(0, str(ROOT / "integrations/pyrevit/QMonitoring.extension/lib"))
    from qm_physical_packet import VERSION, validate_packet
    from qm_revit_probe import serialize_report_utf8

    validate_packet(packet)
    packet_content = serialize_report_utf8(packet)
    packet_hash = hashlib.sha256(packet_content).hexdigest()
    if review["packet_sha256"] != packet_hash:
        raise ValueError("Builder review hash differs from the actual serialized packet")
    engineer, expected = loaded.engineer_comparison, packet["expected"]
    review["engineer_comparison"] = {
        "mass_kg": engineer["mass_kg"], "physical_bar_count": engineer["physical_bar_count"],
        "specification_rows": engineer["specification_rows"],
        "mass_delta_pct": (expected["additional_mass_kg"] / engineer["mass_kg"] - 1) * 100,
        "bar_delta_pct": (expected["physical_bar_count"] / engineer["physical_bar_count"] - 1) * 100,
        "mass_threshold_15pct_met": expected["additional_mass_kg"] <= 1.15 * engineer["mass_kg"],
        "scope": "matched additional reinforcement; physical typologies are not PDF rows; not all gates",
    }
    review["source_snapshot"] = loaded.snapshot
    for record in loaded.snapshot["source_dxf"]:
        _source_record(record, ".dxf")
    _source_record(loaded.snapshot["source_pdf"], ".pdf")
    _source_record({"path": loaded.snapshot["path"], "sha256": loaded.source_sha256}, ".json")
    if (_read_json(source_report_path)[1] != source_hash
            or _read_json(normalized_report_path)[1] != raw_hash):
        raise ValueError("Input reports changed during physical-plan export")
    code_after = _code_digest()
    if code_before != code_after:
        raise ValueError("Core code changed during export; rerun on a stable working tree")
    review["code_sha256"] = code_before
    review["code_sha256_after"] = code_after
    # The same README generator is used for the standalone files and the ZIP.
    from package_revit_physical_trial import _readme, _validate_review

    review_content = serialize_report_utf8(review)
    _validate_review(review_content, packet, packet_content)
    files = {"physical-bar-plan-trial.json": packet_content,
             "engineer-review.json": review_content, "README.md": _readme(packet, VERSION)}
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        with (output_dir / name).open("xb") as stream:
            stream.write(content)
    return {"output_dir": str(output_dir), **expected,
            "same_plane_intersection_pairs": len(packet["manual_joint_tasks"]),
            "engineer_comparison": review["engineer_comparison"],
            "packet_sha256": packet_hash, "placement_eligible": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--normalized-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--stock-time-limit-s", type=float, default=10)
    args = parser.parse_args()
    try:
        result = export_physical_trial(args.source_report, args.normalized_report,
            args.output_dir, stock_time_limit_s=args.stock_time_limit_s)
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
