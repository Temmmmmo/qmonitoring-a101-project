"""Автономный HTML-отчёт по инженерному golden-case."""

from __future__ import annotations

import base64
import html
import json
from pathlib import Path

from rebar.golden import (
    GoldenCaseDefinition,
    GoldenSheetExpectation,
    GoldenSheetMetrics,
    extract_sheet_metrics,
    render_pdf_page_png,
    resolve_golden_case_files,
    validate_sheet_metrics,
)

from .serialization import to_jsonable

PAGE_STYLE = """
  :root { font-family: Inter, ui-sans-serif, system-ui, sans-serif; color: #18222c;
          background: #e9eef2; }
  * { box-sizing: border-box; } body { margin: 0; }
  header { color: white; padding: 30px max(22px, calc((100vw - 1560px) / 2));
           background: linear-gradient(120deg, #102c42, #1d5063); }
  h1 { margin: 0 0 8px; font-size: clamp(25px, 3vw, 38px); }
  header p { max-width: 950px; margin: 6px 0; color: #d5e5eb; }
  main { max-width: 1560px; margin: 24px auto; padding: 0 20px 50px; }
  .summary, .sheet { background: white; border-radius: 14px; box-shadow: 0 4px 18px #20304018; }
  .summary { padding: 20px; margin-bottom: 24px; }
  .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
             gap: 12px; margin: 16px 0; }
  .metric { padding: 14px; border-radius: 10px; background: #eef7f4; }
  .metric strong, .metric span { display: block; }
  .metric strong { font-size: 26px; color: #153c45; }
  .metric span { margin-top: 2px; font-size: 12px; color: #65747e; }
  .notice { border-left: 5px solid #d99500; background: #fff5d8; padding: 13px 15px;
            border-radius: 8px; margin-top: 14px; }
  .sheet { padding: 20px; margin-bottom: 26px; }
  .sheet-head { display: flex; flex-wrap: wrap; align-items: start; gap: 12px; }
  .sheet-head > div:first-child { margin-right: auto; }
  h2 { margin: 0; font-size: 23px; } .subtitle { color: #64737e; margin: 5px 0 0; }
  .sheet-metric { min-width: 105px; background: #edf3f8; border-radius: 9px; padding: 9px 11px; }
  .sheet-metric strong, .sheet-metric span { display: block; }
  .sheet-metric strong { font-size: 19px; } .sheet-metric span { font-size: 11px; color: #62727d; }
  .visuals { display: grid; grid-template-columns: minmax(300px, .8fr) minmax(480px, 1.2fr);
             gap: 16px; margin-top: 18px; align-items: start; }
  figure { margin: 0; border: 1px solid #dbe2e7; border-radius: 10px; overflow: hidden;
           background: #f5f7f8; }
  figcaption { padding: 10px 12px; background: #eaf0f3; font-weight: 700; }
  .image-button { width: 100%; display: block; padding: 0; border: 0; cursor: zoom-in;
                  background: white; }
  .image-button img { display: block; width: 100%; height: auto; max-height: 76vh;
                      object-fit: contain; }
  details { margin-top: 14px; border-top: 1px solid #e0e5e8; padding-top: 12px; }
  summary { cursor: pointer; font-weight: 700; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
  th, td { padding: 7px 9px; border-bottom: 1px solid #e1e6e9; text-align: right; }
  th:first-child, td:first-child { text-align: left; }
  dialog { width: min(96vw, 1700px); max-height: 96vh; border: 0; border-radius: 12px;
           padding: 12px; box-shadow: 0 15px 60px #0008; }
  dialog::backdrop { background: #07131dcc; }
  dialog img { width: 100%; max-height: 87vh; object-fit: contain; display: block; }
  .dialog-head { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
  .dialog-head strong { margin-right: auto; } .dialog-head button { cursor: pointer; }
  @media (max-width: 900px) { .visuals { grid-template-columns: 1fr; } }
"""


def _data_uri(data: bytes, media_type: str = "image/png") -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _specification_table(metrics: GoldenSheetMetrics) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(position.position)}</td>"
        f"<td>⌀{position.diameter_mm}</td>"
        f"<td>{position.length_mm}</td>"
        f"<td>{position.quantity}</td>"
        f"<td>{position.unit_mass_kg:.2f}</td>"
        f"<td>{position.total_mass_kg:.2f}</td>"
        "</tr>"
        for position in metrics.positions
    )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Поз.</th><th>Диаметр</th>'
        '<th>Длина, мм</th><th>Кол-во, шт.</th><th>Масса ед., кг</th>'
        f'<th>Масса, кг</th></tr></thead><tbody>{rows}</tbody></table></div>'
    )


