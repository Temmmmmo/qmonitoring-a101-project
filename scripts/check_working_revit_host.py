"""Read-only offline check of a real Revit slab and optionally a complete physical packet."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from rebar.application.working_host import load_working_host_json
from rebar.application.working_solid_host import (
    DIRECTIONS, MAX_WORKING_REPORT_BYTES, inspect_working_solid, review_working_solid_bars,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--packet", type=Path)
    parser.add_argument("--offset-x-mm", type=float)
    parser.add_argument("--offset-y-mm", type=float)
    parser.add_argument("--binding-source")
    parser.add_argument("--axis-depths-mm", type=float, nargs=4, metavar=("BOTTOM_X", "BOTTOM_Y", "TOP_X", "TOP_Y"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        with args.report.open("rb") as stream:
            raw = stream.read(MAX_WORKING_REPORT_BYTES + 1)
        snapshot = load_working_host_json(raw, maximum_bytes=MAX_WORKING_REPORT_BYTES)
        if args.packet:
            if None in (args.offset_x_mm, args.offset_y_mm, args.binding_source):
                raise ValueError("Для пакета нужны явные XY-сдвиги и источник привязки; значений по умолчанию нет")
            runtime = Path(__file__).resolve().parents[1] / "integrations/pyrevit/QMonitoring.extension/lib"
            sys.path.insert(0, str(runtime))
            from qm_physical_packet import validate_packet
            with args.packet.open("rb") as stream:
                packet_raw = stream.read(8 * 1024 * 1024 + 1)
            packet = validate_packet(load_working_host_json(packet_raw))
            depths = dict(zip(DIRECTIONS, args.axis_depths_mm, strict=True)) if args.axis_depths_mm else None
            result = review_working_solid_bars(snapshot, packet, offset_x_mm=args.offset_x_mm,
                offset_y_mm=args.offset_y_mm, binding_source=args.binding_source, axis_depths_mm=depths)
            result["packet_sha256"] = hashlib.sha256(packet_raw).hexdigest()
            result["bar_check"]["packet_source_certificates_checked"] = True
        else:
            if any(v is not None for v in (args.offset_x_mm, args.offset_y_mm, args.binding_source, args.axis_depths_mm)):
                raise ValueError("Параметры размещения допустимы только вместе с --packet")
            _, result = inspect_working_solid(snapshot)
        result["source_report_sha256"] = hashlib.sha256(raw).hexdigest()
        if args.output.suffix.lower() != ".json":
            raise ValueError("Выход должен быть новым JSON")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"status": result["status"], "host_id": result["host_id"],
        "sections": len(result["geometry"]["sections"]),
        "bars": result.get("bar_check", {}).get("totals"), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
