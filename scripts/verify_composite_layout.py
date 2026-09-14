"""Проверить заданные составные зоны на исходном DXF/SHK, без Revit и без поиска GA."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rebar.application.composite_layout_review import MAX_INPUT_BYTES, load_review_input, review_composite_layout
from rebar.dxf_ingest import read_mosaic


def _read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if not data or len(data) > limit:
        raise ValueError(f"{path.name}: пустой файл или превышен лимит {limit} байт")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dxf", type=Path, required=True)
    parser.add_argument("--shk", type=Path, required=True)
    parser.add_argument("--layout", type=Path, required=True, help="Явные bbox/уровни/фазы, research-only JSON")
    parser.add_argument("--output", type=Path, required=True, help="Только новый .json; исходники не изменяются")
    args = parser.parse_args()
    if args.output.suffix.lower() != ".json" or args.output.exists():
        parser.error("--output должен быть новым файлом .json")
    try:
        sources = ((args.dxf, 64 * 1024 * 1024), (args.shk, 1024 * 1024), (args.layout, MAX_INPUT_BYTES))
        before = {str(path): hashlib.sha256(_read_bounded(path, limit)).hexdigest() for path, limit in sources}
        data = load_review_input(_read_bounded(args.layout, MAX_INPUT_BYTES))
        mosaic = read_mosaic(str(args.dxf), str(args.shk))
        result = review_composite_layout(mosaic, data)
        after = {str(path): hashlib.sha256(_read_bounded(path, limit)).hexdigest() for path, limit in sources}
        if before != after:
            raise ValueError("исходники изменились во время проверки")
        result["source_sha256"] = {"dxf": before[str(args.dxf)], "shk": before[str(args.shk)],
                                   "layout": before[str(args.layout)]}
        content = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(content)
    except (ValueError, KeyError, OSError, OverflowError, TypeError) as error:
        parser.error(str(error))
    print(args.output)
    print(result["status"])
    print("КЭ вне покрытия research-модели:", result["coverage"]["uncovered_cell_count"])
    print("Размещение: запрещено; инженерные проверки остаются открытыми")
    return 0 if result["status"] == "research_checks_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
