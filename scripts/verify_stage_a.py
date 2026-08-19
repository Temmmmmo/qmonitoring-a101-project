#!/usr/bin/env python3
"""Создать просматриваемый HTML-отчёт проверки DXF ingest на реальных данных."""

from __future__ import annotations

import argparse
import html
import json
import sys
import unicodedata
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ezdxf.colors import aci2rgb
from PIL import Image

from rebar import Axis, Direction, Layer
from rebar.dxf_ingest import read_mosaic
from rebar.render import render_mosaic


def _normalised(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _find_named_dir(parent: Path, name: str) -> Path | None:
    expected = _normalised(name)
    for candidate in parent.iterdir():
        if candidate.is_dir() and _normalised(candidate.name) == expected:
            return candidate
    return None


def _png_direction(path: Path) -> Direction | None:
    text = unicodedata.normalize("NFKC", path.stem).casefold()
    bottom = "нижн" in text or "низ" in text
    top = "верх" in text
    if bottom == top:
        return None

    translated = text.replace("х", "x").replace("у", "y")
    axis: Axis | None = None
    if "оси_x" in translated or "по_оси_x" in translated or path.stem.startswith(("X", "Х")):
        axis = Axis.X
    elif "оси_y" in translated or "по_оси_y" in translated or path.stem.startswith(("Y", "У")):
        axis = Axis.Y
    if axis is None:
        return None
    return Direction(Layer.BOTTOM if bottom else Layer.TOP, axis)


def _matching_png(dxf_path: Path, direction: Direction) -> Path | None:
    exact = dxf_path.with_suffix(".png")
    if exact.exists():
        return exact
    for candidate in sorted(dxf_path.parent.glob("*.png")):
        if _png_direction(candidate) == direction:
            return candidate
    return None


def _discover_dxf(data_dir: Path) -> list[Path]:
    paths = sorted(data_dir.glob("*.dxf"))
    verify2 = _find_named_dir(data_dir, "Для верификации изополей 2")
    if verify2 is not None:
        paths.extend(sorted(verify2.rglob("*.dxf")))
    return paths


def _slug(path: Path, data_dir: Path, index: int) -> str:
    relative = path.relative_to(data_dir)
    readable = "__".join(relative.with_suffix("").parts)
    safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in readable)
    return f"{index:02d}_{safe[:150]}"


def _swatches(colors: Counter[int]) -> str:
    parts: list[str] = []
    for aci, count in colors.most_common():
        try:
            rgb = tuple(aci2rgb(aci))
        except (IndexError, ValueError):
            rgb = (0, 0, 0)
        parts.append(
            '<span class="swatch" '
            f'style="--color:rgb{rgb}" title="ACI {aci}">ACI {aci}: {count}</span>'
        )
    return "".join(parts)


def _make_source_preview(source: Path, destination: Path) -> None:
    """Сделать локальное браузерное превью без абсолютной file:// ссылки."""
    with Image.open(source) as image:
        image.thumbnail((1800, 1200), Image.Resampling.LANCZOS)
        image.convert("RGB").save(destination, format="JPEG", quality=88, optimize=True)


