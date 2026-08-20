"""Генерация переносимого отчёта для сравнения оптимизаторов."""

from __future__ import annotations

import html
import json
from pathlib import Path

from rebar.models import Mosaic
from rebar.optimization import (
    AlgorithmRequest,
    LayoutConstraints,
    LayoutProblem,
    LayoutSolution,
    ObjectiveWeights,
    PLATE_11700_CATALOG,
    build_layout_problem,
    built_in_optimizer_registry,
)

from .serialization import to_jsonable
from .svg import render_solution_svg

PAGE_STYLE = """
  :root { font-family: Inter, system-ui, sans-serif; color: #17212b; background: #eef2f5; }
  * { box-sizing: border-box; } body { margin: 0; }
  header { padding: 24px max(22px, calc((100vw - 1500px)/2)); background: #14283a;
           color: white; }
  h1 { margin: 0 0 8px; font-size: 25px; } header p { margin: 4px 0; color: #c5d6e3; }
  main { max-width: 1500px; margin: 24px auto; padding: 0 20px 40px; }
  .notice { padding: 14px 16px; margin-bottom: 20px; border-left: 5px solid #d79400;
            background: #fff7dc; border-radius: 7px; }
  .card { background: white; margin: 0 0 24px; padding: 20px; border-radius: 12px;
          box-shadow: 0 3px 15px #2342; }
  .card-head { display: flex; flex-wrap: wrap; gap: 14px; align-items: center; }
  .card-head > div:first-child { margin-right: auto; } h2 { margin: 0; }
  .card-head p { margin: 4px 0 0; color: #607080; }
  .metric { min-width: 100px; padding: 9px 12px; background: #edf7f3; border-radius: 8px; }
  .metric strong, .metric span { display: block; } .metric strong { font-size: 20px; }
  .metric span { color: #5d6c75; font-size: 12px; } .metric.bad { background: #fbe1e1; }
  .drawing { margin: 18px 0; height: min(62vh, 700px); background: #f9fafb;
             border: 1px solid #dce2e7; }
  svg { display: block; width: 100%; height: 100%; }
  details { border-top: 1px solid #e1e5e8; padding: 12px 0 0; margin-top: 12px; }
  summary { cursor: pointer; font-weight: 650; } .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; }
  th, td { padding: 7px 9px; border-bottom: 1px solid #e3e7ea; text-align: left; }
  ul { margin-bottom: 0; }
"""


def _solution_card(problem: LayoutProblem, solution: LayoutSolution) -> str:
    metrics = solution.metrics
    diagnostics = (
        "".join(f"<li>{html.escape(message)}</li>" for message in solution.diagnostics)
        or "<li>Общая проверка не нашла нарушений в текущей MVP-модели.</li>"
    )
    zones = "".join(
        "<tr>"
        f"<td>{html.escape(zone.id)}</td><td>{zone.level_index}</td>"
        f"<td>⌀{zone.rebar.diameter} / {zone.rebar.step}</td>"
        f"<td>{zone.width_mm:.0f}</td><td>{zone.anchored_length_mm:.0f}</td>"
        f"<td>{zone.installed_length_mm:.0f}</td>"
        f"<td>{zone.bar_count}</td><td>{zone.mass_kg:.1f}</td>"
        "</tr>"
        for zone in solution.zones
    )
    return f"""
    <article class="card">
      <div class="card-head">
        <div><h2>{html.escape(solution.algorithm)}</h2>
          <p>{html.escape(solution.status.value)} · {solution.runtime_ms:.1f} мс</p></div>
        <div class="metric"><strong>{metrics.total_mass_kg:.1f}</strong><span>кг</span></div>
        <div class="metric"><strong>{metrics.detail_count}</strong><span>прямоугольных зон</span></div>
        <div class="metric"><strong>{metrics.physical_bar_count}</strong><span>стержней</span></div>
        <div class="metric"><strong>{metrics.overcovered_cell_count}</strong><span>лишних КЭ</span></div>
        <div class="metric {"bad" if metrics.under_reinforced_cell_count else ""}">
          <strong>{metrics.under_reinforced_cell_count}</strong><span>недоарм. КЭ</span></div>
      </div>
      <div class="drawing">{render_solution_svg(problem, solution)}</div>
      <details><summary>Детали раскладки</summary>
        <div class="table-wrap"><table><thead><tr><th>ID</th><th>Уровень</th>
        <th>⌀ / шаг</th><th>Ширина, мм</th><th>40d минимум, мм</th>
        <th>Отрезок, мм</th><th>Стержней</th>
        <th>Масса, кг</th></tr></thead><tbody>{zones}</tbody></table></div>
      </details>
      <details><summary>Диагностика общей проверки</summary><ul>{diagnostics}</ul></details>
    </article>
    """