def _sheet_card(
    expectation: GoldenSheetExpectation,
    metrics: GoldenSheetMetrics,
    input_png: bytes,
    engineer_png: bytes,
) -> str:
    direction = html.escape(str(expectation.direction))
    input_uri = _data_uri(input_png)
    engineer_uri = _data_uri(engineer_png)
    title = html.escape(expectation.title)
    engineer_axis = html.escape(expectation.engineer_axis_label)
    return f"""
    <article class="sheet" data-testid="golden-sheet-{direction}">
      <div class="sheet-head">
        <div><h2>{title}</h2><p class="subtitle">{direction} · инженер: {engineer_axis} ·
          лист PDF {expectation.pdf_page}</p></div>
        <div class="sheet-metric"><strong>{metrics.position_count}</strong>
          <span>позиций</span></div>
        <div class="sheet-metric"><strong>{metrics.bar_count}</strong>
          <span>стержней</span></div>
        <div class="sheet-metric"><strong>{metrics.total_mass_kg:.2f}</strong>
          <span>кг</span></div>
      </div>
      <div class="visuals">
        <figure><figcaption>Входное изополе</figcaption>
          <button class="image-button" data-zoom="{title} · входное изополе">
            <img src="{input_uri}" alt="{title}: входная цветовая мозаика"></button>
        </figure>
        <figure><figcaption>Ручная раскладка инженера</figcaption>
          <button class="image-button" data-zoom="{title} · решение инженера">
            <img src="{engineer_uri}" alt="{title}: инженерная раскладка"></button>
        </figure>
      </div>
      <details><summary>Спецификация инженера</summary>{_specification_table(metrics)}</details>
    </article>
    """


def generate_golden_report(
    case: GoldenCaseDefinition,
    data_dir: Path,
    out_dir: Path,
    *,
    render_dpi: int = 120,
) -> Path:
    """Проверить PDF-эталон и собрать автономные ``index.html`` + ``golden.json``."""

    if render_dpi <= 0:
        raise ValueError("render_dpi должен быть положительным")
    files = resolve_golden_case_files(case, data_dir)
    cards: list[str] = []
    extracted: list[tuple[GoldenSheetExpectation, GoldenSheetMetrics]] = []
    for expectation in case.sheets:
        metrics = extract_sheet_metrics(files.engineer_pdf, expectation.pdf_page)
        validate_sheet_metrics(expectation, metrics)
        extracted.append((expectation, metrics))
        cards.append(
            _sheet_card(
                expectation,
                metrics,
                files.input_png_by_direction[expectation.direction].read_bytes(),
                render_pdf_page_png(
                    files.engineer_pdf,
                    expectation.pdf_page,
                    dpi=render_dpi,
                ),
            )
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "case_id": case.id,
        "title": case.title,
        "source_pdf": files.engineer_pdf.name,
        "totals": {
            "position_count": sum(metrics.position_count for _, metrics in extracted),
            "bar_count": sum(metrics.bar_count for _, metrics in extracted),
            "total_mass_kg": round(
                sum(metrics.total_mass_kg for _, metrics in extracted), 2
            ),
        },
        "directions": [
            {
                "direction": str(expectation.direction),
                "title": expectation.title,
                "engineer_axis_label": expectation.engineer_axis_label,
                "pdf_page": expectation.pdf_page,
                "input_png": files.input_png_by_direction[expectation.direction].name,
                "metrics": {
                    "position_count": metrics.position_count,
                    "bar_count": metrics.bar_count,
                    "total_mass_kg": metrics.total_mass_kg,
                },
                "positions": to_jsonable(metrics.positions),
            }
            for expectation, metrics in extracted
        ],
        "notes": list(case.notes),
    }
    (out_dir / "golden.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    notes = "".join(f"<li>{html.escape(note)}</li>" for note in case.notes)
    cards_html = "".join(cards)
    page = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width, initial-scale=1"><title>{html.escape(case.title)} · golden-case</title>
<style>{PAGE_STYLE}</style></head><body>
<header><h1>Golden-case: {html.escape(case.title)}</h1>
<p>Слева показаны исходные изополя ЛИРА, справа — соответствующие листы ручной
раскладки. Все изображения встроены в HTML; внешние файлы для просмотра не нужны.</p></header>
<main><section class="summary"><h2>Контрольные итоги инженера</h2>
<div class="metrics"><div class="metric"><strong>{case.expected_position_count}</strong>
<span>позиций в 4 спецификациях</span></div>
<div class="metric"><strong>{case.expected_bar_count}</strong><span>физических стержней</span></div>
<div class="metric"><strong>{case.expected_mass_kg:.2f}</strong><span>кг дополнительной стали</span></div>
<div class="metric"><strong>4</strong><span>направления плиты</span></div></div>
<div class="notice"><strong>Что именно проверено.</strong> Числа заново извлечены из
таблиц PDF и сравнены с зафиксированным эталоном до сборки отчёта. Количество
прямоугольных зон пока нельзя восстановить из PDF надёжно: для геометрической метрики
нужен DWG→DXF либо выгрузка таблицы зон.</div><details><summary>Допущения и ограничения</summary>
<ul>{notes}</ul></details></section>{cards_html}</main>
<dialog id="zoom"><div class="dialog-head"><strong></strong><button type="button">Закрыть</button>
</div><img alt="Увеличенное изображение"></dialog>
<script>
const dialog = document.getElementById('zoom');
const zoomImage = dialog.querySelector('img');
const zoomTitle = dialog.querySelector('strong');
document.querySelectorAll('[data-zoom]').forEach((button) => {{
  button.addEventListener('click', () => {{
    zoomImage.src = button.querySelector('img').src;
    zoomTitle.textContent = button.dataset.zoom;
    dialog.showModal();
  }});
}});
dialog.querySelector('button').addEventListener('click', () => dialog.close());
dialog.addEventListener('click', (event) => {{ if (event.target === dialog) dialog.close(); }});
</script></body></html>"""
    report_path = out_dir / "index.html"
    report_path.write_text(page, encoding="utf-8")
    return report_path

