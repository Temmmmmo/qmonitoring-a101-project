"""Автономная SVG-визуализация карты спроса и прямоугольных деталей."""

from __future__ import annotations

import html

from rebar.optimization import LayoutProblem, LayoutSolution
from rebar.optimization.services import bar_segments

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

    partition_domain: list[str] = []
    partition_atoms: list[str] = []
    partition_debug = solution.meta.get("partition_debug")
    if isinstance(partition_debug, dict):
        for bbox in partition_debug.get("forbidden_tiles", ()):
            fxmin, fymin, fxmax, fymax = bbox
            partition_domain.append(
                f'<rect x="{fxmin - xmin:.3f}" y="{ymax - fymax:.3f}" '
                f'width="{fxmax - fxmin:.3f}" height="{fymax - fymin:.3f}" '
                'fill="#b42318" fill-opacity="0.13" stroke="none"><title>'
                "Нет геометрии КЭ: объединение через плитку запрещено"
                "</title></rect>"
            )
        for item in partition_debug.get("initial_rectangles", ()):
            axmin, aymin, axmax, aymax = item["bbox"]
            partition_atoms.append(
                f'<rect x="{axmin - xmin:.3f}" y="{ymax - aymax:.3f}" '
                f'width="{axmax - axmin:.3f}" height="{aymax - aymin:.3f}" '
                'fill="none" stroke="#263238" stroke-opacity="0.32" '
                'stroke-width="1" vector-effect="non-scaling-stroke"><title>'
                f'Начальный атом · уровень {item["level_index"]}'
                "</title></rect>"
            )

    zones: list[str] = []
    installed_envelopes: list[str] = []
    bars: list[str] = []
    for index, zone in enumerate(solution.zones):
        zxmin, zymin, zxmax, zymax = zone.bbox
        dxmin, dymin, dxmax, dymax = zone.demand_bbox
        color = ZONE_COLORS[index % len(ZONE_COLORS)]
        installed_envelopes.append(
            f'<rect x="{zxmin - xmin:.3f}" y="{ymax - zymax:.3f}" '
            f'width="{zxmax - zxmin:.3f}" height="{zymax - zymin:.3f}" '
            f'fill="none" stroke="{color}" stroke-dasharray="7 5" '
            'stroke-width="2" vector-effect="non-scaling-stroke"><title>'
            f'{html.escape(zone.id)} · установленный envelope после анкеровки/раскроя'
            "</title></rect>"
        )
        zones.append(
            f'<rect x="{dxmin - xmin:.3f}" y="{ymax - dymax:.3f}" '
            f'width="{dxmax - dxmin:.3f}" height="{dymax - dymin:.3f}" '
            f'fill="{color}" fill-opacity="0.10" stroke="{color}" '
            'stroke-width="4" vector-effect="non-scaling-stroke"><title>'
            f'{html.escape(zone.id)} · прямоугольник разбиения · '
            f'уровень {zone.level_index} · ⌀{zone.rebar.diameter}/{zone.rebar.step} · '
            f'{zone.mass_kg:.1f} кг'
            "</title></rect>"
        )
        for bar_index, segment in enumerate(
            bar_segments(problem.demand.direction.axis, zone),
            1,
        ):
            if problem.demand.direction.axis.value == "X":
                x1 = segment.longitudinal_start_mm - xmin
                x2 = segment.longitudinal_end_mm - xmin
                y1 = y2 = ymax - segment.coordinate_mm
            else:
                x1 = x2 = segment.coordinate_mm - xmin
                y1 = ymax - segment.longitudinal_end_mm
                y2 = ymax - segment.longitudinal_start_mm
            bars.append(
                f'<line x1="{x1:.3f}" y1="{y1:.3f}" x2="{x2:.3f}" y2="{y2:.3f}" '
                f'stroke="{color}" stroke-width="2" vector-effect="non-scaling-stroke" '
                'stroke-linecap="round"><title>'
                f'{html.escape(zone.id)} · стержень {bar_index}/{zone.bar_count} · '
                f'⌀{zone.rebar.diameter} · L={zone.installed_length_mm:.0f} мм'
                "</title></line>"
            )

    return (
        f'<svg viewBox="0 0 {width:.3f} {height:.3f}" role="img" '
        f'aria-label="Раскладка {html.escape(solution.algorithm)}">'
        f'<g class="cells">{"".join(cells)}</g>'
        f'<g class="partition-domain">{"".join(partition_domain)}</g>'
        f'<g class="partition-atoms">{"".join(partition_atoms)}</g>'
        f'<g class="installed-envelopes">{"".join(installed_envelopes)}</g>'
        f'<g class="zone-shapes">{"".join(zones)}</g>'
        f'<g class="bar-axes">{"".join(bars)}</g></svg>'
    )