def generate_report(data_dir: Path, out_dir: Path, limit: int | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    render_dir = out_dir / "renders"
    render_dir.mkdir(exist_ok=True)
    source_dir = out_dir / "sources"
    source_dir.mkdir(exist_ok=True)

    dxf_files = _discover_dxf(data_dir)
    if limit is not None:
        dxf_files = dxf_files[:limit]
    if not dxf_files:
        raise SystemExit(f"DXF не найдены в {data_dir}")

    records: list[dict] = []
    cards: list[str] = []
    passed = 0

    for index, dxf_path in enumerate(dxf_files, 1):
        mosaic = read_mosaic(str(dxf_path))
        slug = _slug(dxf_path, data_dir, index)
        render_path = render_dir / f"{slug}.png"
        render_mosaic(mosaic, str(render_path))
        source_png = _matching_png(dxf_path, mosaic.direction)
        source_preview: Path | None = None
        if source_png is not None:
            source_preview = source_dir / f"{slug}.jpg"
            _make_source_preview(source_png, source_preview)
        colors = Counter(cell.aci for cell in mosaic.cells)

        checks = {
            "cells_nonempty": bool(mosaic.cells),
            "plast_removed": mosaic.meta["ignored_plast_count"] in (0, len(mosaic.cells)),
            "raw_faces_accounted": mosaic.meta["raw_3dface_count"]
            == len(mosaic.cells) + mosaic.meta["ignored_plast_count"],
            "scale_intervals_complete": bool(mosaic.meta["scale_intervals"]),
            "coordinates_are_mm": mosaic.meta["units"] == "mm",
            "polygons_are_tri_or_quad": all(len(cell.poly) in (3, 4) for cell in mosaic.cells),
        }
        ok = all(checks.values())
        passed += int(ok)
        xmin, ymin, xmax, ymax = mosaic.bbox

        record = {
            "dxf": str(dxf_path),
            "source_png": str(source_png) if source_png else None,
            "source_preview": str(source_preview) if source_preview else None,
            "render": str(render_path),
            "direction": str(mosaic.direction),
            "cells": len(mosaic.cells),
            "bbox_mm": mosaic.bbox,
            "size_mm": (xmax - xmin, ymax - ymin),
            "colors": dict(colors),
            "checks": checks,
            "meta": mosaic.meta,
        }
        records.append(record)

        source_image = (
            f'<img src="{html.escape(source_preview.relative_to(out_dir).as_posix())}" '
            'alt="Исходный PNG">'
            if source_preview
            else '<div class="missing">Соответствующий исходный PNG не найден</div>'
        )
        render_src = render_path.relative_to(out_dir).as_posix()
        cards.append(
            f"""
            <article class="card {'pass' if ok else 'fail'}">
              <h2>{index}. {html.escape(str(dxf_path.relative_to(data_dir)))}</h2>
              <div class="status">{'PASS' if ok else 'FAIL'} · {mosaic.direction}</div>
              <dl>
                <dt>КЭ</dt><dd>{len(mosaic.cells)}</dd>
                <dt>Треугольники / квады</dt>
                <dd>{mosaic.meta['triangle_count']} / {mosaic.meta['quad_count']}</dd>
                <dt>BBox, мм</dt><dd>{xmin:.1f}, {ymin:.1f} — {xmax:.1f}, {ymax:.1f}</dd>
                <dt>Исходные единицы</dt>
                <dd>{html.escape(mosaic.meta['source_units'])}, ×{mosaic.meta['unit_scale_to_mm']}
                    ({mosaic.meta['unit_detection']})</dd>
                <dt>Шкала As</dt><dd>{html.escape(str(mosaic.meta['scale_bounds_as']))}</dd>
              </dl>
              <div class="swatches">{_swatches(colors)}</div>
              <div class="checks">
                {''.join(f'<span class="{str(value).lower()}">{"✓" if value else "✗"} {html.escape(key)}</span>' for key, value in checks.items())}
              </div>
              <div class="comparison">
                <figure><figcaption>Исходный PNG организаторов</figcaption>{source_image}</figure>
                <figure><figcaption>Наш рендер из DXF</figcaption>
                  <img src="{html.escape(render_src)}" alt="Рендер DXF">
                </figure>
              </div>
            </article>
            """
        )

    (out_dir / "report.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    document = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Stage A · DXF ingest verification</title>
<style>
  :root {{ color-scheme: light; font-family: Inter, system-ui, sans-serif; }}
  body {{ margin: 0; background: #f3f5f7; color: #18202a; }}
  header {{ position: sticky; top: 0; z-index: 2; padding: 20px 28px; color: white;
            background: #172a3a; box-shadow: 0 2px 12px #0003; }}
  header h1 {{ margin: 0 0 6px; font-size: 24px; }}
  header p {{ margin: 0; color: #c9d8e4; }}
  main {{ max-width: 1500px; margin: 24px auto; padding: 0 20px; }}
  .card {{ margin: 0 0 24px; padding: 22px; background: white; border-radius: 12px;
           border-left: 7px solid #c73737; box-shadow: 0 3px 14px #24394d18; }}
  .card.pass {{ border-color: #198754; }}
  h2 {{ margin: 0 130px 14px 0; font-size: 18px; word-break: break-word; }}
  .status {{ float: right; margin-top: -40px; padding: 7px 12px; border-radius: 999px;
             background: #f8d7da; color: #842029; font-weight: 700; }}
  .pass .status {{ background: #d1e7dd; color: #0f5132; }}
  dl {{ display: grid; grid-template-columns: 190px 1fr; margin: 10px 0; font-size: 14px; }}
  dt, dd {{ margin: 0; padding: 4px 0; }} dt {{ color: #667; }}
  .swatches, .checks {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }}
  .swatch {{ border: 1px solid #ccd3da; border-left: 16px solid var(--color);
             padding: 5px 8px; border-radius: 5px; font-size: 12px; }}
  .checks span {{ padding: 5px 8px; border-radius: 5px; font-size: 12px; }}
  .checks .true {{ background: #d1e7dd; }} .checks .false {{ background: #f8d7da; }}
  .comparison {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; align-items: start; }}
  figure {{ margin: 0; min-width: 0; }} figcaption {{ margin: 8px 0; font-weight: 650; }}
  img {{ display: block; width: 100%; max-height: 720px; object-fit: contain;
         background: #eef1f3; border: 1px solid #d8dde2; }}
  .missing {{ min-height: 220px; display: grid; place-items: center; background: #fff3cd; }}
  @media (max-width: 850px) {{ .comparison {{ grid-template-columns: 1fr; }} }}
</style></head>
<body><header><h1>Stage A · DXF ingest verification</h1>
<p>{passed}/{len(records)} файлов прошли структурные проверки. Слева исходник, справа наш рендер.</p>
<p>Сверяйте ориентацию, контур, проёмы и расположение зон. Оттенки могут отличаться из-за
нестандартной PNG-палитры ЛИРА; уровни определяются порядком шкалы и интервалами As.</p>
</header><main>{''.join(cards)}</main></body></html>"""
    report_path = out_dir / "index.html"
    report_path.write_text(document, encoding="utf-8")
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", type=Path, default=REPO_ROOT / "Дополнительные материалы"
    )
    parser.add_argument(
        "--out-dir", type=Path, default=REPO_ROOT / "artifacts/stage_a_verification"
    )
    parser.add_argument("--limit", type=int, default=None, help="ограничить число DXF")
    args = parser.parse_args()
    report = generate_report(args.data_dir, args.out_dir, args.limit)
    print(f"Готово: {report}")


if __name__ == "__main__":
    main()