def generate_comparison_report(
    mosaic: Mosaic,
    out_dir: Path,
    algorithm_names: tuple[str, ...] = (
        "spatial-partition-greedy",
        "bbox",
        "bsp",
        "greedy",
        "greedy-priority",
        "agglomerative",
        "row-run-greedy",
        "strip-profile-dp",
    ),
    *,
    max_details: int = 32,
    detail_penalty_kg: float = 0.0,
    min_width_cells: int = 2,
    allow_overlaps: bool = True,
    cutting_profile: str = "continuous",
) -> Path:
    """Выполнить алгоритмы и сохранить переносимый ``index.html`` + JSON."""

    normalized_cutting_profile = cutting_profile.strip().casefold()
    if normalized_cutting_profile == "continuous":
        allowed_cut_lengths: tuple[float, ...] = ()
    elif normalized_cutting_profile == PLATE_11700_CATALOG.id:
        allowed_cut_lengths = PLATE_11700_CATALOG.lengths_mm
    else:
        raise ValueError(f"неизвестный профиль раскроя: {cutting_profile!r}")
    constraints = LayoutConstraints(
        min_width_cells=min_width_cells,
        allow_overlaps=allow_overlaps,
        allowed_cut_lengths_mm=allowed_cut_lengths,
        cutting_profile=normalized_cutting_profile,
    )
    problem = build_layout_problem(mosaic, constraints)
    request = AlgorithmRequest(
        objective=ObjectiveWeights(detail_penalty_kg=detail_penalty_kg),
        max_details=max_details,
    )
    registry = built_in_optimizer_registry()
    solutions = tuple(registry.create(name).solve(problem, request) for name in algorithm_names)

    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "source_path": mosaic.source_path,
        "case_id": problem.case_id,
        "units": "mm",
        "constraints": to_jsonable(problem.constraints),
        "solutions": [to_jsonable(solution) for solution in solutions],
    }
    (out_dir / "solutions.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    cards = "".join(_solution_card(problem, solution) for solution in solutions)
    source = html.escape(mosaic.source_path or "синтетический пример")
    overlap_mode = "разрешены" if allow_overlaps else "запрещены строгим профилем"
    page = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Сравнение алгоритмов раскладки</title><style>{PAGE_STYLE}</style></head><body>
<header><h1>Локальное сравнение алгоритмов</h1><p>{source}</p>
<p>Миллиметры · максимум зон: {max_details} · overlap demand_bbox: {overlap_mode} ·
JSON: solutions.json</p></header>
<main><div class="notice"><strong>Рабочая инженерная модель.</strong> Пересечения зон
разрешены, но вклад слабых зон не суммируется: каждый КЭ должен быть полностью покрыт
хотя бы одной или объединением частей зон достаточного уровня. 40d, принятая длина,
масса, реальные оси и их раздвижка считаются общей постобработкой; её конфликты показаны
предупреждениями и не склеивают решение. Медианный размер КЭ, правило разных шагов и
контуры проёмов всё ещё требуют уточнения.</div>{cards}</main>
</body></html>"""
    report_path = out_dir / "index.html"
    report_path.write_text(page, encoding="utf-8")
    return report_path
