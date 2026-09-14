"""Составная схема: каждая добавка и её реальные оси, не старый равномерный LayoutZone."""
import html

from rebar.models import Axis
from rebar.optimization.services.axis_patterns import pattern_coordinates

from .svg import ZONE_COLORS, _aci_hex


def render_composite_svg(demand, zones, *, host_envelope=None, outline_cell_ids=(), physical_bars=None) -> str:
    if physical_bars is not None and zones:
        raise ValueError("Render physical bars or source zones, not both inventories at once")
    boxes = [demand.bbox]
    if host_envelope is not None:
        boxes.append(host_envelope.outer_mm)
    outlined = frozenset(outline_cell_ids)
    lines = []
    for zone in zones:
        for component in zone.components:
            start, end = component.longitudinal_interval_mm
            for coord in pattern_coordinates(component.placement, component.axis_window_mm):
                box = (start, coord, end, coord) if demand.direction.axis is Axis.X else (coord, start, coord, end)
                boxes.append(box)
                title = (f"{zone.id}; добавка {component.component_index + 1}; Ø{component.rebar.diameter}; "
                         f"L={component.installed_length_mm:.3f} мм; условный шаг {component.rebar.step}")
                lines.append((box, title, ZONE_COLORS[component.component_index % len(ZONE_COLORS)]))
    for bar in physical_bars or ():
        start, end = bar["longitudinal_mm"]
        coord = bar["coordinate_mm"]
        bounds = (start, coord, end, coord) if demand.direction.axis is Axis.X else (coord, start, coord, end)
        boxes.append(bounds)
        lines.append((bounds, f"{bar['id']}; Ø{bar['diameter_mm']}; L={end-start:.3f} мм; "
                      "физический стержень после обработки", ZONE_COLORS[0]))
    xmin, ymin = min(b[0] for b in boxes), min(b[1] for b in boxes)
    xmax, ymax = max(b[2] for b in boxes), max(b[3] for b in boxes)
    width, height = max(1, xmax - xmin), max(1, ymax - ymin)
    cells = []
    for cell in demand.cells:
        points = " ".join(f"{x-xmin:.3f},{ymax-y:.3f}" for x, y in cell.poly)
        color = _aci_hex(cell.aci) if cell.aci else "#d9e4ec"
        outline = (' stroke="#dc2626" stroke-width="2" vector-effect="non-scaling-stroke"'
                   if cell.id in outlined else "")
        note = "; противоречие продольной анкеровке у края" if cell.id in outlined else ""
        cells.append(f'<polygon data-cell-id="{html.escape(str(cell.id), quote=True)}" points="{points}" '
                     f'fill="{color}" fill-opacity="0.35"{outline}><title>'
                     f'КЭ {html.escape(str(cell.id))}; уровень {cell.level_index}{note}</title></polygon>')
    bars = []
    for (x1, y1, x2, y2), title, color in lines:
        bars.append(f'<line x1="{x1-xmin:.3f}" x2="{x2-xmin:.3f}" y1="{ymax-y1:.3f}" y2="{ymax-y2:.3f}" '
                    f'stroke="{color}" stroke-width="1" vector-effect="non-scaling-stroke"><title>'
                    f'{html.escape(title)}</title></line>')
    contours = []
    if host_envelope is not None:
        for i, (x1, y1, x2, y2) in enumerate((host_envelope.outer_mm, *host_envelope.openings_mm)):
            contours.append(f'<rect x="{x1-xmin:.3f}" y="{ymax-y2:.3f}" width="{x2-x1:.3f}" height="{y2-y1:.3f}" '
                            f'fill="none" stroke="{"#dc2626" if i else "#111827"}" stroke-width="2" '
                            f'vector-effect="non-scaling-stroke"><title>{"Проём" if i else "Контур host"}</title></rect>')
    label = "Фактические оси" if lines else "Исходный спрос; раскладка отсутствует"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.3f} {height:.3f}" role="img" '
            f'aria-label="{label} {html.escape(str(demand.direction))}">'
            f'<g class="cells">{"".join(cells)}</g><g class="bar-axes">{"".join(bars)}</g>'
            f'<g class="host-contours">{"".join(contours)}</g></svg>')
