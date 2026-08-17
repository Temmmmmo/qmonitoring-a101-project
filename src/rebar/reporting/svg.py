"""Автономная SVG-визуализация карты спроса и прямоугольных деталей."""

from __future__ import annotations

import html

from rebar.optimization import LayoutProblem, LayoutSolution

LEVEL_COLORS = (
    "#e8edf2",
    "#9ad5ca",
    "#52b8a5",
    "#ffd166",
    "#f5a65b",
    "#ef6f6c",
    "#c8558c",
    "#775da6",
    "#3949ab",
)
ZONE_COLORS = ("#0066ff", "#d7263d", "#6a4c93", "#00875a", "#b35c00")


def render_solution_svg(problem: LayoutProblem, solution: LayoutSolution) -> str:
    """Вернуть inline SVG без файловых или сетевых зависимостей."""

    all_boxes = [problem.demand.bbox, *(zone.bbox for zone in solution.zones)]
    xmin = min(box[0] for box in all_boxes)
    ymin = min(box[1] for box in all_boxes)
    xmax = max(box[2] for box in all_boxes)
    ymax = max(box[3] for box in all_boxes)
    width = max(xmax - xmin, 1.0)
    height = max(ymax - ymin, 1.0)

    cells: list[str] = []
    for cell in problem.demand.cells:
        points = " ".join(f"{x - xmin:.3f},{ymax - y:.3f}" for x, y in cell.poly)
        color = LEVEL_COLORS[cell.level_index % len(LEVEL_COLORS)]
        cells.append(
            f'<polygon points="{points}" fill="{color}" stroke="#ffffff" '
            'stroke-width="1" vector-effect="non-scaling-stroke"/>'
        )

    zones: list[str] = []
    for index, zone in enumerate(solution.zones):
        zxmin, zymin, zxmax, zymax = zone.bbox
        color = ZONE_COLORS[index % len(ZONE_COLORS)]
        zones.append(
            f'<rect x="{zxmin - xmin:.3f}" y="{ymax - zymax:.3f}" '
            f'width="{zxmax - zxmin:.3f}" height="{zymax - zymin:.3f}" '
            f'fill="{color}" fill-opacity="0.10" stroke="{color}" '
            'stroke-width="4" vector-effect="non-scaling-stroke"><title>'
            f'{html.escape(zone.id)} · уровень {zone.level_index} · '
            f'⌀{zone.rebar.diameter}/{zone.rebar.step} · {zone.mass_kg:.1f} кг'
            "</title></rect>"
        )

    return (
        f'<svg viewBox="0 0 {width:.3f} {height:.3f}" role="img" '
        f'aria-label="Раскладка {html.escape(solution.algorithm)}">'
        f'<g class="cells">{"".join(cells)}</g><g class="zones">{"".join(zones)}</g></svg>'
    )
