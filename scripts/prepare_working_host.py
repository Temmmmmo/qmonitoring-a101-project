"""Prepare a fully checked rectangular host from the specialist's Full Plate Trial JSON."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from rebar.application.working_host import host_from_working_input, load_working_host_json, prepare_working_host
from rebar.reporting.serialization import to_jsonable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--offset-x-mm", type=float, required=True, help="Revit internal X = source X + offset")
    parser.add_argument("--offset-y-mm", type=float, required=True, help="Revit internal Y = source Y + offset")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        with args.report.open("rb") as stream:
            content = stream.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            raise ValueError("Отчёт превышает 8 MiB")
        result = prepare_working_host(load_working_host_json(content), offset_x_mm=args.offset_x_mm,
            offset_y_mm=args.offset_y_mm, source_report_sha256=hashlib.sha256(content).hexdigest())
        host = host_from_working_input(result)
        if args.output.suffix.lower() != ".json":
            raise ValueError("Выход должен быть новым JSON")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(to_jsonable(host), ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
